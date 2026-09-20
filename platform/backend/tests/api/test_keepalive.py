from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator

from aiplatform.api.openai_compat import KEEPALIVE, with_keepalive


async def _slow(closed: list[bool], gap: float = 0.25) -> AsyncGenerator[bytes]:
    try:
        yield b"first"
        await asyncio.sleep(gap)  # model loading / prefill / tool running: nothing to send
        yield b"second"
    finally:
        closed.append(True)


async def test_quiet_source_gets_keepalives_and_all_data_arrives_in_order() -> None:
    closed: list[bool] = []
    out = [b async for b in with_keepalive(_slow(closed), 0.05)]
    data = [b for b in out if b != KEEPALIVE]
    assert data == [b"first", b"second"]
    assert out.count(KEEPALIVE) >= 2 and out.index(KEEPALIVE) > out.index(b"first")
    assert closed == [True]


async def test_busy_source_gets_no_keepalives() -> None:
    async def fast() -> AsyncGenerator[bytes]:
        for i in range(3):
            yield str(i).encode()

    assert [b async for b in with_keepalive(fast(), 5)] == [b"0", b"1", b"2"]


async def test_early_client_disconnect_stops_the_source() -> None:
    closed: list[bool] = []
    gen = with_keepalive(_slow(closed, gap=30), 0.05)
    assert await anext(gen) == b"first"
    assert await anext(gen) == KEEPALIVE  # source is now blocked mid-step; the client then goes away
    await gen.aclose()
    assert closed == [True]


async def test_source_error_propagates() -> None:
    async def boom() -> AsyncGenerator[bytes]:
        yield b"x"
        raise RuntimeError("provider blew up")
        yield b""  # pragma: no cover

    gen = with_keepalive(boom(), 5)
    assert await anext(gen) == b"x"
    try:
        await anext(gen)
    except RuntimeError as e:
        assert "provider blew up" in str(e)
    else:
        raise AssertionError("error was swallowed")
