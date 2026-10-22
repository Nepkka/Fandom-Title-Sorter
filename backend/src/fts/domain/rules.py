from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping

from .models import Fandom, Match, Resolution
from .naming import normalize_key


def rules_revision(rules: Iterable[Fandom]) -> str:
    blob = json.dumps(
        [
            {
                "name": rule.name,
                "aliases": list(rule.aliases),
                "priority": rule.priority,
            }
            for rule in rules
        ],
        ensure_ascii=False,
        sort_keys=True,
    )
    return hashlib.sha1(blob.encode("utf-8")).hexdigest()[:12]


def _rank(match: Match) -> tuple[int, int, str]:
    return (-match.fandom.priority, -len(match.matched), match.fandom.name)


class FandomMap:
    def __init__(self, fandoms: Iterable[Fandom]) -> None:
        self._fandoms: tuple[Fandom, ...] = tuple(fandoms)
        index: dict[str, list[Match]] = {}
        for fandom in self._fandoms:
            for alias in (fandom.name, *fandom.aliases):
                key = normalize_key(alias)
                if key:
                    index.setdefault(key, []).append(Match(fandom, alias))
        self._index: dict[str, tuple[Match, ...]] = {
            key: tuple(sorted(matches, key=_rank)) for key, matches in index.items()
        }

    def __len__(self) -> int:
        return len(self._fandoms)

    def resolve(self, tag: str) -> Resolution:
        key = normalize_key(tag)
        if not key:
            return Resolution()
        return Resolution(self._index.get(key, ()))

    def resolve_first(self, tags: Iterable[str]) -> Resolution:
        for tag in tags:
            resolution = self.resolve(tag)
            if resolution.matches:
                return resolution
        return Resolution()


def fandoms_from_dict(data: Mapping[str, Iterable[str]]) -> list[Fandom]:
    fandoms: list[Fandom] = []
    for name, tokens in data.items():
        aliases = [name]
        for token in tokens:
            if token and token not in aliases:
                aliases.append(token)
        fandoms.append(Fandom(name=name, aliases=tuple(aliases)))
    return fandoms


def merge_duplicate_aliases(fandoms: Iterable[Fandom]) -> list[Fandom]:
    order: list[str] = []
    merged: dict[str, Fandom] = {}
    for fandom in fandoms:
        key = normalize_key(fandom.name)
        if key not in merged:
            order.append(key)
            merged[key] = fandom
            continue
        current = merged[key]
        aliases = list(current.aliases)
        for alias in (fandom.name, *fandom.aliases):
            if alias not in aliases:
                aliases.append(alias)
        merged[key] = Fandom(
            name=current.name,
            aliases=tuple(aliases),
            priority=max(current.priority, fandom.priority),
        )
    return [merged[key] for key in order]
