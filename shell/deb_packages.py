import configparser
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import tempfile


PACKAGE_ROOT = Path.home() / ".local" / "share" / "nexa" / "deb-packages"
SHORTCUT_ROOT = Path.home() / "Nexa Apps"
_VALID_ID = re.compile(r"[^a-zA-Z0-9_.-]+")


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


def _package_roots():
    return [path for path in PACKAGE_ROOT.iterdir()
            if path.is_dir() and not path.name.startswith(".")
            and (path / ".nexa-package").is_file()]


def _resolve_debian_path(root, path):
    candidate = root / path.lstrip("/")
    if candidate.exists():
        return candidate
    for package_root in _package_roots():
        candidate = package_root / path.lstrip("/")
        if candidate.exists():
            return candidate
    return root / path.lstrip("/")


def _package_path_environment():
    paths = []
    for package_root in _package_roots():
        paths.extend(str(package_root / directory)
                     for directory in ("usr/bin", "usr/games", "bin"))
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
    launch_env = {"BOX64_LD_LIBRARY_PATH": _library_path(),
                  "PATH": _package_path_environment()}
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
                    {"BOX64_LD_LIBRARY_PATH": _library_path()}))
        apps.update((app.app_id, app) for app in package_apps)
    return apps


def install(path):
    deb = Path(path).expanduser().resolve(strict=True)
    dpkg_deb = shutil.which("dpkg-deb")
    if dpkg_deb is None:
        raise RuntimeError("install dpkg with `pkg install dpkg` to install .deb files")
    package = _run(dpkg_deb, "-f", str(deb), "Package")
    architecture = _run(dpkg_deb, "-f", str(deb), "Architecture")
    if not re.fullmatch(r"[a-z0-9][a-z0-9+.-]*", package):
        raise ValueError("the Debian package has an invalid package name")
    if architecture not in ("amd64", "all"):
        raise ValueError(f"this Nexa runtime supports amd64 .deb files; package is {architecture}")
    PACKAGE_ROOT.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f".{package}-", dir=PACKAGE_ROOT))
    backup = PACKAGE_ROOT / f".{package}-previous"
    target = PACKAGE_ROOT / package
    try:
        subprocess.run([dpkg_deb, "-x", str(deb), str(staging)], check=True,
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        (staging / ".nexa-package").write_text(package, encoding="utf-8")
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
    apps = [app for app in discover().values() if app.app_id.startswith(f"deb.{package}.")]
    return package, len(apps)
