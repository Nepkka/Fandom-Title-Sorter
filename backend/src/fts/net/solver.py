from __future__ import annotations

import json

import httpx

from .errors import NETWORK_ERRORS

CF_STATUSES = frozenset({403, 503})
CF_MARKERS = ("just a moment", "cf-mitigated", "attention required", "checking your browser")


def looks_like_challenge(status: int, headers: dict[str, str], body: bytes) -> bool:
    if status not in CF_STATUSES:
        return False
    server = (headers.get("server") or headers.get("Server") or "").lower()
    if "cloudflare" not in server:
        return False
    head = body[:4000].decode("utf-8", "replace").lower()
    return any(marker in head for marker in CF_MARKERS)


class CloudflareSolver:
    def __init__(
        self,
        base_url: str,
        *,
        timeout: float = 60.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self._timeout = timeout
        self._transport = transport
        self.solved = 0
        self.failed = 0

    async def solve_get(self, url: str) -> tuple[int, bytes] | None:
        payload = {"cmd": "request.get", "url": url, "maxTimeout": int(self._timeout * 1000)}
        try:
            async with httpx.AsyncClient(
                transport=self._transport,
                timeout=httpx.Timeout(self._timeout),
            ) as client:
                response = await client.post(f"{self.base_url}/v1", json=payload)
        except NETWORK_ERRORS:
            self.failed += 1
            return None

        if response.status_code != 200:
            self.failed += 1
            return None
        try:
            data = json.loads(response.text)
        except json.JSONDecodeError:
            self.failed += 1
            return None

        solution = data.get("solution") or {}
        body = solution.get("response")
        if not isinstance(body, str):
            self.failed += 1
            return None
        self.solved += 1
        return int(solution.get("status") or 200), body.encode("utf-8", "replace")

    def stats(self) -> dict[str, int]:
        return {"solved": self.solved, "failed": self.failed}
