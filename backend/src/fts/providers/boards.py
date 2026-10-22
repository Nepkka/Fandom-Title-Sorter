from __future__ import annotations

import asyncio
import re
from collections.abc import Callable

from bs4 import BeautifulSoup

from ..domain.models import SearchResult, Source
from ..domain.rules import FandomMap
from ..net.client import Fetcher
from .pixiv import PixivBoard

CATEGORY_CLASSES: dict[str, tuple[str, ...]] = {
    "fandom": ("tag-type-copyright", "tag-type-series"),
    "artist": ("tag-type-artist",),
    "character": ("tag-type-character",),
}

SKIP_TEXTS = {"+", "-", "?", "edit", "history"}

PROBE_TTL = 3600.0

POST_FIELDS = (
    "id,source,tag_string_copyright,tag_string_artist,tag_string_character,file_url,large_file_url"
)


def _clean_tags(values: list[str]) -> tuple[str, ...]:
    seen: dict[str, str] = {}
    for value in values:
        text = value.strip()
        if not text or text.lower() in SKIP_TEXTS:
            continue
        seen.setdefault(text.lower(), text)
    return tuple(seen.values())


def parse_booru_tag_lists(html: str) -> dict[str, tuple[str, ...]]:
    soup = BeautifulSoup(html, "lxml")
    result: dict[str, tuple[str, ...]] = {}
    for field, classes in CATEGORY_CLASSES.items():
        collected: list[str] = []
        for css_class in classes:
            for element in soup.find_all("li", class_=css_class):
                for anchor in element.find_all("a", href=True):
                    href = anchor["href"]
                    if "wiki" in href or "help" in href:
                        continue
                    collected.append(anchor.get_text(" ", strip=True))
        if collected:
            result[field] = _clean_tags(collected)
    return result


class DanbooruBoard:
    name = Source.DANBOORU
    bases: tuple[str, ...] = (
        "https://danbooru.donmai.us",
        "https://safebooru.donmai.us",
        "https://betabooru.donmai.us",
    )
    hosts: tuple[str, ...] = (
        "danbooru.donmai.us",
        "safebooru.donmai.us",
        "betabooru.donmai.us",
    )

    def __init__(self, fetcher: Fetcher, bases: tuple[str, ...] | None = None) -> None:
        self._fetcher = fetcher
        self._bases = tuple(bases or self.bases)
        self._resolved: str | None = None
        self._probed = False

    @property
    def host(self) -> str:
        return (self._resolved or self._bases[0]).removeprefix("https://").removeprefix("http://")

    async def resolve_base(self) -> str | None:
        if self._probed:
            return self._resolved
        self._probed = True
        for base in self._bases:
            data, response = await self._fetcher.get_json(
                f"{base}/posts.json",
                params={"limit": 1},
                cache_key=f"probe:{base}",
                cache_ttl=PROBE_TTL,
            )
            if response.ok and isinstance(data, list):
                self._resolved = base
                return base
        return None

    @staticmethod
    def _split(value: str | None) -> tuple[str, ...]:
        return _clean_tags((value or "").split())

    def _to_result(self, post: dict, base: str) -> SearchResult:
        post_id = str(post.get("id") or "")
        return SearchResult(
            source=Source.DANBOORU,
            post_id=post_id,
            post_url=f"{base}/posts/{post_id}" if post_id else None,
            fandom_tags=self._split(post.get("tag_string_copyright")),
            artist_tags=self._split(post.get("tag_string_artist")),
            character_tags=self._split(post.get("tag_string_character")),
            similarity=1.0,
            image_url=post.get("file_url") or post.get("large_file_url"),
        )

    async def by_id(self, post_id: str) -> SearchResult | None:
        base = await self.resolve_base()
        if base is None:
            return None
        data, response = await self._fetcher.get_json(
            f"{base}/posts/{post_id}.json",
            cache_key=f"danbooru:id:{post_id}",
        )
        if not response.ok or not isinstance(data, dict):
            return None
        return self._to_result(data, base)

    async def by_md5(self, md5: str) -> SearchResult | None:
        base = await self.resolve_base()
        if base is None:
            return None
        data, response = await self._fetcher.get_json(
            f"{base}/posts.json",
            params={"tags": f"md5:{md5}", "limit": 1, "only": POST_FIELDS},
            cache_key=f"danbooru:md5:{md5}",
        )
        if not response.ok or not isinstance(data, list) or not data:
            return None
        return self._to_result(data[0], base)

    async def by_pixiv_id(self, pixiv_id: str) -> SearchResult | None:
        base = await self.resolve_base()
        if base is None:
            return None
        data, response = await self._fetcher.get_json(
            f"{base}/posts.json",
            params={"tags": f"pixiv_id:{pixiv_id}", "limit": 1, "only": POST_FIELDS},
            cache_key=f"danbooru:pixiv:{pixiv_id}",
        )
        if not response.ok or not isinstance(data, list) or not data:
            return None
        return self._to_result(data[0], base)

    def post_id_from_url(self, url: str) -> str | None:
        found = re.search(r"/posts/(\d+)", url)
        return found.group(1) if found else None


class GelbooruBoard:
    name = Source.GELBOORU
    bases: tuple[str, ...] = (
        "https://gelbooru.com",
        "https://safebooru.org",
        "https://rule34.xxx",
        "https://xbooru.com",
        "https://tbib.org",
    )
    hosts: tuple[str, ...] = (
        "gelbooru.com",
        "safebooru.org",
        "rule34.xxx",
        "xbooru.com",
        "tbib.org",
    )
    post_path = "/index.php?page=post&s=view&id={}"

    def __init__(self, fetcher: Fetcher, bases: tuple[str, ...] | None = None) -> None:
        self._fetcher = fetcher
        self._bases = tuple(bases or self.bases)
        self._resolved: str | None = None
        self._probed = False

    @property
    def host(self) -> str:
        return (self._resolved or self._bases[0]).removeprefix("https://").removeprefix("http://")

    async def resolve_base(self) -> str | None:
        if self._probed:
            return self._resolved
        self._probed = True
        for base in self._bases:
            data, response = await self._fetcher.get_json(
                f"{base}/index.php",
                params={
                    "page": "dapi",
                    "s": "post",
                    "q": "index",
                    "json": "1",
                    "limit": 1,
                },
                cache_key=f"probe:{base}",
                cache_ttl=PROBE_TTL,
            )
            if response.ok and isinstance(data, dict) and data.get("post"):
                self._resolved = base
                return base
        return None

    async def _by_api(self, params: dict[str, str], key: str, base: str) -> str | None:
        data, response = await self._fetcher.get_json(
            f"{base}/index.php",
            params={"page": "dapi", "s": "post", "q": "index", "json": "1", **params},
            cache_key=key,
        )
        if not response.ok or not isinstance(data, dict):
            return None
        posts = data.get("post") or []
        if isinstance(posts, dict):
            posts = [posts]
        if not posts:
            return None
        return str(posts[0].get("id") or "") or None

    async def _tags(self, post_id: str, base: str) -> SearchResult | None:
        url = f"{base}{self.post_path.format(post_id)}"
        response = await self._fetcher.fetch(
            "GET", url, cache_key=f"gelbooru:html:{base}:{post_id}"
        )
        if not response.ok:
            return None
        tags = await asyncio.to_thread(parse_booru_tag_lists, response.text())
        return SearchResult(
            source=Source.GELBOORU,
            post_id=post_id,
            post_url=url,
            fandom_tags=tags.get("fandom", ()),
            artist_tags=tags.get("artist", ()),
            character_tags=tags.get("character", ()),
            similarity=1.0,
        )

    async def by_id(self, post_id: str) -> SearchResult | None:
        base = await self.resolve_base()
        return await self._tags(post_id, base) if base else None

    async def by_md5(self, md5: str) -> SearchResult | None:
        base = await self.resolve_base()
        if base is None:
            return None
        post_id = await self._by_api({"tags": f"md5:{md5}"}, f"gelbooru:md5:{md5}", base)
        return await self._tags(post_id, base) if post_id else None

    def post_id_from_url(self, url: str) -> str | None:
        found = re.search(r"[?&]id=(\d+)", url)
        return found.group(1) if found else None


ESH_UUSHUU_TYPES: dict[str, str] = {"2": "fandom", "3": "artist", "4": "character"}


def parse_eshuushuu_tags(html: str) -> dict[str, tuple[str, ...]]:
    soup = BeautifulSoup(html, "lxml")
    collected: dict[str, list[str]] = {}
    for anchor in soup.find_all("a", attrs={"data-tag-type": True}):
        if "badge-main" not in (anchor.get("class") or []):
            continue
        field = ESH_UUSHUU_TYPES.get(str(anchor.get("data-tag-type")))
        if field is None:
            continue
        collected.setdefault(field, []).append(anchor.get_text(" ", strip=True))
    return {field: _clean_tags(values) for field, values in collected.items()}


class EshuushuuBoard:
    name = Source.ESHUUSHUU
    host = "e-shuushuu.net"
    hosts = ("e-shuushuu.net",)

    def __init__(self, fetcher: Fetcher) -> None:
        self._fetcher = fetcher

    async def _tags(self, post_id: str) -> SearchResult | None:
        url = f"https://{self.host}/image/{post_id}/"
        response = await self._fetcher.fetch(
            "GET", url, cache_key=f"{self.host}:html:{post_id}"
        )
        if not response.ok:
            return None
        tags = await asyncio.to_thread(parse_eshuushuu_tags, response.text())
        return SearchResult(
            source=Source.ESHUUSHUU,
            post_id=post_id,
            post_url=url,
            fandom_tags=tags.get("fandom", ()),
            artist_tags=tags.get("artist", ()),
            character_tags=tags.get("character", ()),
            similarity=1.0,
        )

    async def by_id(self, post_id: str) -> SearchResult | None:
        return await self._tags(post_id)

    async def by_md5(self, md5: str) -> SearchResult | None:
        return None

    def post_id_from_url(self, url: str) -> str | None:
        found = re.search(r"/image/(\d+)", url)
        return found.group(1) if found else None


class MoebooruBoard:
    def __init__(
        self,
        fetcher: Fetcher,
        name: Source,
        host: str,
        mirrors: tuple[str, ...] = (),
    ) -> None:
        self._fetcher = fetcher
        self.name = name
        self.hosts = (host, *mirrors)
        self.post_url = "https://{host}/post/show/{post}"
        self._resolved: str | None = None
        self._probed = False

    @property
    def host(self) -> str:
        return self._resolved or self.hosts[0]

    async def resolve_host(self) -> str | None:
        if self._probed:
            return self._resolved
        self._probed = True
        for host in self.hosts:
            data, response = await self._fetcher.get_json(
                f"https://{host}/post.json",
                params={"limit": 1},
                cache_key=f"probe:https://{host}",
                cache_ttl=PROBE_TTL,
            )
            if response.ok and isinstance(data, list):
                self._resolved = host
                return host
        return None

    async def _post_id(self, tags: str, cache_key: str) -> str | None:
        host = await self.resolve_host()
        if host is None:
            return None
        data, response = await self._fetcher.get_json(
            f"https://{host}/post.json",
            params={"tags": tags, "limit": 1},
            cache_key=cache_key,
        )
        if not response.ok or not isinstance(data, list) or not data:
            return None
        return str(data[0].get("id") or "") or None

    async def _post_id_by_md5(self, md5: str) -> str | None:
        return await self._post_id(f"md5:{md5}", f"{self.host}:md5:{md5}")

    async def _post_id_by_pixiv(self, pixiv_id: str) -> str | None:
        return await self._post_id(f"source:*{pixiv_id}*", f"{self.host}:pixiv:{pixiv_id}")

    async def _tags(self, post_id: str) -> SearchResult | None:
        host = await self.resolve_host()
        if host is None:
            return None
        url = self.post_url.format(host=host, post=post_id)
        response = await self._fetcher.fetch("GET", url, cache_key=f"{host}:html:{post_id}")
        if not response.ok:
            return None
        tags = await asyncio.to_thread(parse_booru_tag_lists, response.text())
        return SearchResult(
            source=self.name,
            post_id=post_id,
            post_url=url,
            fandom_tags=tags.get("fandom", ()),
            artist_tags=tags.get("artist", ()),
            character_tags=tags.get("character", ()),
            similarity=1.0,
        )

    async def by_id(self, post_id: str) -> SearchResult | None:
        return await self._tags(post_id)

    async def by_md5(self, md5: str) -> SearchResult | None:
        post_id = await self._post_id_by_md5(md5)
        return await self._tags(post_id) if post_id else None

    async def by_pixiv_id(self, pixiv_id: str) -> SearchResult | None:
        post_id = await self._post_id_by_pixiv(pixiv_id)
        return await self._tags(post_id) if post_id else None

    def post_id_from_url(self, url: str) -> str | None:
        found = re.search(r"/post/show/(\d+)", url)
        return found.group(1) if found else None


def build_boards(
    fetcher: Fetcher,
    fandom_map: FandomMap,
    tag_lookup: Callable[[str], str | None] | None = None,
) -> dict[Source, object]:
    moebooru = [
        MoebooruBoard(fetcher, Source.YANDERE, "yande.re"),
        MoebooruBoard(fetcher, Source.KONACHAN, "konachan.net", ("konachan.com",)),
    ]
    boards: list[object] = [
        DanbooruBoard(fetcher),
        GelbooruBoard(fetcher),
        EshuushuuBoard(fetcher),
        *moebooru,
        PixivBoard(fetcher, fandom_map, tag_lookup),
    ]
    return {board.name: board for board in boards}
