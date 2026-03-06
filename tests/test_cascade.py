"""
Tests for cascade orchestration logic.
"""

import asyncio
import pytest
from glide.cascade import TTFTTimeoutError, _first_token_timeout


# -- TTFT timeout helper --

async def fast_gen():
    yield b"token"

async def slow_gen(delay=10.0):
    await asyncio.sleep(delay)
    yield b"token"


@pytest.mark.asyncio
async def test_first_token_within_budget():
    chunk, ttft = await _first_token_timeout(fast_gen(), budget=5.0)
    assert chunk == b"token"
    assert ttft < 1.0


@pytest.mark.asyncio
async def test_first_token_no_budget():
    chunk, ttft = await _first_token_timeout(fast_gen(), budget=None)
    assert chunk == b"token"


@pytest.mark.asyncio
async def test_first_token_exceeds_budget():
    with pytest.raises(TTFTTimeoutError):
        await _first_token_timeout(slow_gen(delay=5.0), budget=0.1)
