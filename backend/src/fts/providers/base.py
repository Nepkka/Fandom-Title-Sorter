from __future__ import annotations

import re
from dataclasses import dataclass

from ..domain.models import Source

_SCHEME_RELATIVE = re.compile(r"^//")

_MD5_RE = re.compile(r"[0-9a-f]{32}", re.IGNORECASE)


def absolute_url(url: str) -> str:
    if not url:
        return url
    if _SCHEME_RELATIVE.match(url):
        return "https:" + url
    if url.startswith("http://"):
        return "https://" + url[len("http://") :]
    return url


def md5_from_url(url: str) -> str | None:
    match = _MD5_RE.search(url or "")
    return match.group(0).lower() if match else None


@dataclass(frozen=True, slots=True)
class DiscoveryHit:
    engine: Source
    similarity: float
    source: Source | None = None
    url: str | None = None
    md5: str | None = None
    post_id: str | None = None
    fandom_tags: tuple[str, ...] = ()
    artist_tags: tuple[str, ...] = ()
    character_tags: tuple[str, ...] = ()
    raw_tags: tuple[str, ...] = ()

    @property
    def has_tags(self) -> bool:
        return bool(self.fandom_tags or self.artist_tags or self.character_tags)
