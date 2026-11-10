import os
import shutil
import subprocess
from concurrent.futures import ThreadPoolExecutor
from queue import Empty, SimpleQueue


clipboard = {"path": None, "cut": False}
_worker = ThreadPoolExecutor(max_workers=1, thread_name_prefix="nexa-files")
_completed = SimpleQueue()


def run_async(operation, callback):
    future = _worker.submit(operation)

    def finish(future):
        try:
            result, error = future.result(), None
        except Exception as caught:
            result, error = None, caught
        _completed.put((callback, result, error))

    future.add_done_callback(finish)


def process_completions():
    while True:
        try:
            callback, result, error = _completed.get_nowait()
        except Empty:
            return
        callback(result, error)


def copy(path):
    if os.path.lexists(path):
        clipboard.update(path=os.path.abspath(path), cut=False)


def cut(path):
    if os.path.lexists(path):
        clipboard.update(path=os.path.abspath(path), cut=True)


def has_clipboard_item():
    return bool(clipboard["path"] and os.path.lexists(clipboard["path"]))


def _available_path(directory, name, is_copy):
    target = os.path.join(directory, name)
    if not os.path.lexists(target):
        return target
    stem, extension = os.path.splitext(name)
    label = "copy" if is_copy else "moved"
    index = 1
    while True:
        suffix = f" ({label})" if index == 1 else f" ({label} {index})"
        target = os.path.join(directory, f"{stem}{suffix}{extension}")
        if not os.path.lexists(target):
            return target
        index += 1


def paste(directory):
    return paste_item(directory, clipboard["path"], clipboard["cut"])


def paste_item(directory, source, is_cut):
    if not source or not os.path.lexists(source):
        if clipboard["path"] == source and clipboard["cut"] == is_cut:
            clipboard.update(path=None, cut=False)
        raise FileNotFoundError("clipboard item no longer exists")
    destination = os.path.realpath(directory)
    if not os.path.isdir(destination):
        raise NotADirectoryError(destination)
    source_real = os.path.realpath(source)
    if os.path.isdir(source_real) and os.path.commonpath((source_real, destination)) == source_real:
        raise ValueError("can't paste a folder inside itself")
    if is_cut and os.path.dirname(source_real) == destination:
        if clipboard["path"] == source and clipboard["cut"] == is_cut:
            clipboard.update(path=None, cut=False)
        return source_real

    target = _available_path(destination, os.path.basename(source), not is_cut)
    if is_cut:
        shutil.move(source, target)
        if clipboard["path"] == source and clipboard["cut"] == is_cut:
            clipboard.update(path=None, cut=False)
    elif os.path.islink(source):
        os.symlink(os.readlink(source), target)
    elif os.path.isdir(source):
        shutil.copytree(source, target, symlinks=True)
    else:
        shutil.copy2(source, target)
    return target


def delete(path):
    if os.path.islink(path) or not os.path.isdir(path):
        os.remove(path)
    else:
        shutil.rmtree(path)


def open_file(path):
    subprocess.Popen(["termux-open", path], close_fds=True)
