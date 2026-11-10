import errno
import fcntl
import os
import pty
import queue
import signal
import struct
import subprocess
import termios
from threading import Event, Lock, Thread, current_thread


class CommandSession:
    def __init__(self, argv, *, cwd=None, env=None, columns=80, rows=24,
                 on_output=None, on_event=None, on_exit=None):
        if not argv or isinstance(argv, (str, bytes)):
            raise ValueError("argv must be a non-empty argument sequence")

        child_env = os.environ.copy()
        if env is not None:
            child_env.update(env)

        master_fd, slave_fd = pty.openpty()
        try:
            self.process = subprocess.Popen(
                argv,
                cwd=cwd,
                env=child_env,
                stdin=slave_fd,
                stdout=slave_fd,
                stderr=slave_fd,
                close_fds=True,
                start_new_session=True,
            )
        except Exception:
            os.close(master_fd)
            os.close(slave_fd)
            raise
        os.close(slave_fd)

        self.on_output = on_output
        self.on_event = on_event
        self.on_exit = on_exit
        self.output = queue.Queue(maxsize=64)
        self.error = None
        self.exit_code = None
        self._fd = master_fd
        self._closed = Event()
        self._write_lock = Lock()
        self.resize(columns, rows)
        self._reader = Thread(target=self._read_output, daemon=True)
        self._reader.start()

    def write(self, data):
        if self._closed.is_set() or self.process.poll() is not None:
            return False
        if isinstance(data, str):
            data = data.encode()
        with self._write_lock:
            offset = 0
            while offset < len(data):
                offset += os.write(self._fd, data[offset:])
        return True

    def resize(self, columns, rows):
        if self._closed.is_set():
            return
        columns = max(1, min(65535, int(columns)))
        rows = max(1, min(65535, int(rows)))
        fcntl.ioctl(self._fd, termios.TIOCSWINSZ,
                    struct.pack("HHHH", rows, columns, 0, 0))
        if self.process.poll() is None:
            try:
                os.killpg(self.process.pid, signal.SIGWINCH)
            except ProcessLookupError:
                pass

    def read_output(self):
        chunks = []
        while True:
            try:
                chunks.append(self.output.get_nowait())
            except queue.Empty:
                return b"".join(chunks)

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
            self.exit_code = self.process.wait()
            try:
                os.close(self._fd)
            except OSError:
                pass
            if not self._closed.is_set() and self.on_exit is not None:
                self.on_exit(self.exit_code)

    def _terminate(self):
        if self.process.poll() is not None:
            return
        try:
            os.killpg(self.process.pid, signal.SIGTERM)
        except ProcessLookupError:
            return

    def close(self):
        if self._closed.is_set():
            return
        self._closed.set()
        self._terminate()
        try:
            self.process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(self.process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            self.process.wait()
        if self._reader is not current_thread():
            self._reader.join(timeout=1)
