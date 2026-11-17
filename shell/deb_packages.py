import configparser
import hashlib
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
from urllib.parse import urljoin
from urllib.request import urlopen


PACKAGE_ROOT = Path.home() / ".local" / "share" / "nexa" / "deb-packages"
DEPENDENCY_PREFIX = "nexa-dependency-"
SHORTCUT_ROOT = Path.home() / "Nexa Apps"
DEBIAN_SUITE = "trixie"
DEBIAN_INDEX_URL = (f"https://deb.debian.org/debian/dists/{DEBIAN_SUITE}"
                    "/main/binary-amd64/Packages.xz")
DEBIAN_BASE_URL = "https://deb.debian.org/debian/"
DEBIAN_CACHE = Path.home() / ".cache" / "nexa" / "debian"
_VALID_ID = re.compile(r"[^a-zA-Z0-9_.-]+")
_library_index = None
_library_index_signature = None


class DebApp:
    external_launcher = True
    preload_ui = False

    def __init__(self, app_id, name, icon, shortcut, argv, cwd, env):
        self.app_id = app_id
        self.name = name
        self.icon = icon
        self.shortcut = str(shortcut)
        self.launch_spec = (tuple(argv), str(cwd), env)

    def supports_file(self, path):
        return os.path.abspath(path) == self.shortcut

    def build(self, *_args, **_kwargs):
        raise RuntimeError("Debian apps are launched through the Wayland host")


def _run(*args):
    result = subprocess.run(args, check=True, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True)
    return result.stdout.strip()


def _desktop_files(root):
    directory = root / "usr" / "share" / "applications"
    return sorted(directory.rglob("*.desktop")) if directory.is_dir() else []


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


def _elf_dependencies(path):
    try:
        with open(path, "rb") as binary:
            header = binary.read(64)
            if (len(header) != 64 or header[:4] != b"\x7fELF"
                    or header[4:6] != b"\x02\x01"):
                return ()
            ph_offset = struct.unpack_from("<Q", header, 32)[0]
            ph_size, ph_count = struct.unpack_from("<HH", header, 54)
            loads = []
            dynamic = None
            for index in range(ph_count):
                binary.seek(ph_offset + index * ph_size)
                entry = binary.read(ph_size)
                if len(entry) < 56:
                    return ()
                kind, _, offset, address, _, file_size, _, _ = struct.unpack_from(
                    "<IIQQQQQQ", entry)
                if kind == 1:
                    loads.append((address, address + file_size, offset))
                elif kind == 2:
                    dynamic = (offset, file_size)
            if dynamic is None:
                return ()
            binary.seek(dynamic[0])
            entries = binary.read(dynamic[1])
            string_table = None
            needed = []
            for offset in range(0, len(entries) - 15, 16):
                tag, value = struct.unpack_from("<qQ", entries, offset)
                if tag == 0:
                    break
                if tag == 1:
                    needed.append(value)
                elif tag == 5:
                    string_table = value
            if string_table is None:
                return ()
            string_offset = next((file_offset + string_table - start
                                  for start, end, file_offset in loads
                                  if start <= string_table < end), None)
            if string_offset is None:
                return ()
            binary.seek(string_offset)
            strings = binary.read(max(needed, default=-1) + 256)
            return tuple(name.decode("ascii", "ignore")
                         for offset in needed
                         if (name := strings[offset:strings.find(b"\0", offset)])
                         and b"/" not in name)
    except (OSError, struct.error, OverflowError):
        return ()


def _debian_library_index():
    global _library_index, _library_index_signature
    roots = _package_roots()
    signature = tuple((str(root), (root / ".nexa-package").stat().st_mtime_ns)
                      for root in roots)
    if _library_index is not None and signature == _library_index_signature:
        return _library_index
    index = {}
    for root in roots:
        for base in (root / "usr/lib", root / "lib"):
            if not base.is_dir():
                continue
            for directory, _, files in os.walk(base):
                for name in files:
                    if ".so" not in name:
                        continue
                    path = Path(directory) / name
                    if path.is_file():
                        index.setdefault(name, path)
    _library_index = index
    _library_index_signature = signature
    return index


def _library_path(executable=None):
    paths = []
    if executable:
        index = _debian_library_index()
        pending = list(_elf_dependencies(executable))
        libraries = {}
        while pending:
            name = pending.pop()
            if name in libraries:
                continue
            library = index.get(name)
            if library is None:
                continue
            libraries[name] = library
            pending.extend(_elf_dependencies(library))
        if libraries:
            identity = hashlib.sha256((str(executable) + "\0" + "\0".join(
                f"{name}:{path}" for name, path in sorted(libraries.items()))
            ).encode()).hexdigest()[:20]
            overlay = DEBIAN_CACHE / "library-overlays" / identity
            overlay.mkdir(parents=True, exist_ok=True)
            for name, library in libraries.items():
                link = overlay / name
                if not link.exists():
                    try:
                        link.symlink_to(library)
                    except FileExistsError:
                        pass
            paths.append(str(overlay))
    inherited = os.environ.get("BOX64_LD_LIBRARY_PATH", "")
    if inherited:
        paths.extend(inherited.split(os.pathsep))
    return os.pathsep.join(dict.fromkeys(paths))


def _package_roots():
    return [path for path in PACKAGE_ROOT.iterdir()
            if path.is_dir() and not path.name.startswith(".")
            and (path / ".nexa-package").is_file()]


def _read_package_index():
    DEBIAN_CACHE.mkdir(parents=True, exist_ok=True)
    index = DEBIAN_CACHE / f"{DEBIAN_SUITE}-amd64-Packages.xz"
    if not index.is_file() or time.time() - index.stat().st_mtime > 86400:
        fd, temporary = tempfile.mkstemp(prefix="Packages-", dir=DEBIAN_CACHE)
        try:
            with os.fdopen(fd, "wb") as output, urlopen(DEBIAN_INDEX_URL, timeout=45) as response:
                shutil.copyfileobj(response, output)
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
                if previous in ("Package", "Version", "Architecture", "Filename",
                                "SHA256", "Depends", "Pre-Depends", "Provides"):
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
        r"^([a-zA-Z0-9][a-zA-Z0-9+.-]*)(?::(?:any|amd64))?"
        r"(?:\s*\((<<|<=|=|>=|>>)\s*([^)]*)\))?", value)
    if match is None:
        return None
    return match.group(1), match.group(2), (match.group(3) or "").strip()


def _dependency_applies(value):
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


def _resolve_dependencies(dependencies):
    if not any(item.get("Depends") or item.get("Pre-Depends") for item in dependencies):
        return []
    packages = _read_package_index()
    providers = {}
    for package, fields in packages.items():
        for provided in _split_dependencies(fields.get("Provides", ""), ","):
            name = re.match(r"[a-zA-Z0-9][a-zA-Z0-9+.-]*", provided)
            if name:
                providers.setdefault(name.group(), package)

    installed = _installed_versions()
    resolved = {}
    visiting = set()

    def choose(group):
        for alternative in _split_dependencies(group, "|"):
            if not _dependency_applies(alternative):
                continue
            parts = _dependency_parts(alternative)
            if parts is None:
                continue
            name, operator, version = parts
            package = name if name in packages else providers.get(name)
            if package is None:
                continue
            current = installed.get(name) or installed.get(package)
            if current and _version_satisfies(current, operator, version):
                return None
            return packages[package]
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
            resolved[package] = fields

    for dependency in dependencies:
        visit(dependency, include=False)
    return list(resolved.values())


def _download_debian_package(fields):
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
            shutil.copyfileobj(response, output)
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


def _extract_package(deb, package, target, version, dependency=False):
    target.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f".{package}-", dir=target.parent))
    backup = target.parent / f".{package}-previous"
    dpkg_deb = shutil.which("dpkg-deb")
    try:
        subprocess.run([dpkg_deb, "-x", str(deb), str(staging)], check=True,
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        (staging / ".nexa-package").write_text(package, encoding="utf-8")
        (staging / ".nexa-version").write_text(version, encoding="utf-8")
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


def _desktop_app(package, desktop_file, root):
    parser = configparser.ConfigParser(interpolation=None, strict=False)
    parser.read(desktop_file, encoding="utf-8")
    entry = parser["Desktop Entry"] if parser.has_section("Desktop Entry") else None
    if (entry is None or entry.get("Type", "Application") != "Application"
            or entry.getboolean("Hidden", fallback=False)
            or entry.getboolean("NoDisplay", fallback=False)
            or entry.getboolean("Terminal", fallback=False)):
        return None
    name = entry.get("Name", "").strip()
    exec_line = entry.get("Exec", "").strip()
    if not name or not exec_line:
        return None
    try:
        command = shlex.split(exec_line)
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
    if not command:
        return None
    launch_env = {"PATH": _package_path_environment(root)}
    if Path(command[0]).name == "env":
        command.pop(0)
    while len(command) > 1 and re.fullmatch(r"[a-zA-Z_][a-zA-Z0-9_]*=.*", command[0]):
        key, value = command.pop(0).split("=", 1)
        launch_env[key] = value
    if not command:
        return None
    executable = command[0]
    if executable.startswith("/"):
        command[0] = str(_resolve_debian_path(root, executable))
    elif "/" not in executable:
        for package_root in _package_roots():
            candidate = next((package_root / relative / executable
                              for relative in ("usr/bin", "usr/games", "bin")
                              if (package_root / relative / executable).exists()), None)
            if candidate is not None:
                command[0] = str(candidate)
                break

    if not Path(command[0]).exists():
        return None

    working_directory = entry.get("Path", "").strip()
    if working_directory.startswith("/"):
        working_directory = str(_resolve_debian_path(root, working_directory))
    else:
        working_directory = str(root)
    if not os.path.isdir(working_directory):
        working_directory = str(root)

    if package == "discord" and Path(command[0]).name == "discord":
        config_root = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
        installed_apps = sorted((config_root / "discord").glob("app-*/Discord"), reverse=True)
        if installed_apps:
            command = [str(installed_apps[0]), "--no-sandbox",
                       "--ozone-platform=wayland",
                       "--enable-features=UseOzonePlatform"]
            working_directory = str(installed_apps[0].parent)

    launch_env.setdefault("BOX64_LD_LIBRARY_PATH", _library_path(command[0]))

    desktop_id = _VALID_ID.sub("_", desktop_file.relative_to(root / "usr/share/applications").as_posix())
    app_id = f"deb.{package}.{desktop_id}"[:120]
    SHORTCUT_ROOT.mkdir(parents=True, exist_ok=True)
    shortcut = SHORTCUT_ROOT / f"{app_id}.desktop"
    shortcut.write_text(f"[Desktop Entry]\nType=Application\nName={name}\nX-Nexa-App={app_id}\n",
                        encoding="utf-8")
    return DebApp(app_id, name, _find_icon(root, entry.get("Icon")), shortcut,
                  command, working_directory, launch_env)


def discover():
    apps = {}
    if not PACKAGE_ROOT.is_dir():
        return apps
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
        for desktop_file in _desktop_files(root):
            try:
                app = _desktop_app(package, desktop_file, root)
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
                candidate = binaries[0]
                name = candidate.name
                app_id = f"deb.{package}.{_VALID_ID.sub('_', name)}"[:120]
                SHORTCUT_ROOT.mkdir(parents=True, exist_ok=True)
                shortcut = SHORTCUT_ROOT / f"{app_id}.desktop"
                shortcut.write_text(
                    f"[Desktop Entry]\nType=Application\nName={name}\nX-Nexa-App={app_id}\n",
                    encoding="utf-8")
                package_apps.append(DebApp(
                    app_id, name, "window", shortcut, [str(candidate)], str(root),
                    {"BOX64_LD_LIBRARY_PATH": _library_path(str(candidate)),
                     "PATH": _package_path_environment(root)}))
        apps.update((app.app_id, app) for app in package_apps)
    return apps


def install(path):
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
              "Pre-Depends": _run(dpkg_deb, "-f", str(deb), "Pre-Depends"),
              "Depends": _run(dpkg_deb, "-f", str(deb), "Depends")}
    dependencies = _resolve_dependencies([fields])
    downloaded = [(item, _download_debian_package(item)) for item in dependencies]
    for item, dependency_deb in downloaded:
        _extract_package(dependency_deb, item["Package"],
                         PACKAGE_ROOT / f"{DEPENDENCY_PREFIX}{item['Package']}",
                         item["Version"], dependency=True)

    _extract_package(deb, package, PACKAGE_ROOT / package, version)
    apps = [app for app in discover().values() if app.app_id.startswith(f"deb.{package}.")]
    return package, len(apps)
