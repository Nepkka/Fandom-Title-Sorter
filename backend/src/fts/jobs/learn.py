from __future__ import annotations

import asyncio
import os
import re
from collections.abc import Callable

from ..domain.models import ORIGINAL_TAG
from ..domain.naming import normalize_key
from ..domain.similar import is_junk_tag
from ..net.client import Fetcher
from ..providers.pixiv import HEADERS, ILLUST_API, english_tag

PIXIV_ID = re.compile(r"(?<!\d)(\d{5,9})_p\d{1,3}(?!\d)")
SKIP_FOLDERS = {"original", "unknown"}


def pixiv_pages(root: str, limit: int | None = None) -> list[tuple[str, str]]:
    pages: dict[str, str] = {}
    for folder in sorted(os.listdir(root)):
        if folder.startswith(".") or folder.strip().lower() in SKIP_FOLDERS:
            continue
        base = os.path.join(root, folder)
        if not os.path.isdir(base):
            continue
        for _current, _dirs, files in os.walk(base):
            for name in files:
                found = PIXIV_ID.search(name)
                if found is None:
                    continue
                pages.setdefault(found.group(1), folder)
                if limit is not None and len(pages) >= limit:
                    return list(pages.items())
    return list(pages.items())


def tag_names(body: dict) -> list[str]:
    tags = body.get("tags")
    values = tags.get("tags") if isinstance(tags, dict) else None
    names: list[str] = []
    for tag in values or []:
        if not isinstance(tag, dict):
            continue
        for name in (tag.get("tag"), english_tag(tag)):
            if isinstance(name, str) and name:
                names.append(name)
    return names


def teachable(name: str) -> bool:
    key = normalize_key(name)
    return bool(key) and key != ORIGINAL_TAG and not is_junk_tag(name)


async def learn_from_library(
    fetcher: Fetcher,
    learn_tags: Callable[[list[tuple[str, str]]], int],
    root: str,
    limit: int | None = None,
    pause: float = 0.3,
    on_page: Callable[[int, int, str], None] | None = None,
) -> dict[str, int]:
    pages = pixiv_pages(root, limit)
    stats = {"pages": len(pages), "tagged": 0, "votes": 0, "missing": 0}
    for index, (post_id, fandom) in enumerate(pages, start=1):
        data, response = await fetcher.get_json(
            ILLUST_API.format(post_id),
            cache_key=f"pixiv:id:{post_id}",
            headers=HEADERS,
        )
        body = data.get("body") if isinstance(data, dict) and response.ok else None
        raw = tag_names(body) if isinstance(body, dict) else []
        names = [name for name in raw if teachable(name)]
        if names:
            stats["tagged"] += 1
            stats["votes"] += learn_tags([(name, fandom) for name in names])
        else:
            stats["missing"] += 1
        if on_page is not None:
            on_page(index, len(pages), fandom)
        await asyncio.sleep(pause)
    return stats
