"""Short, cross-process transactions and atomic private configuration files."""
import json
import os
import stat
import sys
import tempfile
import threading
import time
from contextlib import contextmanager
from pathlib import Path


class ConfigError(OSError):
    pass


class ConfigChangedError(ConfigError):
    def __init__(self):
        super().__init__("The saved account or configuration changed. Start this command again.")


WINDOWS = sys.platform == "win32"
_thread_lock = threading.RLock()


def _lock(fd, *, release=False):
    if WINDOWS:
        import msvcrt
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK if release else msvcrt.LK_NBLCK, 1)
    else:
        import fcntl
        fcntl.flock(fd, fcntl.LOCK_UN if release else fcntl.LOCK_EX | fcntl.LOCK_NB)


@contextmanager
def locked_config(path: Path, timeout=5.0):
    # Keep the lock inode after release; unlinking it lets waiters lock different
    # files. No API/provider work may run inside this context.
    with _thread_lock:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        lock_path = path.with_name(path.name + ".lock")
        if lock_path.is_symlink():
            raise ConfigError("The configuration lock is unavailable.")
        fd = os.open(lock_path, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
        acquired = False
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise ConfigError("The configuration lock is unavailable.")
            if WINDOWS and os.fstat(fd).st_size == 0:
                os.write(fd, b"0")
            deadline = time.monotonic() + timeout
            while not acquired:
                try:
                    _lock(fd)
                    acquired = True
                except OSError:
                    if time.monotonic() >= deadline:
                        raise ConfigError("The configuration is busy. Retry this command.") from None
                    time.sleep(.01)
            yield
        finally:
            try:
                if acquired:
                    _lock(fd, release=True)
            finally:
                os.close(fd)


def atomic_write(path: Path, payload):
    """Caller holds locked_config. Replace only a complete, synced private file."""
    temporary = None
    try:
        fd, temporary = tempfile.mkstemp(prefix="." + path.name + ".", suffix=".tmp", dir=path.parent)
        with os.fdopen(fd, "wb") as stream:
            stream.write((json.dumps(payload, indent=2, sort_keys=True) + "\n").encode())
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        if not WINDOWS:
            directory = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
    except OSError:
        raise ConfigError("Unable to save the configuration. Retry before continuing.") from None
    finally:
        if temporary is not None:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass
