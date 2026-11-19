import os
import logging
import hashlib
from pathlib import Path
import re
import shutil
import shlex
import subprocess
import tarfile
import tempfile
from threading import Lock
from urllib.request import Request, urlopen

_PACKAGE_PROGRESS = re.compile(r"\[\s*(\d{1,3})%\]")
_NODE_VERSION = "v24.22.3"
_NODE_ARCHIVE = f"node-{_NODE_VERSION}-linux-x64.tar.xz"
_NODE_SHA256 = "4f2b175a6d53a5c617a80dba2676238a0688cef28c6da4eba35c7d844e66fcfb"
_NODE_RUNTIME = (Path.home() / ".local/share/nexa/runtime"
                 / f"node-{_NODE_VERSION}-linux-x64/bin/node")
_node_install_lock = Lock()
logger = logging.getLogger("nexa.x86")


def node_runtime_path():
    if not os.access(_NODE_RUNTIME, os.X_OK):
        return None
    try:
        with _NODE_RUNTIME.open("rb") as source:
            header = source.read(20)
    except OSError:
        return None
    if (len(header) != 20 or header[:4] != b"\x7fELF" or header[4] != 2
            or header[5] != 1 or int.from_bytes(header[18:20], "little") != 62):
        return None
    return _NODE_RUNTIME


def ensure_node_runtime(on_progress=None):
    runtime = node_runtime_path()
    if runtime is not None:
        return runtime
    if not installed():
        raise RuntimeError("the Box64 runtime is required before installing x86 Node.js")

    with _node_install_lock:
        runtime = node_runtime_path()
        if runtime is not None:
            return runtime
        parent = _NODE_RUNTIME.parent
        parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix=".node-runtime-", dir=parent) as staging:
            archive = Path(staging) / _NODE_ARCHIVE
            url = f"https://nodejs.org/dist/{_NODE_VERSION}/{_NODE_ARCHIVE}"
            request = Request(url, headers={"User-Agent": "Nexa/1"})
            with urlopen(request, timeout=30) as response, archive.open("wb") as target:
                total = int(response.headers.get("Content-Length") or 0)
                copied = 0
                while True:
                    chunk = response.read(256 * 1024)
                    if not chunk:
                        break
                    target.write(chunk)
                    copied += len(chunk)
                    if on_progress is not None and total:
                        on_progress("downloading x86 Node.js", min(90, copied * 90 // total))

            digest = hashlib.sha256()
            with archive.open("rb") as source:
                while chunk := source.read(1024 * 1024):
                    digest.update(chunk)
            if digest.hexdigest() != _NODE_SHA256:
                raise RuntimeError("x86 Node.js archive checksum did not match")

            member_name = f"node-{_NODE_VERSION}-linux-x64/bin/node"
            with tarfile.open(archive, "r:xz") as bundle:
                member = bundle.getmember(member_name)
                if not member.isfile() or member.size > 256 * 1024 * 1024:
                    raise RuntimeError("x86 Node.js archive has an invalid executable")
                source = bundle.extractfile(member)
                if source is None:
                    raise RuntimeError("x86 Node.js executable is missing")
                temporary_node = Path(staging) / "node"
                with source, temporary_node.open("wb") as target:
                    shutil.copyfileobj(source, target)
            with temporary_node.open("rb") as source:
                header = source.read(20)
            if (len(header) != 20 or header[:4] != b"\x7fELF" or header[4] != 2
                    or header[5] != 1 or int.from_bytes(header[18:20], "little") != 62):
                raise RuntimeError("downloaded Node.js binary is not Linux x86_64")
            temporary_node.chmod(0o700)
            _NODE_RUNTIME.parent.mkdir(parents=True, exist_ok=True)
            os.replace(temporary_node, _NODE_RUNTIME)
        if on_progress is not None:
            on_progress("x86 Node.js ready", 100)
        logger.info("installed standalone x86 Node.js %s", _NODE_VERSION)
        return _NODE_RUNTIME

def box64_command():
    runtime = Path.home() / ".local" / "share" / "nexa" / "runtime" / "box64"
    if runtime.is_file() and os.access(runtime, os.X_OK):
        return str(runtime)
    return "box64"


def runner_shell():
    prefix = Path(os.environ.get("PREFIX", "/data/data/com.termux/files/usr"))
    shell = prefix / "glibc" / "bin" / "bash"
    return str(shell) if os.access(shell, os.X_OK) else None


def installed():
    prefix = os.environ.get("PREFIX", "/data/data/com.termux/files/usr")
    glibc = Path(prefix) / "glibc"
    return (shutil.which("grun") is not None
            and (glibc / "bin" / "box64").is_file()
            and (glibc / "bin" / "bash").is_file()
            and (glibc / "lib" / "libwayland-client.so.0").is_file())


def _box64_check():
    if not installed():
        logger.error("runtime check failed: runtime packages or Wayland libraries are missing")
        return False, "runtime packages or Wayland libraries are missing"
    runner = shutil.which("glibc-runner")
    if runner is None:
        logger.error("runtime check failed: glibc-runner is missing")
        return False, "glibc-runner is missing"
    environment = os.environ.copy()
    shell = runner_shell()
    if shell is not None:
        environment["SHELL"] = shell
    try:
        result = subprocess.run([runner, "--shell",
                                 f"{shlex.quote(box64_command())} --version"],
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                text=True, timeout=60, env=environment)
    except subprocess.TimeoutExpired as error:
        output = error.stdout or ""
        if isinstance(output, bytes):
            output = output.decode("utf-8", "replace")
        details = f"Box64 check timed out after 60 seconds: {output.strip()}"
        logger.error("%s", details)
        return False, details
    except OSError as error:
        logger.exception("could not run Box64 readiness check")
        return False, str(error)
    output = result.stdout.strip()
    if result.returncode:
        details = output or f"glibc-runner exited with status {result.returncode}"
        logger.error("Box64 readiness check failed: %s", details)
        return False, details
    logger.info("Box64 readiness check passed: %s", output)
    return True, output


def ready():
    return _box64_check()[0]


def _ensure_gpu_server(pkg, on_status=None):
    if shutil.which("virgl_test_server_android") is not None:
        return
    try:
        status = "installing VirGL GPU runtime"
        logger.info("%s", status)
        if on_status is not None:
            on_status(status, 0)
        output = []
        process = subprocess.Popen([pkg, "install", "-y", "virglrenderer-android"],
                                   stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                   text=True, bufsize=1)
        for line in process.stdout:
            output.append(line)
            for match in _PACKAGE_PROGRESS.finditer(line):
                if on_status is not None:
                    on_status(status, min(100, int(match.group(1))))
        result = process.wait()
        if result:
            raise RuntimeError("\n".join(output)[-1000:].strip()
                               or f"pkg exited with status {result}")
        if on_status is not None:
            on_status(status, 100)
    except (OSError, RuntimeError) as error:
        logger.warning("VirGL setup failed; x86 Linux apps cannot use GPU rendering: %s", error)


def ensure_installed(on_status=None, electron_node=False):
    logger.info("checking whether the x86_64 runtime is installed")
    pkg = shutil.which("pkg")
    if pkg is None:
        raise RuntimeError("Termux pkg command is unavailable")
    if ready():
        _ensure_gpu_server(pkg, on_status)
        if electron_node:
            ensure_node_runtime(on_status)
        return
    logger.warning("installing missing x86_64 runtime packages")

    def run(command, status):
        output = []
        logger.info("%s", status)
        if on_status is not None:
            on_status(status, 0)
        process = subprocess.Popen(command, stdout=subprocess.PIPE,
                                   stderr=subprocess.STDOUT, text=True,
                                   bufsize=1)
        for line in process.stdout:
            output.append(line)
            for match in _PACKAGE_PROGRESS.finditer(line):
                progress = min(100, int(match.group(1)))
                if on_status is not None:
                    on_status(status, progress)
        result = process.wait()
        if result:
            details = "".join(output)[-1000:].strip()
            raise RuntimeError(f"{status} failed ({result}): {details}")
        if on_status is not None:
            on_status(status, 100)

    def install(package):
        run([pkg, "install", "-y", package], f"installing {package}")

    install("glibc-repo")
    run([pkg, "update"], "updating package lists")
    for package in ("glibc", "glibc-runner", "bash-glibc", "box64-glibc", "libwayland-glibc",
                    "libwayland-protocols-glibc", "libxkbcommon-glibc"):
        install(package)
    passed, details = _box64_check()
    if not passed:
        raise RuntimeError(f"x86_64 Wayland runtime installation failed its Box64 check: {details}")
    _ensure_gpu_server(pkg, on_status)
    if electron_node:
        ensure_node_runtime(on_status)
