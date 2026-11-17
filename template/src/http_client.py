"""httpx client with timeout and retry on transient failures."""
import asyncio
import json as _json
from typing import Any

import httpx

from errors import UpstreamError, from_status

RETRY_STATUS = {429, 500, 502, 503, 504}
MAX_BYTES = 1_000_000


async def _read_capped(r: httpx.Response, limit: int) -> bytes:
    buf = bytearray()
    async for chunk in r.aiter_bytes():
        buf += chunk
        if len(buf) > limit:
            raise UpstreamError(f"Upstream response exceeded {limit} bytes; narrow the request.")
    return bytes(buf)


async def request_json(method: str, url: str, *, headers: dict[str, str] | None = None,
                       params: dict[str, Any] | None = None, json: Any = None,
                       timeout: float = 15.0, retries: int = 2, backoff: float = 0.3, max_bytes: int = MAX_BYTES,
                       transport: httpx.AsyncBaseTransport | None = None) -> Any:
    last: Exception | None = None
    async with httpx.AsyncClient(timeout=timeout, transport=transport) as client:
        for attempt in range(retries + 1):
            try:
                req = client.build_request(method, url, headers=headers, params=params, json=json)
                r = await client.send(req, stream=True)
                try:
                    body = await _read_capped(r, max_bytes)
                finally:
                    await r.aclose()
            except httpx.HTTPError as e:
                last = UpstreamError(f"Network error: {type(e).__name__}")
            else:
                if r.status_code < 400:
                    if not body:
                        return None
                    try:
                        return _json.loads(body)
                    except ValueError:
                        raise UpstreamError("Upstream returned a non-JSON response.", r.status_code)
                last = from_status(r.status_code, body.decode("utf-8", "replace"))
                if r.status_code not in RETRY_STATUS:
                    raise last
            if attempt < retries:
                await asyncio.sleep(backoff * (2 ** attempt))
    assert last is not None
    raise last
