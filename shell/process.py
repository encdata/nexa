import errno
import fcntl
import os
import pty
import queue
import signal
import struct
import termios
from threading import Event, Lock, Thread, current_thread


class CommandSession:
    def __init__(self, argv, *, cwd=None, env=None, columns=80, rows=24,
                 on_output=None, on_event=None, on_exit=None):
        if not argv or isinstance(argv, (str, bytes)):
            raise ValueError("argv must be a non-empty argument sequence")

        child_env = os.environ.copy()
        if env is not None:
            for key, value in env.items():
                if value is None:
                    child_env.pop(key, None)
                else:
                    child_env[key] = value

        pid, master_fd = pty.fork()
        if pid == 0:
            try:
                if cwd is not None:
                    os.chdir(cwd)
                os.execvpe(argv[0], argv, child_env)
            except BaseException as error:
                try:
                    os.write(2, f"nexa terminal: {error}\n".encode())
                finally:
                    os._exit(127)

        self.on_output = on_output
        self.on_event = on_event
        self.on_exit = on_exit
        self.output = queue.Queue(maxsize=64)
        self.error = None
        self.exit_code = None
        self.pid = pid
        self._fd = master_fd
        self._closed = Event()
        self._write_lock = Lock()
        self.resize(columns, rows)
        self._reader = Thread(target=self._read_output, daemon=True)
        self._reader.start()

    def write(self, data):
        if self._closed.is_set() or self.exit_code is not None:
            return False
        if isinstance(data, str):
            data = data.encode()
        with self._write_lock:
            offset = 0
            try:
                while offset < len(data):
                    offset += os.write(self._fd, data[offset:])
            except OSError:
                return False
        return True

    def resize(self, columns, rows):
        if self._closed.is_set():
            return
        columns = max(1, min(65535, int(columns)))
        rows = max(1, min(65535, int(rows)))
        fcntl.ioctl(self._fd, termios.TIOCSWINSZ,
                    struct.pack("HHHH", rows, columns, 0, 0))
        if self.exit_code is None:
            try:
                os.killpg(self.pid, signal.SIGWINCH)
            except ProcessLookupError:
                pass

    def read_output(self):
        chunks = bytearray()
        while True:
            try:
                chunks.extend(self.output.get_nowait())
            except queue.Empty:
                return bytes(chunks)

    def handle_event(self, event):
        return self.on_event(event) if self.on_event is not None else False

    def _read_output(self):
        try:
            while not self._closed.is_set():
                try:
                    data = os.read(self._fd, 4096)
                except OSError as error:
                    if error.errno == errno.EIO or self._closed.is_set():
                        break
                    raise
                if not data:
                    break
                if self.on_output is None:
                    while not self._closed.is_set():
                        try:
                            self.output.put(data, timeout=0.1)
                            break
                        except queue.Full:
                            continue
                else:
                    self.on_output(data)
        except Exception as error:
            self.error = error
            self._terminate()
        finally:
            try:
                _, status = os.waitpid(self.pid, 0)
                self.exit_code = os.waitstatus_to_exitcode(status)
            except ChildProcessError:
                pass
            try:
                os.close(self._fd)
            except OSError:
                pass
            if not self._closed.is_set() and self.on_exit is not None:
                self.on_exit(self.exit_code)

    def _terminate(self):
        if self.exit_code is not None:
            return
        try:
            os.killpg(self.pid, signal.SIGTERM)
        except ProcessLookupError:
            return

    def close(self):
        if self._closed.is_set():
            return
        self._closed.set()
        self._terminate()
        if self._reader is current_thread():
            return
        self._reader.join(timeout=1)
        if self._reader.is_alive():
            try:
                os.killpg(self.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            self._reader.join()
