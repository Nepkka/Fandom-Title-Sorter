from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass

from .models import (
    ORIGINAL_TAG,
    Action,
    Alternative,
    Decision,
    Fandom,
    SearchResult,
    Status,
    TargetKind,
)
from .naming import display_artist, display_character, normalize_key
from .rules import FandomMap
from .similar import is_junk_tag


@dataclass(frozen=True, slots=True)
class DecisionSettings:
    min_similarity: float = 0.55
    review_below_confidence: float = 0.5
    max_alternatives: int = 3


def _found_phrase(count: int) -> str:
    if count % 10 == 1 and count % 100 != 11:
        return f"Найден {count} источник"
    if 2 <= count % 10 <= 4 and not 12 <= count % 100 <= 14:
        return f"Найдено {count} источника"
    return f"Найдено {count} источников"


def _unique(values: Iterable[str]) -> list[str]:
    seen: dict[str, str] = {}
    for value in values:
        if value:
            seen.setdefault(value.lower(), value)
    return list(seen.values())


def _confidence(evidence: int, best_similarity: float) -> float:
    return round(0.6 * min(1.0, evidence / 2) + 0.4 * best_similarity, 3)


def decide(
    results: Iterable[SearchResult],
    fandom_map: FandomMap,
    settings: DecisionSettings | None = None,
) -> Decision:
    settings = settings or DecisionSettings()
    usable = [
        r for r in results if r.status is Status.OK and r.similarity >= settings.min_similarity
    ]

    evidence: dict[str, set[str]] = defaultdict(set)
    similarities: dict[str, list[float]] = defaultdict(list)
    support: dict[str, list[SearchResult]] = defaultdict(list)
    fandom_of: dict[str, Fandom] = {}
    original_results: list[SearchResult] = []

    for result in usable:
        for tag in result.fandom_tags:
            if normalize_key(tag) == ORIGINAL_TAG:
                original_results.append(result)
                continue
            if is_junk_tag(tag):
                continue
            match = fandom_map.resolve(tag).best
            if match is None:
                continue
            key = normalize_key(match.fandom.name)
            fandom_of[key] = match.fandom
            evidence[key].add(result.evidence_key)
            similarities[key].append(result.similarity)
            support[key].append(result)

    if not evidence:
        if original_results:
            return _from_original(original_results)
        artists_only = _unique(
            display_artist(tag) for result in usable for tag in result.artist_tags
        )
        if artists_only:
            best = max((result.similarity for result in usable), default=0.0)
            count = len({result.evidence_key for result in usable})
            return Decision(
                kind=TargetKind.ORIGINAL,
                artist=artists_only[0],
                confidence=_confidence(count, best),
                reason="Фандом не определён, но автор известен"
                + (
                    "; Несколько авторов — взят первый"
                    if len(artists_only) > 1
                    else ""
                ),
            )
        tagged = [
            result
            for result in usable
            if result.fandom_tags or result.artist_tags or result.character_tags
        ]
        if tagged:
            best = max((result.similarity for result in tagged), default=0.0)
            count = len({result.evidence_key for result in tagged})
            return Decision(
                kind=TargetKind.UNKNOWN,
                confidence=_confidence(count, best),
                reason=(
                    f"{_found_phrase(count)}, но фандом по ним не определяется"
                    " — в Unknown"
                ),
            )
        if usable:
            return Decision(
                kind=TargetKind.UNKNOWN,
                confidence=0.0,
                reason="Источники ответили, но тегов не дали — в Unknown",
            )
        return Decision(
            kind=TargetKind.UNKNOWN,
            confidence=0.0,
            reason="Ничего не нашлось",
        )

    ranked = sorted(
        evidence,
        key=lambda key: (
            -len(evidence[key]),
            -fandom_of[key].priority,
            -max(similarities[key]),
            key,
        ),
    )
    winner = ranked[0]
    winner_evidence = len(evidence[winner])
    best_similarity = max(similarities[winner])
    confidence = _confidence(winner_evidence, best_similarity)

    alternatives = tuple(
        Alternative(
            fandom=fandom_of[key].name,
            evidence=len(evidence[key]),
            best_similarity=round(max(similarities[key]), 3),
        )
        for key in ranked[1 : 1 + settings.max_alternatives]
    )

    reasons: list[str] = []
    action = Action.MOVE

    tie = (
        len(ranked) > 1
        and len(evidence[ranked[1]]) == winner_evidence
        and evidence[ranked[1]] != evidence[winner]
    )
    if tie:
        action = Action.REVIEW
        reasons.append("Источники расходятся")
    elif len(ranked) > 1 and len(evidence[ranked[1]]) == winner_evidence:
        reasons.append("Посты помечены несколькими фандомами — взят приоритетный")
    if confidence < settings.review_below_confidence:
        action = Action.REVIEW
        reasons.append(
            f"Низкая уверенность: {confidence:.2f}, порог {settings.review_below_confidence:.2f}"
        )

    characters = _unique(
        display_character(tag) for result in support[winner] for tag in result.character_tags
    )
    multi_character = False
    character: str | None = None
    if len(characters) == 1:
        character = characters[0]
    elif len(characters) > 1:
        multi_character = True
        reasons.append(f"Персонажей несколько: {', '.join(characters[:4])}")

    artists = _unique(
        display_artist(tag) for result in support[winner] for tag in result.artist_tags
    )
    artist = artists[0] if artists else None
    if len(artists) > 1:
        reasons.append("Несколько авторов — взят первый")

    if not reasons:
        reasons.append(f"{_found_phrase(winner_evidence)}, похожесть {best_similarity:.2f}")

    return Decision(
        kind=TargetKind.FANDOM,
        action=action,
        fandom=fandom_of[winner].name,
        character=character,
        artist=artist,
        multi_character=multi_character,
        confidence=confidence,
        reason="; ".join(reasons),
        alternatives=alternatives,
    )


def _from_original(results: list[SearchResult]) -> Decision:
    artists = _unique(display_artist(tag) for result in results for tag in result.artist_tags)
    best_similarity = max((result.similarity for result in results), default=0.0)
    count = len({result.evidence_key for result in results})
    confidence = _confidence(count, best_similarity)

    reason = "Оригинальный персонаж"
    if not artists:
        reason += ", автора нет — папка Unknown"
    elif len(artists) > 1:
        reason += "; Несколько авторов — взят первый"

    return Decision(
        kind=TargetKind.ORIGINAL,
        artist=artists[0] if artists else None,
        confidence=confidence,
        reason=reason,
    )
