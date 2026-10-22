from __future__ import annotations

import httpx

NETWORK_ERRORS: tuple[type[BaseException], ...] = (httpx.HTTPError, OSError)
try:
    from socksio.exceptions import ProtocolError as _SocksProtocolError
except ImportError:  # pragma: no cover
    pass
else:
    NETWORK_ERRORS = (*NETWORK_ERRORS, _SocksProtocolError)

__all__ = ["NETWORK_ERRORS"]
