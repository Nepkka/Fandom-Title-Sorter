from __future__ import annotations

import hashlib
import os
import shutil
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import suppress
from dataclasses import dataclass

from PIL import Image, UnidentifiedImageError

from ..domain.models import Decision
from ..domain.naming import target_for

LONG_THRESHOLD = 240

CHUNK = 1 << 20

PARTIAL_SUFFIXES = frozenset(
    {".crdownload", ".part", ".partial", ".download", ".tmp", ".aria2"}
)

IMAGE_SUFFIXES = frozenset(
    {
        ".jpg",
        ".jpeg",
        ".jfif",
        ".png",
        ".apng",
        ".webp",
        ".gif",
        ".bmp",
        ".tif",
        ".tiff",
        ".avif",
        ".heic",
        ".heif",
        ".jxl",
        ".svg",
        ".ico",
        ".psd",
    }
)


def long_path(path: str | os.PathLike[str]) -> str:
    text = os.fspath(path)
    if os.name != "nt":
        return text
    absolute = os.path.abspath(text)
    if len(absolute) >= LONG_THRESHOLD and not absolute.startswith("\\\\?\\"):
        return "\\\\?\\" + absolute
    return absolute


def exists(path: str | os.PathLike[str]) -> bool:
    return os.path.exists(long_path(path))


def size_of(path: str | os.PathLike[str]) -> int:
    return os.path.getsize(long_path(path))


def image_size(path: str | os.PathLike[str]) -> tuple[int, int]:
    try:
        with Image.open(long_path(path)) as image:
            return image.size
    except OSError:
        return (0, 0)


def ensure_dir(path: str | os.PathLike[str]) -> None:
    os.makedirs(long_path(path), exist_ok=True)


def _is_truncated(path: str, size: int) -> bool:
    if size < 16:
        return True
    try:
        with open(long_path(path), "rb") as handle:
            head = handle.read(16)
            handle.seek(-64, 2)
            tail = handle.read(64)
    except OSError:
        return False
    if head[:2] == b"\xff\xd8":
        return b"\xff\xd9" not in tail
    if head[:8] == b"\x89PNG\r\n\x1a\n":
        return b"IEND" not in tail
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return int.from_bytes(head[4:8], "little") + 8 > size
    return False


def is_junk_file(path: str | os.PathLike[str]) -> bool:
    text = os.fspath(path)
    suffix = os.path.splitext(text)[1].lower()
    if suffix in PARTIAL_SUFFIXES:
        return True
    try:
        size = os.path.getsize(long_path(path))
    except OSError:
        return False
    if size == 0:
        return True
    try:
        with Image.open(long_path(path)) as image:
            image.verify()
    except UnidentifiedImageError:
        return suffix not in IMAGE_SUFFIXES
    except OSError:
        return _is_truncated(text, size)
    except Exception:
        return False
    return _is_truncated(text, size)


def remove_file(path: str | os.PathLike[str]) -> bool:
    try:
        os.remove(long_path(path))
        return True
    except OSError:
        return False


def hash_file(path: str | os.PathLike[str], algorithm: str = "md5") -> str:
    digest = hashlib.new(algorithm)
    with open(long_path(path), "rb") as handle:
        while chunk := handle.read(CHUNK):
            digest.update(chunk)
    return digest.hexdigest()


def perceptually_hash(path: str | os.PathLike[str], size: int = 8) -> int:
    with Image.open(long_path(path)) as image:
        gray = image.convert("L").resize((size + 1, size), Image.Resampling.LANCZOS)
        pixels = gray.tobytes()
    bits = 0
    for row in range(size):
        offset = row * (size + 1)
        for col in range(size):
            bits <<= 1
            if pixels[offset + col] > pixels[offset + col + 1]:
                bits |= 1
    return bits


def hamming(left: int, right: int) -> int:
    return (left ^ right).bit_count()


def iter_files(root: str | os.PathLike[str]) -> Iterator[str]:
    for dirpath, _dirnames, filenames in os.walk(long_path(root)):
        for name in filenames:
            yield os.path.join(dirpath, name)


@dataclass(frozen=True, slots=True)
class MovePlan:
    source: str
    target: str
    size: int


def plan_move(
    output_root: str,
    decision: Decision,
    source_path: str,
    sep_characters: bool = True,
    exists_fn: Callable[[str], bool] | None = None,
) -> MovePlan:
    check = exists_fn or exists
    target = target_for(output_root, decision, source_path, check, sep_characters)
    directory = os.path.dirname(target)
    if not check(directory):
        ensure_dir(directory)
    return MovePlan(source=source_path, target=target, size=size_of(source_path))


def apply_move(plan: MovePlan) -> MovePlan:
    ensure_dir(os.path.dirname(plan.target))
    target = _move_without_overwrite(plan.source, plan.target)
    moved = size_of(target)
    if moved != plan.size:
        raise OSError(
            f"размер после перемещения не совпал: было {plan.size}, стало {moved} ({target})"
        )
    return MovePlan(source=plan.source, target=target, size=moved)


def _move_without_overwrite(source: str, target: str) -> str:
    base, ext = os.path.splitext(target)
    counter = 0
    while True:
        try:
            os.rename(long_path(source), long_path(target))
            return target
        except FileExistsError:
            counter += 1
            target = f"{base} ({counter}){ext}"
        except OSError:
            shutil.move(long_path(source), long_path(target))
            return target


_MOVE_LOCK = threading.Lock()


def plan_and_move(
    output_root: str,
    decision: Decision,
    source_path: str,
    sep_characters: bool = True,
    exists_fn: Callable[[str], bool] | None = None,
    on_planned: Callable[[MovePlan], None] | None = None,
) -> MovePlan:
    with _MOVE_LOCK:
        plan = plan_move(output_root, decision, source_path, sep_characters, exists_fn)
        if on_planned is not None:
            on_planned(plan)
        return apply_move(plan)


def merge_folder(
    source_root: str,
    target_root: str,
    on_planned: Callable[[MovePlan], None] | None = None,
) -> list[MovePlan]:
    root = long_path(source_root)
    if not os.path.isdir(root):
        return []
    plans: list[MovePlan] = []
    with _MOVE_LOCK:
        for path in iter_files(root):
            relative = os.path.relpath(path, root)
            planned = MovePlan(
                source=path,
                target=os.path.join(target_root, relative),
                size=size_of(path),
            )
            if on_planned is not None:
                on_planned(planned)
            plans.append(apply_move(planned))
    for dirpath, _dirnames, _filenames in os.walk(root, topdown=False):
        with suppress(OSError):
            os.rmdir(long_path(dirpath))
    return plans


def prune_thumbs(
    directory: str | os.PathLike[str],
    *,
    max_age_seconds: float,
    max_bytes: int,
    now: float | None = None,
) -> tuple[int, int]:
    root = long_path(directory)
    if not os.path.isdir(root):
        return (0, 0)

    entries: list[tuple[float, int, str]] = []
    with os.scandir(root) as scan:
        for item in scan:
            try:
                if not item.is_file():
                    continue
                info = item.stat()
            except OSError:
                continue
            entries.append((info.st_mtime, info.st_size, item.path))

    cutoff = (time.time() if now is None else now) - max_age_seconds
    removed = 0
    freed = 0
    survivors: list[tuple[float, int, str]] = []
    for mtime, size, path in entries:
        if mtime < cutoff:
            if _unlink(path):
                removed += 1
                freed += size
        else:
            survivors.append((mtime, size, path))

    total = sum(size for _mtime, size, _path in survivors)
    if total > max_bytes:
        survivors.sort()
        for _mtime, size, path in survivors:
            if total <= max_bytes:
                break
            if _unlink(path):
                removed += 1
                freed += size
                total -= size
    return (removed, freed)


def _unlink(path: str) -> bool:
    try:
        os.remove(path)
        return True
    except OSError:
        return False
