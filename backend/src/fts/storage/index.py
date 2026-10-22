from __future__ import annotations

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

    def nearest(self, phash: int, max_distance: int = 6) -> list[tuple[int, int]]:
        found = []
        for stored, image_id in self._entries:
            distance = hamming(stored, phash)
            if distance <= max_distance:
                found.append((image_id, distance))
        found.sort(key=lambda item: item[1])
        return found
