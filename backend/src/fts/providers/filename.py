from __future__ import annotations

import os
import re

from ..domain.models import ORIGINAL_TAG, Source
from ..domain.naming import normalize_key
from ..domain.rules import FandomMap
from .base import DiscoveryHit

MD5_IN_NAME = re.compile(r"(?<![0-9a-f])([0-9a-f]{32})(?![0-9a-f])", re.IGNORECASE)

PIXIV_ARTWORK = "https://www.pixiv.net/artworks/{}"

PAGE_PATTERNS: tuple[tuple[Source, re.Pattern[str], str], ...] = (
    (
        Source.ANIME_PICTURES,
        re.compile(r"ANIME-PICTURES\.NET_-_(\d+)", re.IGNORECASE),
        "https://anime-pictures.net/posts/{}",
    ),
    (
        Source.PIXIV,
        re.compile(r"(?<!\d)(\d{5,9})_p\d{1,3}(?!\d)"),
        PIXIV_ARTWORK,
    ),
    (
        Source.PIXIV,
        re.compile(r"illust_(\d{5,9})", re.IGNORECASE),
        PIXIV_ARTWORK,
    ),
)

DRAWN_BY = "_drawn_by_"
TAGS_SEPARATOR = "_and_"
TRUNCATED_TAG = re.compile(r"^\d+_more$")
TRUNCATED_PREFIX = re.compile(r"^\d+_more_")
POST_ID_PREFIX = re.compile(r"^\d+_{2,}")
POST_ID_SUFFIX = re.compile(r"__\d+$")
NAME_TAIL = re.compile(
    r"__(?:sample|preview|large|original|full|thumb)?-?([0-9a-f]{32})$", re.IGNORECASE
)


def md5_from_filename(name: str) -> str | None:
    found = MD5_IN_NAME.search(name)
    return found.group(1).lower() if found else None


def _ordered(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        key = normalize_key(value)
        if key in seen:
            continue
        seen.add(key)
        result.append(value)
    return result


def _split_source(tag: str, fandom_map: FandomMap) -> tuple[str, str] | None:
    if fandom_map.resolve(tag).best is not None:
        return None
    words = tag.replace("_", " ").split()
    for cut in range(1, len(words)):
        head = " ".join(words[:cut])
        tail = " ".join(words[cut:])
        if normalize_key(tail) == ORIGINAL_TAG or fandom_map.resolve(tail).best:
            return head, tail
    return None


def _booru_parts(name: str, fandom_map: FandomMap) -> tuple[list[str], list[str], str]:
    stem = NAME_TAIL.sub("", os.path.splitext(name)[0])
    stem = POST_ID_PREFIX.sub("", stem).lstrip("_")
    if DRAWN_BY not in stem:
        return [], [], ""
    tag_blob, artist = stem.rsplit(DRAWN_BY, 1)
    artist = POST_ID_SUFFIX.sub("", artist.strip("_"))
    pieces: list[str] = []
    for piece in tag_blob.split(TAGS_SEPARATOR):
        piece = TRUNCATED_PREFIX.sub("", piece.strip("_"))
        if piece and not TRUNCATED_TAG.match(piece):
            pieces.append(piece)

    fandoms: list[str] = []
    characters: list[str] = []
    unresolved: list[str] = []
    for piece in pieces:
        split = _split_source(piece, fandom_map)
        if split is not None:
            characters.append(split[0])
            fandoms.append(split[1])
        elif normalize_key(piece) == ORIGINAL_TAG or fandom_map.resolve(piece).matches:
            fandoms.append(piece)
        else:
            unresolved.append(piece)

    if fandoms:
        characters.extend(unresolved)
    else:
        fandoms.extend(unresolved)

    return _ordered(fandoms), _ordered(characters), artist.strip("_")


def resolve_from_filename(path: str, fandom_map: FandomMap | None = None) -> list[DiscoveryHit]:
    name = os.path.basename(path)
    hits: list[DiscoveryHit] = []

    md5 = md5_from_filename(name)
    if md5:
        hits.append(
            DiscoveryHit(
                engine=Source.FILENAME,
                similarity=1.0,
                md5=md5,
            )
        )

    for source, pattern, template in PAGE_PATTERNS:
        found = pattern.search(name)
        if found:
            hits.append(
                DiscoveryHit(
                    engine=Source.FILENAME,
                    similarity=1.0,
                    source=source,
                    post_id=found.group(1),
                    url=template.format(found.group(1)),
                )
            )

    if fandom_map is not None:
        fandoms, characters, artist = _booru_parts(name, fandom_map)
        if fandoms or artist:
            hits.append(
                DiscoveryHit(
                    engine=Source.FILENAME,
                    similarity=1.0,
                    fandom_tags=tuple(fandoms),
                    character_tags=tuple(characters),
                    artist_tags=(artist,) if artist else (),
                )
            )

    return hits
