from __future__ import annotations

import os
import re
import unicodedata
from collections.abc import Callable

from .models import (
    MULTI_CHAR_FOLDER,
    ORIGINAL_FOLDER,
    UNKNOWN_FOLDER,
    Decision,
    TargetKind,
)

FORBIDDEN_IN_NAME = '\\/:*?"<>|'

RESERVED_NAMES = frozenset(
    ["con", "prn", "aux", "nul"]
    + [f"com{i}" for i in range(1, 10)]
    + [f"lpt{i}" for i in range(1, 10)]
)

_SEPARATORS = re.compile(r"[\s_\-/:]+")
_WHITESPACE = re.compile(r"\s+")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


def normalize_key(value: str) -> str:
    text = unicodedata.normalize("NFKC", value or "").lower()
    text = _SEPARATORS.sub("", text)
    return text.rstrip("!.")


def sanitize_component(value: str) -> str:
    text = unicodedata.normalize("NFKC", value or "")
    text = _CONTROL.sub("", text)
    text = "".join(" " if ch in FORBIDDEN_IN_NAME else ch for ch in text)
    text = _WHITESPACE.sub(" ", text).strip()
    text = text.rstrip(". ")
    return text


def safe_component(value: str, fallback: str = "Unnamed") -> str:
    text = sanitize_component(value)
    if not text:
        return fallback
    stem = text.split(".")[0].lower()
    if stem in RESERVED_NAMES:
        text = f"_{text}"
    return text


def display_character(tag: str) -> str:
    base = tag.split("(")[0]
    return _WHITESPACE.sub(" ", base.replace("_", " ")).strip().title()


def display_artist(tag: str) -> str:
    return _WHITESPACE.sub(" ", tag.replace("_", " ")).strip()


def uniquify(path: str, exists: Callable[[str], bool]) -> str:
    if not exists(path):
        return path
    base, ext = os.path.splitext(path)
    counter = 1
    while True:
        candidate = f"{base} ({counter}){ext}"
        if not exists(candidate):
            return candidate
        counter += 1


def target_directory(root: str, decision: Decision, sep_characters: bool = True) -> str:
    if decision.kind is TargetKind.UNKNOWN:
        return os.path.join(root, UNKNOWN_FOLDER)

    if not sep_characters:
        head = (
            ORIGINAL_FOLDER
            if decision.kind is TargetKind.ORIGINAL
            else (decision.fandom or UNKNOWN_FOLDER)
        )
        return os.path.join(root, safe_component(head, UNKNOWN_FOLDER))

    if decision.kind is TargetKind.ORIGINAL:
        if not decision.artist:
            return os.path.join(root, UNKNOWN_FOLDER)
        return os.path.join(root, ORIGINAL_FOLDER, safe_component(decision.artist))

    parts = [safe_component(decision.fandom or UNKNOWN_FOLDER)]
    if decision.multi_character:
        parts.append(MULTI_CHAR_FOLDER)
    elif decision.character:
        parts.append(safe_component(decision.character))
    return os.path.join(root, *parts)


def target_for(
    root: str,
    decision: Decision,
    filename: str,
    exists: Callable[[str], bool],
    sep_characters: bool = True,
) -> str:
    directory = target_directory(root, decision, sep_characters)
    candidate = os.path.join(directory, os.path.basename(filename))
    return uniquify(candidate, exists)
