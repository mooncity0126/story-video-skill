from __future__ import annotations

import hashlib
import json
import os
import re
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path


class HarnessError(Exception):
    """An actionable failure safe to show to the operator."""


class BudgetError(HarnessError):
    pass


class PauseRequested(HarnessError):
    pass


def now():
    return datetime.now(timezone.utc).isoformat()


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def file_hash(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(value, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    for attempt in range(6):
        try:
            os.replace(tmp, path)
            break
        except PermissionError:
            if attempt == 5:
                raise
            time.sleep(0.02 * 2**attempt)


def safe_error(exc):
    # Never serialize HTTP request/response bodies, URLs, headers or validation inputs.
    text = str(exc) if isinstance(exc, HarnessError) else type(exc).__name__
    text = re.sub(r"apikey_[A-Za-z0-9_-]+", "[REDACTED]", text)
    text = re.sub(r"sk-[A-Za-z0-9_.-]+", "[REDACTED]", text)
    text = re.sub(r"Bearer\s+\S+", "Bearer [REDACTED]", text, flags=re.I)
    return text[:1000]


def inside(root, relative):
    root = Path(root).resolve()
    result = (root / relative).resolve()
    if not result.is_relative_to(root):
        raise HarnessError("素材路径越出 run 目录")
    return result


@contextmanager
def run_lock(root):
    """OS-released advisory lock; a crashed process cannot leave a stale lock."""
    f = (Path(root) / "run.lock").open("a+b")
    try:
        f.seek(0)
        if f.read(1) == b"":
            f.write(b"0")
            f.flush()
        f.seek(0)
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        f.close()
        raise HarnessError("该 run 正由另一个进程使用") from None
    try:
        yield
    finally:
        if os.name == "nt":
            import msvcrt

            f.seek(0)
            msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(f, fcntl.LOCK_UN)
        f.close()
