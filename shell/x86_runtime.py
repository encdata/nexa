import os
from pathlib import Path
import re
import shutil
import subprocess

_PACKAGE_PROGRESS = re.compile(r"\[\s*(\d{1,3})%\]")

def installed():
    prefix = os.environ.get("PREFIX", "/data/data/com.termux/files/usr")
    glibc = Path(prefix) / "glibc"
    return (shutil.which("grun") is not None
            and (glibc / "bin" / "box64").is_file()
            and (glibc / "lib" / "libwayland-client.so.0").is_file())


def _box64_check():
    if not installed():
        return False, "runtime packages or Wayland libraries are missing"
    runner = shutil.which("glibc-runner")
    if runner is None:
        return False, "glibc-runner is missing"
    try:
        result = subprocess.run([runner, "--shell", "box64 --version"],
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                text=True, timeout=60)
    except subprocess.TimeoutExpired as error:
        output = error.stdout or ""
        if isinstance(output, bytes):
            output = output.decode("utf-8", "replace")
        return False, f"Box64 check timed out after 60 seconds: {output.strip()}"
    except OSError as error:
        return False, str(error)
    output = result.stdout.strip()
    if result.returncode:
        return False, output or f"glibc-runner exited with status {result.returncode}"
    return True, output


def ready():
    return _box64_check()[0]


def ensure_installed(on_status=None):
    if ready():
        return
    pkg = shutil.which("pkg")
    if pkg is None:
        raise RuntimeError("Termux pkg command is unavailable")

    def run(command, status):
        output = []
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
    for package in ("glibc", "glibc-runner", "box64-glibc", "libwayland-glibc",
                    "libwayland-protocols-glibc", "libxkbcommon-glibc"):
        install(package)
    passed, details = _box64_check()
    if not passed:
        raise RuntimeError(f"x86_64 Wayland runtime installation failed its Box64 check: {details}")
