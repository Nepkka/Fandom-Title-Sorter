from __future__ import annotations

from heapq import nsmallest

from ..storage.db import Database
from ..storage.paths import hamming


class PhashIndex:
    def __init__(self, entries: list[tuple[int, int]] | None = None) -> None:
        self._entries: list[tuple[int, int]] = list(entries or [])

    @classmethod
    def from_db(cls, db: Database) -> PhashIndex:
        return cls(db.decided_phashes())

    def __len__(self) -> int:
        return len(self._entries)

    def add(self, image_id: int, phash: int | None) -> None:
        if phash is not None:
            self._entries.append((phash, image_id))

    def nearest(
        self, phash: int, max_distance: int = 6, limit: int | None = None
    ) -> list[tuple[int, int]]:
        size = limit if limit is not None else len(self._entries)
        if size <= 0:
            return []
        found = (
            (distance, image_id)
            for stored, image_id in self._entries
            if (distance := hamming(stored, phash)) <= max_distance
        )
        return [
            (image_id, distance)
            for distance, image_id in nsmallest(size, found, key=lambda item: item[0])
        ]
