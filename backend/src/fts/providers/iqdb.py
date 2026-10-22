from __future__ import annotations

import asyncio
import re

from bs4 import BeautifulSoup

from ..domain.models import Source
from ..net.client import Fetched, Fetcher, fetch_routed
from ..net.ratelimit import AdaptiveLimiter, SearchUnavailable
from .base import DiscoveryHit, absolute_url, md5_from_url

IQDB_URL = "https://iqdb.org/"
IQDB_HOST = "iqdb.org"

SERVICES = (1, 2, 3, 4, 6, 11, 13)

IQDB_CACHE_TTL = 30 * 24 * 3600.0

SOURCE_NAMES: dict[str, Source] = {
    "danbooru": Source.DANBOORU,
    "gelbooru": Source.GELBOORU,
    "yande.re": Source.YANDERE,
    "yandere": Source.YANDERE,
    "konachan": Source.KONACHAN,
    "zerochan": Source.ZEROCHAN,
    "e-shuushuu": Source.ESHUUSHUU,
    "anime-pictures": Source.ANIME_PICTURES,
    "sankaku": Source.DANBOORU,
}

_SIMILARITY_RE = re.compile(r"(\d{1,3})\s*%")

_TAGS_MARK = "Tags:"

_ERROR_MARK = "Can't read query result"


def is_error_page(html: str) -> bool:
    return _ERROR_MARK in html


def preview_tags(preview) -> tuple[str, ...]:
    if preview is None:
        return ()
    for attribute in ("alt", "title"):
        value = preview.get(attribute) or ""
        if _TAGS_MARK not in value:
            continue
        raw = value.split(_TAGS_MARK, 1)[1].strip()
        if not raw:
            continue
        parts = raw.split(",") if "," in raw else raw.split()
        return tuple(part.strip() for part in parts if part.strip())
    return ()


class IqdbEngine:
    name = Source.IQDB
    host = IQDB_HOST

    def __init__(
        self,
        fetcher: Fetcher,
        limiter: AdaptiveLimiter | None = None,
        min_similarity: float = 0.8,
    ) -> None:
        self._fetcher = fetcher
        self._limiter = limiter
        self._min_similarity = min_similarity

    async def search(self, image: bytes, *, phash: int | None = None) -> list[DiscoveryHit]:
        cache_key = f"iqdb:{phash}" if phash is not None else None
        response = await self._request(image, cache_key)
        if is_error_page(response.text()):
            if response.from_cache:
                self._fetcher.drop_cache(cache_key)
                response = await self._request(image, cache_key)
            if is_error_page(response.text()):
                self._fetcher.drop_cache(cache_key)
                raise SearchUnavailable(
                    "IQDB: «Can't read query result» — сервер не справился, попробуем позже"
                )
        if not response.ok:
            raise SearchUnavailable(f"IQDB не ответил: статус {response.status}")
        return await asyncio.to_thread(self.parse, response.text())

    async def _request(self, image: bytes, cache_key: str | None) -> Fetched:
        routes = self._fetcher.routes_for(IQDB_HOST)
        while True:
            response = await fetch_routed(
                self._fetcher,
                self._limiter,
                IQDB_HOST,
                "POST",
                IQDB_URL,
                data={"service[]": [str(service) for service in SERVICES], "url": ""},
                files={"file": ("image.jpg", image, "image/jpeg")},
                headers={"Referer": IQDB_URL},
                cache_key=cache_key,
                cache_ttl=IQDB_CACHE_TTL,
                batch=1,
                routes=routes,
            )

            if self._limiter is None or response.from_cache:
                break
            if response.status == 429:
                self._limiter.note_rate_limited(response.proxy)
                routes = [route for route in routes if route != response.proxy]
                if routes:
                    continue
                break
            if not response.ok:
                self._limiter.note_failure(response.proxy)
                break
            self._limiter.note_success(response.proxy)
            break
        return response

    def parse(self, html: str) -> list[DiscoveryHit]:
        soup = BeautifulSoup(html, "lxml")
        if soup.find("div", class_="nomatch") is not None:
            return []

        container = soup.find("div", id="pages")
        if container is None:
            return []

        hits: list[DiscoveryHit] = []
        for block in container.find_all("div", recursive=False):
            rows = block.find_all("tr")
            if len(rows) < 3:
                continue

            title = rows[0].get_text(" ", strip=True) if rows else ""
            if "your image" in title.lower():
                continue

            names_row = rows[2].get_text(" ", strip=True) if len(rows) > 2 else ""
            similarity = 0.0
            for row in rows[3:]:
                found = _SIMILARITY_RE.search(row.get_text(" ", strip=True))
                if found:
                    similarity = int(found.group(1)) / 100.0
                    break

            image_cell = block.find("td", class_="image")
            links = [
                absolute_url(anchor["href"])
                for anchor in (image_cell.find_all("a", href=True) if image_cell else [])
            ]
            md5 = None
            preview = image_cell.find("img") if image_cell else None
            if preview is not None:
                md5 = md5_from_url(preview.get("src", ""))
            if md5 is None:
                for link in links:
                    md5 = md5_from_url(link)
                    if md5:
                        break

            names = [part for part in re.split(r"[\s,]+", names_row) if part]
            raw_tags = (
                preview_tags(preview) if similarity >= self._min_similarity else ()
            )
            if not names:
                for index, link in enumerate(links):
                    hits.append(
                        DiscoveryHit(
                            engine=Source.IQDB,
                            similarity=similarity,
                            url=link,
                            md5=md5,
                            raw_tags=raw_tags if index == 0 else (),
                        )
                    )
                continue

            for index, name in enumerate(names):
                source = SOURCE_NAMES.get(name.lower())
                url = links[index] if index < len(links) else None
                hits.append(
                    DiscoveryHit(
                        engine=Source.IQDB,
                        similarity=similarity,
                        source=source,
                        url=url,
                        md5=md5_from_url(url) or md5,
                        raw_tags=raw_tags if index == 0 else (),
                    )
                )
        return hits
