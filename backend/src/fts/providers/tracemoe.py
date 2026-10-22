from __future__ import annotations

import asyncio
import json
import re

from ..domain.models import Source
from ..net.client import Fetcher, fetch_routed
from ..net.ratelimit import AdaptiveLimiter, BudgetExhausted, SearchUnavailable
from .base import DiscoveryHit

TRACEMOE_URL = "https://api.trace.moe/search"
TRACEMOE_HOST = "api.trace.moe"

TRACEMOE_CACHE_TTL = 30 * 24 * 3600.0

TRACEMOE_MAX_TITLES = 6
TRACEMOE_MARGIN = 0.05

_PAREN = re.compile(r"\s*\([^)]*\)\s*$")
_SEASON = re.compile(
    r"\s*[:—\-]?\s*(?:season\s+\d+|part\s+\d+|cour\s+\d+|\d+(?:st|nd|rd|th)\s+season"
    r"|final\s+season)\s*$",
    re.IGNORECASE,
)
_ROMAN = re.compile(r"\s+(?:i{1,3}|iv|v|vi{1,3}|ix|x)\s*$", re.IGNORECASE)
_TRAILING_NUMBER = re.compile(r"\s+\d{1,2}\s*$")
_NON_LATIN = re.compile(
    r"[\u0400-\u04ff\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uac00-\ud7af\u0e00-\u0eff]"
)


def title_variants(title: str) -> tuple[str, ...]:
    current = " ".join(title.split())
    variants: list[str] = []
    for _ in range(3):
        if not current or current in variants:
            break
        variants.append(current)
        trimmed = _PAREN.sub("", current) or current
        for pattern in (_SEASON, _ROMAN, _TRAILING_NUMBER):
            trimmed = pattern.sub("", trimmed) or trimmed
        if trimmed == current:
            break
        current = trimmed
    return tuple(variants)


def _latin_title(value: str | None) -> bool:
    return bool(value) and not _NON_LATIN.search(value or "")


def titles_of(anilist: dict) -> tuple[str, ...]:
    titles = anilist.get("title") or {}
    candidates = [titles.get("english"), titles.get("romaji")]
    found: list[str] = []
    for candidate in candidates:
        if not _latin_title(candidate):
            continue
        for variant in title_variants(candidate or ""):
            if variant not in found:
                found.append(variant)
    return tuple(found[:TRACEMOE_MAX_TITLES])


class TraceMoeEngine:
    name = Source.TRACEMOE
    host = TRACEMOE_HOST

    def __init__(
        self,
        fetcher: Fetcher,
        limiter: AdaptiveLimiter | None = None,
        min_similarity: float = 0.75,
    ) -> None:
        self._fetcher = fetcher
        self._limiter = limiter
        self._min_similarity = min_similarity

    async def search(self, image: bytes, *, phash: int | None = None) -> list[DiscoveryHit]:
        routes = self._fetcher.routes_for(TRACEMOE_HOST)
        while True:
            response = await fetch_routed(
                self._fetcher,
                self._limiter,
                TRACEMOE_HOST,
                "POST",
                TRACEMOE_URL,
                params={"anilistInfo": "", "cutBorders": ""},
                files={"image": ("image.jpg", image, "image/jpeg")},
                cache_key=f"tracemoe:{phash}" if phash is not None else None,
                cache_ttl=TRACEMOE_CACHE_TTL,
                batch=1,
                routes=routes,
            )

            if self._limiter is None or response.from_cache:
                break
            if response.status == 429:
                self._limiter.exhaust_route(response.proxy)
                routes = [route for route in routes if route != response.proxy]
                if not routes:
                    break
                if not self._limiter.any_budget_left(routes):
                    raise BudgetExhausted("trace.moe: суточная квота исчерпана на всех IP")
                continue
            if not response.ok:
                self._limiter.note_failure(response.proxy)
                break
            self._limiter.note_success(response.proxy)
            break

        if not response.ok:
            raise SearchUnavailable(f"trace.moe не ответил: статус {response.status}")
        return await asyncio.to_thread(self.parse, response.text())

    def parse(self, text: str) -> list[DiscoveryHit]:
        try:
            payload = json.loads(text)
        except ValueError:
            return []
        if not isinstance(payload, dict):
            return []

        best: dict[int, tuple[float, dict]] = {}
        for item in payload.get("result") or ():
            anilist = item.get("anilist") or {}
            anime_id = anilist.get("id")
            if not anime_id:
                continue
            similarity = float(item.get("similarity") or 0.0)
            current = best.get(int(anime_id))
            if current is None or similarity > current[0]:
                best[int(anime_id)] = (similarity, item)

        ranked = sorted(best.items(), key=lambda pair: (-pair[1][0], pair[0]))
        if not ranked:
            return []
        anime_id, (similarity, item) = ranked[0]
        if similarity < self._min_similarity:
            return []
        if len(ranked) > 1 and similarity - ranked[1][1][0] < TRACEMOE_MARGIN:
            return []
        anilist = item.get("anilist") or {}
        return [
            DiscoveryHit(
                engine=Source.TRACEMOE,
                similarity=round(similarity, 3),
                post_id=str(anime_id),
                url=f"https://anilist.co/anime/{anime_id}",
                fandom_tags=titles_of(anilist),
            )
        ]
