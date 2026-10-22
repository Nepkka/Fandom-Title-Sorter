from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum


class Source(StrEnum):
    DANBOORU = "danbooru"
    GELBOORU = "gelbooru"
    YANDERE = "yandere"
    KONACHAN = "konachan"
    ZEROCHAN = "zerochan"
    ESHUUSHUU = "eshuushuu"
    ANIME_PICTURES = "anime_pictures"
    PIXIV = "pixiv"
    TRACEMOE = "tracemoe"
    IQDB = "iqdb"
    SAUCENAO = "saucenao"
    FILENAME = "filename"


class Status(StrEnum):
    OK = "ok"
    NO_MATCH = "no_match"
    PARSE_ERROR = "parse_error"


class TargetKind(StrEnum):
    FANDOM = "fandom"
    ORIGINAL = "original"
    UNKNOWN = "unknown"


class Action(StrEnum):
    MOVE = "move"
    REVIEW = "review"


ORIGINAL_FOLDER = "Original"
UNKNOWN_FOLDER = "Unknown"
MULTI_CHAR_FOLDER = "MultiChar"

ORIGINAL_TAG = "original"


@dataclass(frozen=True, slots=True)
class ImageMeta:
    path: str
    size: int = 0
    width: int = 0
    height: int = 0
    mtime: float = 0.0
    md5: str | None = None
    phash: int | None = None
    ext: str = ""


@dataclass(frozen=True, slots=True)
class SearchResult:
    source: Source
    status: Status = Status.OK
    post_id: str | None = None
    post_url: str | None = None
    fandom_tags: tuple[str, ...] = ()
    artist_tags: tuple[str, ...] = ()
    character_tags: tuple[str, ...] = ()
    similarity: float = 0.0
    image_url: str | None = None
    error: str | None = None
    raw_tags: tuple[str, ...] = ()

    @property
    def evidence_key(self) -> str:
        if self.post_url:
            return self.post_url.rstrip("/").lower()
        if self.post_id:
            return f"{self.source.value}:{self.post_id}"
        return f"{self.source.value}:{id(self)}"


@dataclass(frozen=True, slots=True)
class Fandom:
    name: str
    aliases: tuple[str, ...] = ()
    priority: int = 0


@dataclass(frozen=True, slots=True)
class Match:
    fandom: Fandom
    matched: str


@dataclass(frozen=True, slots=True)
class Resolution:
    matches: tuple[Match, ...] = ()

    @property
    def best(self) -> Match | None:
        return self.matches[0] if self.matches else None


@dataclass(frozen=True, slots=True)
class Alternative:
    fandom: str
    evidence: int
    best_similarity: float


@dataclass(frozen=True, slots=True)
class Decision:
    kind: TargetKind
    action: Action = Action.MOVE
    fandom: str | None = None
    character: str | None = None
    artist: str | None = None
    multi_character: bool = False
    confidence: float = 0.0
    reason: str = ""
    alternatives: tuple[Alternative, ...] = field(default=())
    manual: bool = False
