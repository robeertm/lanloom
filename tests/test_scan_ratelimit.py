import asyncio

import pytest

from lanloom.scan.ratelimit import Limiter, TokenBucket


class FakeClock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t

    async def sleep(self, d):
        self.t += d
        await asyncio.sleep(0)


def test_bucket_rate_with_fake_clock():
    clock = FakeClock()
    bucket = TokenBucket(40, burst=1, clock=clock, sleep=clock.sleep)

    async def go():
        for _ in range(401):
            await bucket.acquire()

    asyncio.run(go())
    # first token is free (burst 1), then 400 tokens at 40/s = 10 s
    assert clock.t == pytest.approx(10.0, rel=0.01)
    assert bucket.granted == 401


def test_bucket_burst_then_rate():
    clock = FakeClock()
    bucket = TokenBucket(10, burst=5, clock=clock, sleep=clock.sleep)

    async def go():
        for _ in range(5):
            await bucket.acquire()
        assert clock.t == 0.0
        await bucket.acquire()
        assert clock.t == pytest.approx(0.1)

    asyncio.run(go())


def test_bucket_rejects_bad_rate():
    with pytest.raises(ValueError):
        TokenBucket(0)


def test_limiter_concurrency_caps():
    async def go():
        lim = Limiter(rate=1000, concurrency=6, per_host=2)

        async def job(host):
            async with lim.slot(host):
                await asyncio.sleep(0.01)

        await asyncio.gather(*(job(f"192.168.1.{i % 5}") for i in range(60)))
        return lim

    lim = asyncio.run(go())
    assert lim.max_active_global <= 6
    assert lim.max_active_host <= 2
    assert lim.probes == 60


def test_limiter_releases_on_error_and_cancel():
    async def go():
        lim = Limiter(rate=1000, concurrency=2, per_host=1)

        async def boom():
            async with lim.slot("h"):
                raise RuntimeError("x")

        with pytest.raises(RuntimeError):
            await boom()

        async def hang():
            async with lim.slot("h"):
                await asyncio.sleep(10)

        t = asyncio.create_task(hang())
        await asyncio.sleep(0.01)
        t.cancel()
        with pytest.raises(asyncio.CancelledError):
            await t
        # slot must be free again
        async with lim.slot("h"):
            pass
        return lim

    asyncio.run(asyncio.wait_for(go(), 2))


def test_limiter_rate_is_bounded():
    """30 acquisitions at 100/s (burst 10) through the full limiter take 0.2 s (fake clock, no wall-clock limit)."""
    clock = FakeClock()

    async def go():
        lim = Limiter(rate=100, concurrency=24, per_host=24, clock=clock, sleep=clock.sleep)

        async def job():
            async with lim.slot("h"):
                pass

        await asyncio.gather(*(job() for _ in range(30)))
        return lim

    lim = asyncio.run(go())
    assert clock.t == pytest.approx(0.2, abs=0.005)
    assert lim.probes == 30
