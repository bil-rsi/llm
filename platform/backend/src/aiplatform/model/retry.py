"""Open a streaming HTTP response with bounded retries. Retries happen only before any byte reaches the caller."""

from __future__ import annotations

import asyncio
import random
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import httpx
import structlog

from aiplatform.model.types import ProviderError

log = structlog.get_logger("provider")
RETRYABLE_STATUS = {408, 429, 500, 502, 503, 504}


@asynccontextmanager
async def open_with_retry(
    http: httpx.AsyncClient, method: str, url: str, *, retries: int, **kwargs: Any
) -> AsyncIterator[httpx.Response]:
    attempt = 0
    while True:
        try:
            req = http.build_request(method, url, **kwargs)
            resp = await http.send(req, stream=True)
        except (httpx.ConnectError, httpx.ConnectTimeout, httpx.RemoteProtocolError, httpx.PoolTimeout) as e:
            if attempt >= retries:
                raise ProviderError(
                    f"model endpoint unreachable ({type(e).__name__}); is the model runtime running?", retryable=True
                ) from e
            await _backoff(attempt, str(e))
            attempt += 1
            continue
        except httpx.ReadTimeout as e:
            raise ProviderError("model did not start answering in time (first-token timeout)", retryable=True) from e
        if resp.status_code >= 400:
            body = (await resp.aread())[:500].decode("utf-8", "replace")
            await resp.aclose()
            if resp.status_code in RETRYABLE_STATUS and attempt < retries:
                await _backoff(attempt, f"HTTP {resp.status_code}")
                attempt += 1
                continue
            raise ProviderError(
                f"model endpoint returned HTTP {resp.status_code}: {body}",
                status=resp.status_code,
                retryable=resp.status_code in RETRYABLE_STATUS,
            )
        try:
            yield resp
        except httpx.ReadTimeout as e:
            raise ProviderError("model stream stalled (idle timeout)") from e
        except httpx.HTTPError as e:
            raise ProviderError(f"model stream failed: {type(e).__name__}") from e
        finally:
            await resp.aclose()
        return


async def _backoff(attempt: int, why: str) -> None:
    delay = min(8.0, 0.5 * 2**attempt) * (0.5 + random.random())  # noqa: S311 - jitter, not crypto
    log.warning("provider_retry", attempt=attempt + 1, delay_s=round(delay, 2), reason=why[:200])
    await asyncio.sleep(delay)
