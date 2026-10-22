from __future__ import annotations

import difflib
import re
from collections.abc import Iterable, Mapping

from .models import Fandom
from .naming import normalize_key

TAG_VOTES_MIN = 3
TAG_SHARE_MIN = 0.9

_SPLIT = re.compile(r"[^0-9a-zа-яё]+")

JUNK_TAGS = frozenset(
    normalize_key(tag)
    for tag in (
        "instagram",
        "youtube",
        "twitter",
        "pixiv",
        "deviantart",
        "tumblr",
        "artstation",
        "facebook",
        "vk",
        "danbooru",
        "gelbooru",
        "yande.re",
        "konachan",
        "sankaku",
        "e-shuushuu",
        "zerochan",
        "anime-pictures",
        "newgrounds",
        "reddit",
        "weibo",
        "bilibili",
        "nicovideo",
        "patreon",
        "fanbox",
        "skeb",
        "web",
        "original",
        "unknown",
        "comic",
        "sketch",
        "monochrome",
        "absurdres",
        "highres",
        "original character",
        "itunes",
        "app store",
        "appstore",
        "apple",
        "apple inc",
        "google",
        "google play",
        "microsoft",
        "adobe",
        "nintendo",
        "sony",
        "playstation",
        "xbox",
        "steam",
        "crunchyroll",
        "netflix",
        "amazon",
        "spotify",
        "soundcloud",
        "bandcamp",
        "twitch",
        "discord",
        "telegram",
        "imgur",
        "pinterest",
        "tiktok",
        "dailymotion",
        "boosty",
        "gumroad",
        "ko-fi",
        "kofi",
        "buymeacoffee",
        "subscribestar",
        "fantia",
        "ci-en",
        "tinami",
        "nijie",
        "seiga",
        "clip studio",
        "medibang",
        "mihoyo",
        "hoyoverse",
        "capcom",
        "bandai",
        "bandai namco",
        "square enix",
        "sega",
        "konami",
        "tencent",
        "netease",
        "grasshopper",
        "koei",
        "tecmo",
        "snk",
        "bushiroad",
        "cygames",
        "yostar",
        "manjuu",
        "hypergryph",
        "sunborn",
        "colopl",
        "mixi",
        "level-5",
        "cyberconnect2",
        "happy elements",
        "bili bili douga",
        "bili bili",
    )
)


def words(text: str) -> set[str]:
    return {part for part in _SPLIT.split(normalize_key(text)) if part}


def is_junk_tag(tag: str) -> bool:
    return normalize_key(tag) in JUNK_TAGS


def fandom_name_from_tag(tag: str) -> str:
    base = re.sub(r"\s+", " ", tag.replace("_", " ")).strip()
    if not base:
        return ""
    return " ".join(word[:1].upper() + word[1:] for word in base.split(" "))


def closeness(tag: str, known: str) -> float:
    left, right = normalize_key(tag), normalize_key(known)
    if not left or not right:
        return 0.0
    if left == right:
        return 1.0
    score = difflib.SequenceMatcher(None, left, right).ratio()
    tag_words, known_words = words(tag), words(known)
    if tag_words and known_words:
        small, big = sorted((tag_words, known_words), key=len)
        if small <= big:
            score = max(score, 0.75 + 0.25 * len(small) / len(big))
    if left in right or right in left:
        score = max(score, 0.8)
    return round(score, 3)


def closest_fandom(tag: str, fandoms: Iterable[Fandom]) -> tuple[str, float]:
    best_name, best_score = "", 0.0
    for fandom in fandoms:
        for alias in (fandom.name, *fandom.aliases):
            score = closeness(tag, alias)
            if score > best_score:
                best_name, best_score = fandom.name, score
    return best_name, best_score


def pick_tag_fandom(
    votes: Mapping[str, int],
    min_votes: int = TAG_VOTES_MIN,
    share: float = TAG_SHARE_MIN,
) -> str | None:
    total = sum(votes.values())
    if total < min_votes:
        return None
    best, count = max(votes.items(), key=lambda item: (item[1], item[0]))
    return best if count / total >= share else None

