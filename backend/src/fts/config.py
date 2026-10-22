from __future__ import annotations

import os
import secrets
from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


def _default_data_dir() -> Path:
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    return Path(base) / "FTS"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        extra="ignore",
        case_sensitive=False,
    )

    fts_input_root: str = ""
    fts_output_root: str = ""
    fts_data_dir: str = ""

    fts_host: str = "127.0.0.1"
    fts_port: int = 0
    fts_api_token: str = ""

    saucenao_per_window: int = 3
    saucenao_window_seconds: int = 30
    saucenao_max_per_window: int = 12
    saucenao_daily_budget_per_ip: int = 100
    saucenao_min_similarity: float = 0.80

    iqdb_per_window: int = 4
    iqdb_window_seconds: int = 30
    iqdb_max_per_window: int = 8
    iqdb_daily_budget_per_ip: int = 100000
    iqdb_min_similarity: float = 0.8

    tracemoe_per_window: int = 10
    tracemoe_window_seconds: int = 60
    tracemoe_max_per_window: int = 20
    tracemoe_daily_budget_per_ip: int = 100
    tracemoe_min_similarity: float = 0.75

    proxy_sources: str = ""
    proxy_file: str = "data/proxies.txt"
    proxy_ready_per_host: int = 16
    proxy_min_ready: int = 3
    proxy_validate_concurrency: int = 200
    proxy_connect_timeout: float = 4.0
    proxy_check_timeout: float = 6.0
    direct_fail_threshold: int = 3
    direct_retry_after: float = 1800.0

    fts_max_concurrency: int = 64
    fts_per_host_concurrency: int = 16
    fts_per_host_cap: int = 16
    fts_proxy_batch: int = 8
    fts_proxy_attempts: int = 16
    fts_topup_batch: int = 1000
    fts_topup_timeout: float = 120.0
    fts_cf_solver_url: str = ""
    fts_browser_bypass: bool = True
    fts_browser_path: str = ""
    fts_browser_tabs: int = 4
    fts_browser_hidden: bool = True
    fts_connect_timeout: float = 3.0
    fts_read_timeout: float = 30.0
    fts_user_agent: str = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    )

    fts_sep_characters: bool = True
    fts_min_similarity: float = 0.55
    fts_review_below_confidence: float = 0.5
    fts_auto_fandom: bool = True
    fts_fandom_similarity: float = 0.8

    fts_thumbs_max_age_days: int = 30
    fts_thumbs_max_bytes: int = 512 * 1024 * 1024
    fts_http_cache_max_age_days: int = 30
    fts_log_level: str = "INFO"

    @property
    def data_path(self) -> Path:
        return Path(self.fts_data_dir) if self.fts_data_dir else _default_data_dir()

    @property
    def db_path(self) -> Path:
        return self.data_path / "fts.db"

    @property
    def thumbs_dir(self) -> Path:
        return self.data_path / "thumbs"

    @property
    def cache_dir(self) -> Path:
        return self.data_path / "cache"

    @property
    def logs_dir(self) -> Path:
        return self.data_path / "logs"

    @property
    def proxy_file_path(self) -> Path:
        path = Path(self.proxy_file)
        return path if path.is_absolute() else self.data_path / path

    @property
    def frontend_dist(self) -> Path:
        return Path(__file__).resolve().parents[3] / "frontend" / "dist"

    @property
    def extra_proxy_sources(self) -> list[str]:
        return [s.strip() for s in self.proxy_sources.split(",") if s.strip()]

    def resolve_api_token(self) -> str:
        if self.fts_api_token:
            return self.fts_api_token
        token_file = self.data_path / "token"
        if token_file.exists():
            token = token_file.read_text(encoding="utf-8").strip()
            if token:
                return token
        token = secrets.token_urlsafe(32)
        self.ensure_dirs()
        token_file.write_text(token, encoding="utf-8")
        return token

    def ensure_dirs(self) -> None:
        for path in (self.data_path, self.thumbs_dir, self.cache_dir, self.logs_dir):
            path.mkdir(parents=True, exist_ok=True)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
