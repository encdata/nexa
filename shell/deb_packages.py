import configparser
import hashlib
import json
import lzma
import os
from pathlib import Path
import re
import shlex
import shutil
import struct
import subprocess
import tempfile
import time
from xml.sax.saxutils import escape
from urllib.parse import urljoin, urlsplit
from urllib.request import urlopen

from shell.x86_runtime import box64_command, node_runtime_path, runner_shell


PACKAGE_ROOT = Path.home() / ".local" / "share" / "nexa" / "deb-packages"
DEPENDENCY_PREFIX = "nexa-dependency-"
SHORTCUT_ROOT = Path.home() / "Nexa Apps"
WEBVIEW_APPS_PATH = Path.home() / ".config" / "nexa" / "webview-apps.json"
DEBIAN_SUITE = "trixie"
DEBIAN_INDEX_URL = (f"https://deb.debian.org/debian/dists/{DEBIAN_SUITE}"
                    "/main/binary-amd64/Packages.xz")
DEBIAN_BASE_URL = "https://deb.debian.org/debian/"
DEBIAN_CACHE = Path.home() / ".cache" / "nexa" / "debian"
_VALID_ID = re.compile(r"[^a-zA-Z0-9_.-]+")
_webview_apps_cache = None


class DebApp:
    external_launcher = True
    preload_ui = False

    def __init__(self, app_id, name, icon, shortcut, argv, cwd, env, description=""):
        self.app_id = app_id
        self.name = name
        self.description = description or "Linux app launched by Nexa"
        self.summary = self.description
        if len(self.summary) > 48:
            self.summary = self.summary[:45].rsplit(" ", 1)[0] + "…"
        self.icon = icon
        self.shortcut = str(shortcut)
        self.launch_spec = (tuple(argv), str(cwd), env)

    def supports_file(self, path):
        return os.path.abspath(path) == self.shortcut

    def build(self, *_args, **_kwargs):
        raise RuntimeError("Debian apps are launched through the Wayland host")


class DebElectronApp(DebApp):
    external_launcher = False

    def __init__(self, app_id, name, icon, shortcut, argv, cwd, env,
                 archive, entry, main_entry, website_url=None, description=""):
        super().__init__(app_id, name, icon, shortcut, argv, cwd, env,
                         description or "Electron app hosted by Nexa")
        self.archive = str(archive) if archive is not None else None
        self.renderer_entry = entry
        self.main_entry = main_entry
        self.website_url = website_url
        self._native_addon_cache = None

    def _native_addons(self):
        if self._native_addon_cache is not None:
            return self._native_addon_cache
        if not self.archive:
            self._native_addon_cache = ()
            return self._native_addon_cache
        runtime_directory = Path(self.archive).parent.parent
        roots = (runtime_directory / "modules",
                 Path(f"{self.archive}.unpacked"))
        addons = [addon for root in roots if root.is_dir()
                  for addon in root.rglob("*.node") if addon.is_file()]
        validated = []
        for addon in addons:
            try:
                with addon.open("rb") as source:
                    header = source.read(20)
                    if (len(header) != 20 or header[:4] != b"\x7fELF"
                            or header[4] != 2 or header[5] != 1
                            or int.from_bytes(header[18:20], "little") != 62):
                        continue
                    validated.append(addon)
            except OSError:
                continue
        self._native_addon_cache = tuple(validated)
        return self._native_addon_cache

    @property
    def requires_x86_node(self):
        return bool(self._native_addons())

    def _node_runtime(self):
        node = node_runtime_path()
        runner = shutil.which("glibc-runner")
        if not self.requires_x86_node or node is None or runner is None:
            return None, None, dict(self.launch_spec[2])
        environment = dict(self.launch_spec[2])
        environment.setdefault("BOX64_PROFILE", "safe")
        library_paths = [str(addon.parent) for addon in self._native_addons()]
        library_paths.extend(environment.get("BOX64_LD_LIBRARY_PATH", "").split(os.pathsep))
        environment["BOX64_LD_LIBRARY_PATH"] = os.pathsep.join(
            dict.fromkeys(path for path in library_paths if path))
        shell = runner_shell()
        if shell is not None:
            environment["SHELL"] = shell
        return [box64_command(), str(node)], runner, environment

    def build(self, activity, parent, context=None):
        from shell.electron_host import ElectronRenderer, ElectronWebsiteRenderer
        if self.website_url:
            return ElectronWebsiteRenderer(activity, parent, self.website_url)
        node, node_runner, host_environment = self._node_runtime()
        return ElectronRenderer(activity, parent, self.archive,
                                self.renderer_entry, node=node, main_entry=self.main_entry,
                                app_id=self.app_id, app_name=self.name, icon=self.icon,
                                host_environment=host_environment,
                                node_runner=node_runner)

    def build_with_services(self, activity, parent, context, services):
        from shell.electron_host import ElectronRenderer, ElectronWebsiteRenderer
        if self.website_url:
            return ElectronWebsiteRenderer(activity, parent, self.website_url)
        node, node_runner, host_environment = self._node_runtime()
        return ElectronRenderer(
            activity, parent, self.archive, self.renderer_entry, node=node,
            main_entry=self.main_entry, app_id=self.app_id,
            app_name=self.name, icon=self.icon,
            window_host=services.get("window_host"),
            close_window=services.get("close_window"),
            host_environment=host_environment, node_runner=node_runner)


def _asar_header(archive):
    with archive.open("rb") as stream:
        prefix = stream.read(16)
        if len(prefix) != 16:
            raise ValueError("asar header is truncated")
        header_size = struct.unpack_from("<I", prefix, 4)[0]
        json_size = struct.unpack_from("<I", prefix, 12)[0]
        if json_size <= 0 or json_size + 16 > header_size + 8:
            raise ValueError("asar header size is invalid")
        header = json.loads(stream.read(json_size).decode("utf-8"))
    return header_size, header


def _asar_file_node(header, path):
    node = header
    for part in path.split("/"):
        node = node.get("files", {}).get(part)
        if not isinstance(node, dict):
            return None
    return node


def _read_asar_file(archive, header_size, node):
    if node.get("unpacked"):
        return json.loads(Path(f"{archive}.unpacked/package.json").read_text(encoding="utf-8"))
    with archive.open("rb") as stream:
        stream.seek(8 + header_size + int(node["offset"]))
        return json.loads(stream.read(int(node["size"])).decode("utf-8"))


def _electron_bundle(package, root):
    candidates = set()
    for pattern in (
            "usr/lib/*/resources/app.asar",
            "usr/lib/*/*/resources/app.asar",
            "usr/lib/*/app.asar",
            "opt/*/resources/app.asar",
            "opt/*/*/resources/app.asar",
            "opt/*/app.asar",
            "usr/share/*/resources/app.asar",
            "usr/share/*/*/resources/app.asar",
            "usr/share/*/app.asar"):
        candidates.update(root.glob(pattern))

    config_root = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / package
    if config_root.is_dir():
        candidates.update(config_root.glob("*/resources/app.asar"))
        candidates.update(config_root.glob("app-*/resources/app.asar"))
    archives = sorted(set(candidates), key=lambda path: path.stat().st_mtime, reverse=True)
    for archive in archives:
        try:
            header_size, header = _asar_header(archive)
            package_node = _asar_file_node(header, "package.json")
            if package_node is None:
                continue
            if package_node.get("unpacked"):
                package_data = json.loads(
                    Path(f"{archive}.unpacked/package.json").read_text(encoding="utf-8"))
            else:
                package_data = _read_asar_file(archive, header_size, package_node)
            main_entry = package_data.get("main", "index.js")
            if not isinstance(main_entry, str) or Path(main_entry).is_absolute() or ".." in Path(main_entry).parts:
                continue

            renderer_entry = "index.html"
            root_index = _asar_file_node(header, renderer_entry)
            if root_index is None or "files" in root_index:
                def find_index(directory, prefix=""):
                    for name, item in directory.get("files", {}).items():
                        relative = f"{prefix}/{name}".strip("/")
                        if "files" in item:
                            found = find_index(item, relative)
                            if found is not None:
                                return found
                        elif name == "index.html":
                            return relative
                    return None

                renderer_entry = find_index(header) or renderer_entry
            return archive, renderer_entry, main_entry
        except (OSError, ValueError, KeyError, json.JSONDecodeError) as error:
            print(f"nexa: couldn't inspect Electron archive {archive}: {error}")
    return None


def _run(*args):
    result = subprocess.run(args, check=True, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True)
    return result.stdout.strip()


def _desktop_files(root):
    directory = root / "usr" / "share" / "applications"
    files = set(directory.rglob("*.desktop")) if directory.is_dir() else set()
    vendor_directory = root / "usr" / "share" / root.name
    if vendor_directory.is_dir():
        files.update(vendor_directory.rglob("*.desktop"))
    return sorted(files)


def _desktop_app_id(package, desktop_file, root):
    applications_directory = root / "usr" / "share" / "applications"
    desktop_base = (applications_directory if applications_directory in desktop_file.parents
                    else root / "usr" / "share")
    desktop_id = _VALID_ID.sub("_", desktop_file.relative_to(desktop_base).as_posix())
    return f"deb.{package}.{desktop_id}"[:120]


def _webview_app_urls():
    global _webview_apps_cache
    if _webview_apps_cache is None:
        try:
            with WEBVIEW_APPS_PATH.open(encoding="utf-8") as source:
                saved = json.load(source)
        except FileNotFoundError:
            saved = {}
        except (OSError, json.JSONDecodeError) as error:
            print(f"nexa: couldn't read WebView app settings: {error}")
            saved = {}
        _webview_apps_cache = (
            {key: value.strip() for key, value in saved.items()
             if isinstance(key, str) and isinstance(value, str) and value.strip()}
            if isinstance(saved, dict) else {})
    return _webview_apps_cache


def set_webview_app_url(app_id, url):
    global _webview_apps_cache
    if not isinstance(app_id, str) or not app_id.strip():
        raise ValueError("app id is required")
    urls = dict(_webview_app_urls())
    if url is None or not str(url).strip():
        urls.pop(app_id, None)
    else:
        url = str(url).strip()
        _validate_webview_url(url)
        urls[app_id] = url

    WEBVIEW_APPS_PATH.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = None
    try:
        with tempfile.NamedTemporaryFile(
                "w", encoding="utf-8", dir=WEBVIEW_APPS_PATH.parent,
                prefix=".webview-apps-", delete=False) as target:
            temporary_path = target.name
            json.dump(urls, target, separators=(",", ":"))
        os.replace(temporary_path, WEBVIEW_APPS_PATH)
    finally:
        if temporary_path and os.path.exists(temporary_path):
            os.remove(temporary_path)
    _webview_apps_cache = urls


def _validate_webview_url(url):
    try:
        parsed_url = urlsplit(url)
        hostname = (parsed_url.hostname or "").lower()
        parsed_url.port
    except ValueError as error:
        raise ValueError(f"invalid Nexa WebView URL: {error}") from error
    local_http = parsed_url.scheme == "http" and hostname in (
        "127.0.0.1", "localhost", "::1")
    if (parsed_url.scheme != "https" and not local_http) or not hostname \
            or parsed_url.username is not None or parsed_url.password is not None:
        raise ValueError("Nexa WebView URL must use HTTPS or local HTTP")


def _find_icon(root, value):
    if not value:
        return "window"
    icon = Path(value)
    if icon.is_absolute():
        candidate = root / icon.relative_to("/")
        if candidate.is_file() and candidate.suffix.lower() == ".png":
            return str(candidate)
    else:
        for base in (root / "usr/share/pixmaps", root / "usr/share/icons"):
            if not base.is_dir():
                continue
            direct = base / (value if value.endswith(".png") else value + ".png")
            if direct.is_file():
                return str(direct)
        for base in (root / "usr/share/pixmaps", root / "usr/share/icons"):
            if base.is_dir():
                matches = list(base.rglob(Path(value).name + ".png"))
                if matches:
                    return str(matches[0])
    return "window"


def _library_path():
    paths = []
    if PACKAGE_ROOT.is_dir():
        for package in PACKAGE_ROOT.iterdir():
            for relative in ("usr/lib/x86_64-linux-gnu", "lib/x86_64-linux-gnu",
                             "usr/lib", "lib"):
                path = package / relative
                if path.is_dir():
                    paths.append(str(path))
    inherited = os.environ.get("BOX64_LD_LIBRARY_PATH", "")
    if inherited:
        paths.extend(inherited.split(os.pathsep))
    return os.pathsep.join(dict.fromkeys(paths))


def _driver_path():
    paths = []
    for package in _package_roots():
        for relative in ("usr/lib/x86_64-linux-gnu/dri", "lib/x86_64-linux-gnu/dri",
                         "usr/lib/dri", "lib/dri"):
            path = package / relative
            if path.is_dir():
                paths.append(str(path))
    return os.pathsep.join(dict.fromkeys(paths))


def graphics_packages_installed():
    installed = set()
    for package in _package_roots():
        try:
            installed.add((package / ".nexa-package").read_text(encoding="utf-8").strip())
        except OSError:
            continue
    return {"libegl1", "libgl1-mesa-dri"}.issubset(installed)


def ensure_graphics_packages(on_progress=None):
    if graphics_packages_installed():
        return
    PACKAGE_ROOT.mkdir(parents=True, exist_ok=True)
    dependencies = _resolve_dependencies([], on_progress)
    total = max(1, len(dependencies))
    for index, item in enumerate(dependencies):
        start = int(index * 70 / total)
        end = int((index + 1) * 70 / total)
        if on_progress is not None:
            on_progress(f"downloading {item['Package']} ({index + 1}/{len(dependencies)})", start)

        def download_progress(copied, size):
            if on_progress is not None:
                on_progress(f"downloading {item['Package']} ({index + 1}/{len(dependencies)})",
                            start + int((end - start) * copied / size))

        package = _download_debian_package(item, download_progress)
        _extract_package(package, item["Package"],
                         PACKAGE_ROOT / f"{DEPENDENCY_PREFIX}{item['Package']}",
                         item["Version"], dependency=True,
                         provides=item.get("Provides", ""),
                         multi_arch=item.get("Multi-Arch", ""))
    if on_progress is not None:
        on_progress("Mesa GPU libraries ready", 100)


def refresh_graphics_environment(environment):
    environment["BOX64_LD_LIBRARY_PATH"] = _library_path()
    drivers = _driver_path()
    if drivers:
        environment["LIBGL_DRIVERS_PATH"] = drivers


def _package_roots():
    if not PACKAGE_ROOT.is_dir():
        return []
    return [path for path in PACKAGE_ROOT.iterdir()
            if path.is_dir() and not path.name.startswith(".")
            and (path / ".nexa-package").is_file()]


def _schema_directory():
    sources = []
    digest = hashlib.sha256()
    for package in _package_roots():
        schema_root = package / "usr/share/glib-2.0/schemas"
        if not schema_root.is_dir():
            continue
        for source in sorted((*schema_root.glob("*.gschema.xml"),
                              *schema_root.glob("*.gschema.override"))):
            stat = source.stat()
            digest.update(package.name.encode())
            digest.update(source.name.encode())
            digest.update(f"{stat.st_size}:{stat.st_mtime_ns}".encode())
            sources.append((package, source))
    if not sources:
        return None

    cache_root = Path.home() / ".cache" / "nexa" / "gsettings"
    cache_root.mkdir(parents=True, exist_ok=True)
    target = cache_root / digest.hexdigest()
    if (target / "gschemas.compiled").is_file():
        return target
    compiler = shutil.which("glib-compile-schemas")
    if compiler is None:
        raise RuntimeError("glib-compile-schemas is required for Debian GSettings schemas")

    staging = Path(tempfile.mkdtemp(prefix="build-", dir=cache_root))
    try:
        for package, source in sources:
            (staging / f"{package.name}-{source.name}").symlink_to(source)
        result = subprocess.run([compiler, str(staging)], capture_output=True, text=True)
        if result.returncode:
            details = result.stderr.strip() or result.stdout.strip()
            raise RuntimeError(details or "glib-compile-schemas failed")
        try:
            staging.rename(target)
        except FileExistsError:
            shutil.rmtree(staging)
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return target


def _fontconfig_file():
    packages = _package_roots()
    config = next((package / "etc/fonts/fonts.conf" for package in packages
                   if (package / "etc/fonts/fonts.conf").is_file()), None)
    font_dirs = sorted(package / "usr/share/fonts" for package in packages
                       if (package / "usr/share/fonts").is_dir())
    if config is None or not font_dirs:
        return None

    stat = config.stat()
    digest = hashlib.sha256(
        f"{config}:{stat.st_size}:{stat.st_mtime_ns}".encode())
    for font_dir in font_dirs:
        digest.update(str(font_dir).encode())
        dir_stat = font_dir.stat()
        digest.update(f"{dir_stat.st_mtime_ns}:{dir_stat.st_size}".encode())
    cache_root = Path.home() / ".cache" / "nexa" / "fontconfig"
    target = cache_root / digest.hexdigest()
    target_file = target / "fonts.conf"
    if target_file.is_file():
        return target_file

    cache_root.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix="build-", dir=cache_root))
    try:
        conf_dir = config.parent / "conf.d"
        if conf_dir.is_dir():
            (staging / "conf.d").symlink_to(conf_dir, target_is_directory=True)
        font_xml = "".join(f"\t<dir>{escape(str(path))}</dir>\n" for path in font_dirs)
        contents = config.read_text(encoding="utf-8").replace(
            "\t<dir>/usr/share/fonts</dir>", font_xml.rstrip())
        cache_dir = Path.home() / ".cache" / "nexa" / "fontconfig-cache"
        cache_dir.mkdir(parents=True, exist_ok=True)
        contents = contents.replace(
            "<cachedir>/var/cache/fontconfig</cachedir>",
            f"<cachedir>{escape(str(cache_dir))}</cachedir>")
        (staging / "fonts.conf").write_text(contents, encoding="utf-8")
        try:
            staging.rename(target)
        except FileExistsError:
            shutil.rmtree(staging)
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return target_file


def _read_package_index(on_progress=None):
    DEBIAN_CACHE.mkdir(parents=True, exist_ok=True)
    index = DEBIAN_CACHE / f"{DEBIAN_SUITE}-amd64-Packages.xz"
    if not index.is_file() or time.time() - index.stat().st_mtime > 86400:
        fd, temporary = tempfile.mkstemp(prefix="Packages-", dir=DEBIAN_CACHE)
        try:
            with os.fdopen(fd, "wb") as output, urlopen(DEBIAN_INDEX_URL, timeout=45) as response:
                total = int(response.headers.get("Content-Length", 0) or 0)
                copied = 0
                while True:
                    chunk = response.read(256 * 1024)
                    if not chunk:
                        break
                    output.write(chunk)
                    copied += len(chunk)
                    if on_progress is not None and total:
                        on_progress("downloading Debian package index",
                                    min(12, 4 + int(copied * 8 / total)))
            os.replace(temporary, index)
        except Exception:
            Path(temporary).unlink(missing_ok=True)
            raise

    packages = {}
    fields = {}

    def save():
        package = fields.get("Package")
        architecture = fields.get("Architecture")
        if package and architecture in ("amd64", "all"):
            packages[package] = fields.copy()

    with lzma.open(index, "rt", encoding="utf-8", errors="replace") as source:
        previous = None
        for line in source:
            if not line.strip():
                save()
                fields.clear()
                previous = None
            elif line[:1].isspace() and previous in fields:
                fields[previous] += " " + line.strip()
            elif ": " in line:
                previous, value = line.split(": ", 1)
                if previous in ("Package", "Version", "Architecture", "Multi-Arch",
                                "Filename", "SHA256", "Depends", "Pre-Depends", "Provides"):
                    fields[previous] = value.strip()
        save()
    return packages


def _split_dependencies(value, delimiter):
    parts = []
    depth = 0
    start = 0
    for index, character in enumerate(value):
        if character == "(":
            depth += 1
        elif character == ")":
            depth = max(0, depth - 1)
        elif character == delimiter and depth == 0:
            parts.append(value[start:index].strip())
            start = index + 1
    parts.append(value[start:].strip())
    return [part for part in parts if part]


def _version_satisfies(installed, operator, required):
    if not operator or not installed:
        return True
    if installed == "system":
        return False
    comparison = {"<<": "lt", "<=": "le", "=": "eq",
                  ">=": "ge", ">>": "gt"}[operator]
    result = subprocess.run(["dpkg", "--compare-versions", installed,
                             comparison, required], check=False)
    return result.returncode == 0


def _dependency_parts(value):
    match = re.match(
        r"^([a-zA-Z0-9][a-zA-Z0-9+.-]*)(?::(?:any|native|amd64))?"
        r"(?:\s*\((<<|<=|=|>=|>>)\s*([^)]*)\))?", value)
    if match is None:
        return None
    return match.group(1), match.group(2), (match.group(3) or "").strip()


def _dependency_qualifier(value):
    qualifier = re.match(r"^[a-zA-Z0-9][a-zA-Z0-9+.-]*:([a-zA-Z0-9-]+)", value)
    return qualifier.group(1) if qualifier is not None else None


def _dependency_applies(value):
    qualifier = _dependency_qualifier(value)
    if qualifier is not None and qualifier not in ("any", "native", "amd64"):
        return False
    restriction = re.search(r"\[([^]]+)\]", value)
    if restriction is not None:
        architectures = restriction.group(1).split()
        def matches(architecture):
            return architecture in ("amd64", "any-amd64", "linux-any",
                                    "linux-amd64", "any")

        if any(item.startswith("!") and matches(item[1:]) for item in architectures):
            return False
        positive = [item for item in architectures if not item.startswith("!")]
        if positive and not any(matches(item) for item in positive):
            return False
    return True


def _installed_versions():
    installed = {}
    for root in _package_roots():
        try:
            name = (root / ".nexa-package").read_text(encoding="utf-8").strip()
            version = (root / ".nexa-version").read_text(encoding="utf-8").strip()
        except OSError:
            continue
        if name:
            installed[name] = version
    try:
        version = _run("glibc-runner", "--shell", "getconf GNU_LIBC_VERSION").split()[-1]
        installed["libc6"] = version
    except (OSError, subprocess.CalledProcessError, IndexError):
        pass
    if (Path(os.environ.get("PREFIX", "/data/data/com.termux/files/usr"))
            / "glibc/lib/libstdc++.so.6").is_file():
        installed["libstdc++6"] = "system"
    return installed


def _provided_versions(value):
    entries = {}
    for item in _split_dependencies(value, ","):
        parts = _dependency_parts(item)
        if parts is not None:
            name, operator, version = parts
            entries[name] = version if operator == "=" else ""
    return entries


def _installed_provides():
    provided = {}
    for root in _package_roots():
        try:
            package = (root / ".nexa-package").read_text(encoding="utf-8").strip()
            value = (root / ".nexa-provides").read_text(encoding="utf-8")
        except OSError:
            continue
        if package:
            provided[package] = _provided_versions(value)
    return provided


def _installed_multi_arch():
    multi_arch = {}
    for root in _package_roots():
        try:
            package = (root / ".nexa-package").read_text(encoding="utf-8").strip()
            value = (root / ".nexa-multi-arch").read_text(encoding="utf-8").strip()
        except OSError:
            continue
        if package:
            multi_arch[package] = value
    return multi_arch


def _resolve_dependencies(dependencies, on_progress=None):
    installed = _installed_versions()
    installed_provides = _installed_provides()
    installed_multi_arch = _installed_multi_arch()
    graphics_packages = [name for name in ("libegl1", "libgl1-mesa-dri")
                         if name not in installed]
    if not graphics_packages and not any(
            item.get("Depends") or item.get("Pre-Depends") for item in dependencies):
        return []
    if on_progress is not None:
        on_progress("reading Debian package catalog", 14)
    packages = _read_package_index(on_progress)
    providers = {}
    for package, fields in packages.items():
        for provided in _split_dependencies(fields.get("Provides", ""), ","):
            parts = _dependency_parts(provided)
            if parts is not None:
                name, operator, version = parts
                if not operator or operator == "=":
                    providers.setdefault(name, []).append(
                        (package, version if operator == "=" else ""))
    for package, entries in installed_provides.items():
        for name, version in entries.items():
            providers.setdefault(name, []).append((package, version))

    resolved = {}
    visiting = set()

    def choose(group):
        candidates = []
        applicable = False
        for alternative in _split_dependencies(group, "|"):
            if not _dependency_applies(alternative):
                continue
            applicable = True
            parts = _dependency_parts(alternative)
            if parts is None:
                continue
            name, operator, version = parts
            qualifier = _dependency_qualifier(alternative)
            if name in packages:
                candidates.append((name, None, False, name, operator, version, qualifier))
            candidates.extend((package, provided_version, True, name, operator, version,
                               qualifier)
                              for package, provided_version in providers.get(name, ()))

        for package, provided_version, virtual, name, operator, version, qualifier in candidates:
            current = installed.get(package)
            if qualifier == "any":
                repository_package = packages.get(package)
                current_multi_arch = installed_multi_arch.get(package)
                if (current_multi_arch is None and repository_package is not None
                        and current == repository_package.get("Version")):
                    current_multi_arch = repository_package.get("Multi-Arch")
                if current_multi_arch != "allowed":
                    continue
            if virtual:
                current_provides = installed_provides.get(package, {})
                current_version = current_provides.get(name)
                current_provided = name in current_provides
                if current and package not in installed_provides:
                    repository_package = packages.get(package)
                    if (repository_package is not None and current ==
                            repository_package.get("Version")):
                        current_provides = _provided_versions(
                            repository_package.get("Provides", ""))
                        current_version = current_provides.get(name)
                        current_provided = name in current_provides
                if current and current_provided and (not operator or (current_version and
                        _version_satisfies(current_version, operator, version))):
                    return None
            else:
                if current and _version_satisfies(current, operator, version):
                    return None

        for package, provided_version, virtual, _name, operator, version, qualifier in candidates:
            if package not in packages:
                continue
            fields = packages[package]
            if qualifier == "any" and fields.get("Multi-Arch") != "allowed":
                continue
            candidate_version = provided_version if virtual else fields.get("Version", "")
            if operator and (not candidate_version or not _version_satisfies(
                    candidate_version, operator, version)):
                continue
            return fields
        if not applicable:
            return None
        raise RuntimeError(f"no Debian amd64 package can satisfy: {group}")

    def visit(fields, include=True):
        package = fields["Package"]
        version = fields.get("Version", "")
        if package in visiting:
            return
        visiting.add(package)
        for group in _split_dependencies(fields.get("Pre-Depends", "") + ","
                                         + fields.get("Depends", ""), ","):
            selected = choose(group)
            if selected is not None:
                visit(selected)
        visiting.remove(package)
        if include:
            installed[package] = version
            installed_provides[package] = _provided_versions(fields.get("Provides", ""))
            installed_multi_arch[package] = fields.get("Multi-Arch", "")
            resolved[package] = fields

    for dependency in dependencies:
        visit(dependency, include=False)
    for package in graphics_packages:
        visit(packages[package])
    return list(resolved.values())


def _download_debian_package(fields, on_progress=None):
    name = fields["Package"]
    version = fields["Version"]
    digest = fields.get("SHA256", "")
    filename = fields.get("Filename", "")
    if not re.fullmatch(r"[a-fA-F0-9]{64}", digest) or not filename:
        raise RuntimeError(f"Debian repository has incomplete metadata for {name}")
    package_cache = DEBIAN_CACHE / "packages"
    package_cache.mkdir(parents=True, exist_ok=True)
    target = package_cache / f"{name}_{version}_amd64.deb"
    if target.is_file():
        hasher = hashlib.sha256()
        with open(target, "rb") as cached:
            for chunk in iter(lambda: cached.read(1024 * 1024), b""):
                hasher.update(chunk)
        if hasher.hexdigest() == digest:
            return target
    fd, temporary = tempfile.mkstemp(prefix=f"{name}-", dir=package_cache)
    try:
        with os.fdopen(fd, "wb") as output, urlopen(
                urljoin(DEBIAN_BASE_URL, filename), timeout=60) as response:
            total = int(response.headers.get("Content-Length", 0) or 0)
            copied = 0
            while True:
                chunk = response.read(256 * 1024)
                if not chunk:
                    break
                output.write(chunk)
                copied += len(chunk)
                if on_progress is not None and total:
                    on_progress(copied, total)
        hasher = hashlib.sha256()
        with open(temporary, "rb") as source:
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                hasher.update(chunk)
        if hasher.hexdigest() != digest:
            raise RuntimeError(f"SHA256 mismatch while downloading {name}")
        os.replace(temporary, target)
    except Exception:
        Path(temporary).unlink(missing_ok=True)
        raise
    return target


def _extract_package(deb, package, target, version, dependency=False, provides="",
                     multi_arch=""):
    target.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f".{package}-", dir=target.parent))
    backup = target.parent / f".{package}-previous"
    dpkg_deb = shutil.which("dpkg-deb")
    try:
        subprocess.run([dpkg_deb, "-x", str(deb), str(staging)], check=True,
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        (staging / ".nexa-package").write_text(package, encoding="utf-8")
        (staging / ".nexa-version").write_text(version, encoding="utf-8")
        (staging / ".nexa-provides").write_text(provides, encoding="utf-8")
        (staging / ".nexa-multi-arch").write_text(multi_arch, encoding="utf-8")
        if dependency:
            (staging / ".nexa-dependency").touch()
        if backup.exists():
            shutil.rmtree(backup)
        if target.exists():
            target.rename(backup)
        try:
            staging.rename(target)
        except Exception:
            if backup.exists():
                backup.rename(target)
            raise
        if backup.exists():
            shutil.rmtree(backup)
    finally:
        if staging.exists():
            shutil.rmtree(staging)


def _resolve_debian_path(root, path):
    candidate = root / path.lstrip("/")
    if candidate.exists():
        return candidate
    for package_root in _package_roots():
        candidate = package_root / path.lstrip("/")
        if candidate.exists():
            return candidate
    return root / path.lstrip("/")


def _package_path_environment(root):
    paths = [str(root / directory)
             for directory in ("usr/bin", "usr/games", "bin")]
    paths.append(os.environ.get("PATH", ""))
    return os.pathsep.join(path for path in paths if path)


def _desktop_app(package, desktop_file, root, schema_directory=None, fontconfig_file=None,
                 electron_bundle=None, desktop_entry=None):
    if desktop_entry is None:
        parser = configparser.ConfigParser(interpolation=None, strict=False)
        parser.read(desktop_file, encoding="utf-8")
        entry = parser["Desktop Entry"] if parser.has_section("Desktop Entry") else None
    else:
        entry = desktop_entry
    if (entry is None or entry.get("Type", "Application") != "Application"
            or entry.getboolean("Hidden", fallback=False)
            or entry.getboolean("NoDisplay", fallback=False)
            or entry.getboolean("Terminal", fallback=False)):
        return None
    name = entry.get("Name", "").strip()
    description = entry.get("Comment", "").strip()
    exec_line = entry.get("Exec", "").strip()
    app_id = _desktop_app_id(package, desktop_file, root)
    website_url = (_webview_app_urls().get(app_id)
                   or entry.get("X-Nexa-WebView-URL", "").strip() or None)
    if not name or (not exec_line and not website_url):
        return None
    if website_url:
        _validate_webview_url(website_url)
    try:
        command = shlex.split(exec_line) if exec_line and not website_url else []
    except ValueError:
        return None
    expanded = []
    for part in command:
        if part in ("%f", "%F", "%u", "%U", "%i"):
            continue
        part = part.replace("%c", name).replace("%k", str(desktop_file))
        part = re.sub(r"%[fFuUdDnNickvm]", "", part)
        if part:
            expanded.append(part)
    command = expanded
    command = [part for part in command if part]
    if not command and not website_url:
        return None
    launch_env = {}
    if not website_url:
        launch_env = {"BOX64_LD_LIBRARY_PATH": _library_path(),
                      "BOX64_PREFER_EMULATED": "1",
                      "PATH": _package_path_environment(root)}
        driver_path = _driver_path()
        if driver_path:
            launch_env["LIBGL_DRIVERS_PATH"] = driver_path
        if schema_directory is not None:
            launch_env["GSETTINGS_SCHEMA_DIR"] = str(schema_directory)
        if fontconfig_file is not None:
            launch_env["FONTCONFIG_FILE"] = str(fontconfig_file)
    if command and Path(command[0]).name == "env":
        command.pop(0)
    while len(command) > 1 and re.fullmatch(r"[a-zA-Z_][a-zA-Z0-9_]*=.*", command[0]):
        key, value = command.pop(0).split("=", 1)
        launch_env[key] = value
    if not command and not website_url:
        return None
    executable = command[0] if command else None
    if executable and executable.startswith("/"):
        command[0] = str(_resolve_debian_path(root, executable))
    elif executable and "/" not in executable:
        for package_root in _package_roots():
            candidate = next((package_root / relative / executable
                              for relative in ("usr/bin", "usr/games", "bin")
                              if (package_root / relative / executable).exists()), None)
            if candidate is not None:
                command[0] = str(candidate)
                break

    if command and not Path(command[0]).exists() and not website_url:
        return None

    working_directory = entry.get("Path", "").strip()
    if working_directory.startswith("/"):
        working_directory = str(_resolve_debian_path(root, working_directory))
    else:
        working_directory = str(root)
    if not os.path.isdir(working_directory):
        working_directory = str(root)

    SHORTCUT_ROOT.mkdir(parents=True, exist_ok=True)
    shortcut = SHORTCUT_ROOT / f"{app_id}.desktop"
    shortcut.write_text(f"[Desktop Entry]\nType=Application\nName={name}\nX-Nexa-App={app_id}\n",
                        encoding="utf-8")
    icon = _find_icon(root, entry.get("Icon"))
    if electron_bundle is not None or website_url:
        archive, renderer_entry, main_entry = electron_bundle or (None, None, None)
        return DebElectronApp(app_id, name, icon, shortcut, command,
                              working_directory, launch_env, archive,
                              renderer_entry, main_entry, website_url, description)
    return DebApp(app_id, name, icon, shortcut, command, working_directory,
                  launch_env, description)


def discover():
    apps = {}
    if not PACKAGE_ROOT.is_dir():
        return apps
    schema_directory = None
    fontconfig_file = None
    runtime_paths_loaded = False

    def load_runtime_paths():
        nonlocal schema_directory, fontconfig_file, runtime_paths_loaded
        if runtime_paths_loaded:
            return
        runtime_paths_loaded = True
        try:
            schema_directory = _schema_directory()
        except (OSError, RuntimeError) as error:
            print(f"nexa: couldn't compile Debian GSettings schemas: {error}")
        try:
            fontconfig_file = _fontconfig_file()
        except OSError as error:
            print(f"nexa: couldn't prepare Debian font configuration: {error}")

    for root in sorted(PACKAGE_ROOT.iterdir()):
        if not root.is_dir():
            continue
        if (root / ".nexa-dependency").exists():
            continue
        metadata = root / ".nexa-package"
        try:
            package = metadata.read_text(encoding="utf-8").strip()
        except OSError:
            continue
        package_apps = []
        desktop_files = _desktop_files(root)
        desktop_entries = {}
        needs_electron_bundle = False
        for desktop_file in desktop_files:
            parser = configparser.ConfigParser(interpolation=None, strict=False)
            try:
                parser.read(desktop_file, encoding="utf-8")
                entry = (parser["Desktop Entry"]
                         if parser.has_section("Desktop Entry") else None)
            except (configparser.Error, OSError) as error:
                desktop_entries[desktop_file] = None
                needs_electron_bundle = True
                print(f"nexa: couldn't read Debian launcher {desktop_file}: {error}")
                continue
            desktop_entries[desktop_file] = entry
            app_id = _desktop_app_id(package, desktop_file, root)
            website_url = (_webview_app_urls().get(app_id)
                           or (entry.get("X-Nexa-WebView-URL", "").strip()
                               if entry is not None else ""))
            if not website_url:
                needs_electron_bundle = True
                break
        electron_bundle = (_electron_bundle(package, root)
                           if desktop_files and needs_electron_bundle else None)
        if needs_electron_bundle:
            load_runtime_paths()
        for desktop_file in desktop_files:
            desktop_entry = desktop_entries.get(desktop_file)
            if desktop_entry is None:
                continue
            try:
                app = _desktop_app(package, desktop_file, root,
                                   schema_directory, fontconfig_file, electron_bundle,
                                   desktop_entry)
            except (configparser.Error, OSError, ValueError) as error:
                print(f"nexa: couldn't read Debian launcher {desktop_file}: {error}")
                continue
            if app is not None:
                package_apps.append(app)
        if not package_apps:
            binary_dir = root / "usr" / "bin"
            binaries = []
            if binary_dir.is_dir():
                for candidate in binary_dir.iterdir():
                    try:
                        with candidate.open("rb") as binary:
                            header = binary.read(20)
                        if (candidate.is_file() and os.access(candidate, os.X_OK)
                                and header[:4] == b"\x7fELF" and header[5] == 1
                                and int.from_bytes(header[18:20], "little") == 62):
                            binaries.append(candidate)
                    except OSError:
                        continue
            if len(binaries) == 1:
                load_runtime_paths()
                candidate = binaries[0]
                name = candidate.name
                app_id = f"deb.{package}.{_VALID_ID.sub('_', name)}"[:120]
                SHORTCUT_ROOT.mkdir(parents=True, exist_ok=True)
                shortcut = SHORTCUT_ROOT / f"{app_id}.desktop"
                shortcut.write_text(
                    f"[Desktop Entry]\nType=Application\nName={name}\nX-Nexa-App={app_id}\n",
                    encoding="utf-8")
                launch_env = {"BOX64_LD_LIBRARY_PATH": _library_path(),
                              "BOX64_PREFER_EMULATED": "1",
                              "PATH": _package_path_environment(root)}
                driver_path = _driver_path()
                if driver_path:
                    launch_env["LIBGL_DRIVERS_PATH"] = driver_path
                if schema_directory is not None:
                    launch_env["GSETTINGS_SCHEMA_DIR"] = str(schema_directory)
                if fontconfig_file is not None:
                    launch_env["FONTCONFIG_FILE"] = str(fontconfig_file)
                package_apps.append(DebApp(
                    app_id, name, "window", shortcut, [str(candidate)], str(root),
                    launch_env))
        apps.update((app.app_id, app) for app in package_apps)
    return apps


def install(path, on_progress=None):
    def report(message, percent=None):
        if on_progress is not None:
            on_progress(message, percent)

    report("checking Debian package", 2)
    deb = Path(path).expanduser().resolve(strict=True)
    dpkg_deb = shutil.which("dpkg-deb")
    if dpkg_deb is None:
        raise RuntimeError("install dpkg with `pkg install dpkg` to install .deb files")
    package = _run(dpkg_deb, "-f", str(deb), "Package")
    architecture = _run(dpkg_deb, "-f", str(deb), "Architecture")
    version = _run(dpkg_deb, "-f", str(deb), "Version")
    if not re.fullmatch(r"[a-z0-9][a-z0-9+.-]*", package):
        raise ValueError("the Debian package has an invalid package name")
    if architecture not in ("amd64", "all"):
        raise ValueError(f"this Nexa runtime supports amd64 .deb files; package is {architecture}")
    PACKAGE_ROOT.mkdir(parents=True, exist_ok=True)
    fields = {"Package": package, "Version": version,
              "Architecture": architecture,
              "Multi-Arch": _run(dpkg_deb, "-f", str(deb), "Multi-Arch"),
              "Pre-Depends": _run(dpkg_deb, "-f", str(deb), "Pre-Depends"),
              "Depends": _run(dpkg_deb, "-f", str(deb), "Depends"),
              "Provides": _run(dpkg_deb, "-f", str(deb), "Provides")}
    report(f"resolving dependencies for {package}", 10)
    dependencies = _resolve_dependencies([fields], report)
    downloaded = []
    total = max(1, len(dependencies))
    for index, item in enumerate(dependencies):
        start = 18 + int(index * 52 / total)
        end = 18 + int((index + 1) * 52 / total)
        report(f"downloading {item['Package']} ({index + 1}/{len(dependencies)})", start)

        def download_progress(copied, size):
            report(f"downloading {item['Package']} ({index + 1}/{len(dependencies)})",
                   start + int((end - start) * copied / size))

        downloaded.append((item, _download_debian_package(item, download_progress)))
    for index, (item, dependency_deb) in enumerate(downloaded):
        report(f"installing dependency {item['Package']} ({index + 1}/{total})",
               72 + int(index * 14 / total))
        _extract_package(dependency_deb, item["Package"],
                         PACKAGE_ROOT / f"{DEPENDENCY_PREFIX}{item['Package']}",
                         item["Version"], dependency=True,
                         provides=item.get("Provides", ""),
                         multi_arch=item.get("Multi-Arch", ""))

    report(f"installing {package}", 88)
    _extract_package(deb, package, PACKAGE_ROOT / package, version,
                     provides=fields["Provides"], multi_arch=fields["Multi-Arch"])
    report("registering desktop apps", 96)
    apps = [app for app in discover().values() if app.app_id.startswith(f"deb.{package}.")]
    report(f"installed {package}", 100)
    return package, len(apps)
