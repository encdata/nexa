import os
from pathlib import Path
import shutil
import subprocess


def installed():
    prefix = os.environ.get("PREFIX", "/data/data/com.termux/files/usr")
    glibc = Path(prefix) / "glibc"
    return (shutil.which("grun") is not None
            and (glibc / "bin" / "box64").is_file()
            and (glibc / "lib" / "libwayland-client.so.0").is_file())


def ensure_installed():
    if installed():
        return
    pkg = shutil.which("pkg")
    if pkg is None:
        raise RuntimeError("Termux pkg command is unavailable")
    subprocess.run([pkg, "install", "-y", "glibc-repo"], check=True)
    subprocess.run([pkg, "update"], check=True)
    subprocess.run([pkg, "install", "-y", "glibc-runner", "box64-glibc",
                    "libwayland-glibc", "libwayland-protocols-glibc",
                    "libxkbcommon-glibc"], check=True)
    if not installed():
        raise RuntimeError("x86_64 Wayland runtime installation did not complete")
