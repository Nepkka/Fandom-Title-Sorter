from __future__ import annotations

import logging
import re
from collections.abc import Callable

from ..domain.models import ORIGINAL_TAG, SearchResult, Source
from ..domain.naming import normalize_key
from ..domain.rules import FandomMap
from ..net.client import Fetcher

logger = logging.getLogger(__name__)

ARTWORK_URL = "https://www.pixiv.net/artworks/{}"
ILLUST_API = "https://www.pixiv.net/ajax/illust/{}"
ARTWORK_ID = re.compile(r"/artworks/(\d+)")
HEADERS = {
    "Referer": "https://www.pixiv.net/",
    "Accept-Language": "en-US,en;q=0.9",
}


def english_tag(tag: dict) -> str | None:
    translation = tag.get("translation")
    if isinstance(translation, dict):
        value = translation.get("en")
        if isinstance(value, str) and value:
            return value
    return None


class PixivBoard:
    name = Source.PIXIV
    hosts: tuple[str, ...] = ("pixiv.net", "www.pixiv.net")

    def __init__(
        self,
        fetcher: Fetcher,
        fandom_map: FandomMap,
        tag_lookup: Callable[[str], str | None] | None = None,
    ) -> None:
        self._fetcher = fetcher
        self._map = fandom_map
        self._tag_lookup = tag_lookup

    def _learned(self, tag: str) -> str | None:
        if self._tag_lookup is None:
            return None
        name = self._tag_lookup(tag)
        if name is None or self._map.resolve(name).best is None:
            return None
        return name

    def _fandom_tags(self, tags: list[object]) -> tuple[str, ...]:
        direct: list[str] = []
        learned: list[str] = []
        for tag in tags:
            if not isinstance(tag, dict):
                continue
            for candidate in (english_tag(tag), tag.get("tag")):
                if not candidate or not isinstance(candidate, str):
                    continue
                if normalize_key(candidate) == ORIGINAL_TAG:
                    direct.append(candidate)
                    break
                if self._map.resolve(candidate).best is not None:
                    direct.append(candidate)
                    break
                found = self._learned(candidate)
                if found is not None:
                    learned.append(found)
                    break
        if direct:
            return tuple(dict.fromkeys(direct))
        unique = tuple(dict.fromkeys(learned))
        return unique if len(unique) == 1 else ()

    @staticmethod
    def _raw_tags(tags: list[object]) -> tuple[str, ...]:
        names: list[str] = []
        for tag in tags:
            if not isinstance(tag, dict):
                continue
            for candidate in (tag.get("tag"), english_tag(tag)):
                if isinstance(candidate, str) and candidate:
                    names.append(candidate)
        return tuple(dict.fromkeys(names))

    async def by_id(self, post_id: str) -> SearchResult | None:
        data, response = await self._fetcher.get_json(
            ILLUST_API.format(post_id),
            cache_key=f"pixiv:id:{post_id}",
            headers=HEADERS,
        )
        if not response.ok:
            logger.info(
                "pixiv: работа %s недоступна (статус %s) — удалена, закрыта или нужен вход",
                post_id,
                response.status,
            )
            return None
        if not isinstance(data, dict) or data.get("error"):
            logger.info("pixiv: работа %s не отдаёт тегов", post_id)
            return None
        body = data.get("body")
        if not isinstance(body, dict):
            return None
        tags = body.get("tags")
        values = tags.get("tags") if isinstance(tags, dict) else []
        artist = body.get("userName")
        return SearchResult(
            source=Source.PIXIV,
            post_id=str(body.get("illustId") or post_id),
            post_url=ARTWORK_URL.format(post_id),
            fandom_tags=self._fandom_tags(values),
            artist_tags=(artist,) if isinstance(artist, str) and artist else (),
            similarity=1.0,
            raw_tags=self._raw_tags(values),
        )

    async def by_md5(self, md5: str) -> SearchResult | None:
        return None

    def post_id_from_url(self, url: str) -> str | None:
        found = ARTWORK_ID.search(url)
        return found.group(1) if found else None
