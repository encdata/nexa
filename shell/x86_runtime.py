import os
import logging
from pathlib import Path
import re
import shlex
import shutil
import subprocess

_PACKAGE_PROGRESS = re.compile(r"\[\s*(\d{1,3})%\]")
logger = logging.getLogger("nexa.x86")

def box64_command():
    runtime = Path.home() / ".local" / "share" / "nexa" / "runtime" / "box64"
    if runtime.is_file() and os.access(runtime, os.X_OK):
        return str(runtime)
    return "box64"


def runner_shell():
    prefix = Path(os.environ.get("PREFIX", "/data/data/com.termux/files/usr"))
    shell = os.environ.get("SHELL")
    if (shell and os.access(shell, os.X_OK)
            and Path(shell).parent != prefix / "glibc" / "bin"):
        return shell
    return shutil.which("bash") or shutil.which("sh")


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


def ensure_installed(on_status=None):
    logger.info("checking whether the x86_64 runtime is installed")
    pkg = shutil.which("pkg")
    if pkg is None:
        raise RuntimeError("Termux pkg command is unavailable")
    if ready():
        _ensure_gpu_server(pkg, on_status)
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
