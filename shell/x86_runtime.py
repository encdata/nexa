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


def ensure_installed(on_status=None):
    if installed():
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
    for package in ("glibc-runner", "box64-glibc", "libwayland-glibc",
                    "libwayland-protocols-glibc", "libxkbcommon-glibc"):
        install(package)
    if not installed():
        raise RuntimeError("x86_64 Wayland runtime installation did not complete")
