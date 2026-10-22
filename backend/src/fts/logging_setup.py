from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

from .config import Settings

MAX_BYTES = 5 * 1024 * 1024
BACKUPS = 3

NAMESPACE = "fts"


class _FtsFileHandler(RotatingFileHandler):
    pass

def setup_logging(settings: Settings, level: str | None = None) -> Path | None:
    root = logging.getLogger()
    logging.getLogger(NAMESPACE).setLevel((level or settings.fts_log_level).upper())
    path = settings.logs_dir / "fts.log"
    if any(isinstance(handler, _FtsFileHandler) for handler in root.handlers):
        return path
    try:
        settings.logs_dir.mkdir(parents=True, exist_ok=True)
        handler = _FtsFileHandler(path, maxBytes=MAX_BYTES, backupCount=BACKUPS, encoding="utf-8")
        handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s")
        )
        root.addHandler(handler)
        return path
    except OSError:
        return None


__all__ = ["setup_logging", "MAX_BYTES", "BACKUPS"]
