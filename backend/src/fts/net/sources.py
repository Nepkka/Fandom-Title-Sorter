from __future__ import annotations

import json
import re
from dataclasses import dataclass
from enum import StrEnum


class SourceFormat(StrEnum):
    PLAIN = "plain"
    SPYS = "spys"
    GEONODE = "geonode"


@dataclass(frozen=True, slots=True)
class ProxyRecord:
    host: str
    port: int
    scheme: str
    source: str
    country: str | None = None
    anonymity: str | None = None
    latency_ms: int | None = None
    uptime: float | None = None

    @property
    def key(self) -> str:
        return f"{self.scheme}://{self.host}:{self.port}"


@dataclass(frozen=True, slots=True)
class ProxySource:
    name: str
    url: str
    fmt: SourceFormat
    scheme: str = "http"


SOURCES: tuple[ProxySource, ...] = (
    ProxySource(
        name="proxifly-all",
        url="https://raw.githubusercontent.com/proxifly/free-proxy-list/main/proxies/all/data.txt",
        fmt=SourceFormat.PLAIN,
    ),
    ProxySource(
        name="fyvri-fresh-http",
        url=(
            "https://raw.githubusercontent.com/fyvri/fresh-proxy-list/"
            "archive/storage/classic/http.txt"
        ),
        fmt=SourceFormat.PLAIN,
    ),
    ProxySource(
        name="geonode",
        url=(
            "https://proxylist.geonode.com/api/proxy-list"
            "?limit=500&page=1&sort_by=lastChecked&sort_type=desc"
        ),
        fmt=SourceFormat.GEONODE,
        scheme="",
    ),
    ProxySource(
        name="thespeedx-http",
        url="https://raw.githubusercontent.com/TheSpeedX/PROXY-List/master/http.txt",
        fmt=SourceFormat.PLAIN,
    ),
    ProxySource(
        name="thespeedx-socks5",
        url="https://raw.githubusercontent.com/TheSpeedX/PROXY-List/master/socks5.txt",
        fmt=SourceFormat.PLAIN,
        scheme="socks5",
    ),
    ProxySource(
        name="proxyscrape-v4-http",
        url=(
            "https://api.proxyscrape.com/v4/free-proxy-list/get"
            "?request=display_proxies&protocol=http&proxy_format=ipport&format=text"
        ),
        fmt=SourceFormat.PLAIN,
    ),
    ProxySource(
        name="monosans-http",
        url="https://raw.githubusercontent.com/monosans/proxy-list/main/proxies/http.txt",
        fmt=SourceFormat.PLAIN,
    ),
    ProxySource(
        name="monosans-socks5",
        url="https://raw.githubusercontent.com/monosans/proxy-list/main/proxies/socks5.txt",
        fmt=SourceFormat.PLAIN,
        scheme="socks5",
    ),
    ProxySource(
        name="spys-me",
        url="https://spys.me/proxy.txt",
        fmt=SourceFormat.SPYS,
    ),
)

_LINE = re.compile(
    r"^(?:(?P<scheme>[a-zA-Z][a-zA-Z0-9+.\-]*)://)?"
    r"(?:(?P<user>[^:@\s]+):(?P<password>[^@\s]+)@)?"
    r"(?P<host>\d{1,3}(?:\.\d{1,3}){3}):(?P<port>\d{1,5})$"
)

_OCTET = re.compile(r"^\d{1,3}(?:\.\d{1,3}){3}$")


def _valid_host(host: str) -> bool:
    if not _OCTET.match(host):
        return False
    return all(0 <= int(part) <= 255 for part in host.split("."))


def _valid_port(port: int) -> bool:
    return 1 <= port <= 65535


def _number(value: object) -> float | None:
    if value is None:
        return None
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _record(
    source: ProxySource,
    host: str,
    port: int,
    scheme: str | None = None,
    country: str | None = None,
    anonymity: str | None = None,
    latency_ms: int | None = None,
    uptime: float | None = None,
) -> ProxyRecord | None:
    if not _valid_host(host) or not _valid_port(port):
        return None
    return ProxyRecord(
        host=host,
        port=port,
        scheme=scheme or source.scheme or "http",
        source=source.name,
        country=country,
        anonymity=anonymity,
        latency_ms=latency_ms,
        uptime=uptime,
    )


def parse_plain(text: str, source: ProxySource) -> list[ProxyRecord]:
    records: list[ProxyRecord] = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith(("#", "//", ";")):
            continue
        match = _LINE.match(line)
        if match is None:
            continue
        record = _record(
            source,
            match["host"],
            int(match["port"]),
            scheme=match["scheme"],
        )
        if record is not None:
            records.append(record)
    return records


def parse_spys(text: str, source: ProxySource) -> list[ProxyRecord]:
    records: list[ProxyRecord] = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith(("#", "//", ";")):
            continue
        parts = line.split()
        match = _LINE.match(parts[0])
        if match is None:
            continue
        country: str | None = None
        anonymity: str | None = None
        if len(parts) > 1:
            tag = parts[1]
            country, _, anonymity = tag.partition("-")
            country = country or None
            anonymity = anonymity or None
        record = _record(
            source,
            match["host"],
            int(match["port"]),
            scheme=match["scheme"],
            country=country,
            anonymity=anonymity,
        )
        if record is not None:
            records.append(record)
    return records


def parse_geonode(text: str, source: ProxySource) -> list[ProxyRecord]:
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return []
    rows = payload.get("data") if isinstance(payload, dict) else payload
    if not isinstance(rows, list):
        return []

    records: list[ProxyRecord] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        host = str(row.get("ip") or "").strip()
        raw_port = str(row.get("port") or "").strip()
        if not host or not raw_port.isdigit():
            continue
        protocols = row.get("protocols") or []
        scheme = str(protocols[0]).lower() if isinstance(protocols, list) and protocols else None
        latency = _number(row.get("latency"))
        uptime = _number(row.get("upTime") if row.get("upTime") is not None else row.get("uptime"))
        record = _record(
            source,
            host,
            int(raw_port),
            scheme=scheme,
            country=(str(row.get("country")).upper() if row.get("country") else None),
            anonymity=(str(row.get("anonymityLevel")) if row.get("anonymityLevel") else None),
            latency_ms=(int(round(latency)) if latency is not None else None),
            uptime=uptime,
        )
        if record is not None:
            records.append(record)
    return records


_PARSERS = {
    SourceFormat.PLAIN: parse_plain,
    SourceFormat.SPYS: parse_spys,
    SourceFormat.GEONODE: parse_geonode,
}


def parse(text: str, source: ProxySource) -> list[ProxyRecord]:
    parser = _PARSERS.get(source.fmt)
    if parser is None:
        raise ValueError(f"неизвестный формат источника: {source.fmt}")
    return parser(text, source)


def dedupe(records: list[ProxyRecord]) -> list[ProxyRecord]:
    seen: set[str] = set()
    result: list[ProxyRecord] = []
    for record in records:
        if record.key in seen:
            continue
        seen.add(record.key)
        result.append(record)
    return result


def normalize(text: str, source: ProxySource) -> list[ProxyRecord]:
    return dedupe(parse(text, source))
