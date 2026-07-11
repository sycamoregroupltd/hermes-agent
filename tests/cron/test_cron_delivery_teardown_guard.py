"""Verify the delivery-path guarded submit helper (_safe_submit_threadpool).

The standalone delivery fallback (used when no live adapter is available)
builds a throwaway ``ThreadPoolExecutor`` and submits ``asyncio.run`` to it.
During interpreter teardown that ``submit`` raises "cannot schedule new
futures after interpreter shutdown" — the SAME class of error as the
2026-07-10 06:39 fleet outage. _safe_submit_threadpool swallows it and
returns None so the caller can skip that target instead of letting the
RuntimeError escape _deliver_result and crash the whole delivery loop
(skipping every remaining target, #47163). See t_dfd9b303.
"""

from __future__ import annotations

import concurrent.futures

import pytest


def _load():
    import cron.scheduler as sched

    return sched


def test_safe_submit_threadpool_returns_future_normally():
    sched = _load()
    pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    try:
        fut = sched._safe_submit_threadpool(pool, lambda: 7)
        assert fut is not None
        assert fut.result() == 7
    finally:
        pool.shutdown(wait=True)


def test_safe_submit_threadpool_swallows_interpreter_shutdown():
    sched = _load()
    pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    try:

        def _boom(*a, **k):
            raise RuntimeError("cannot schedule new futures after interpreter shutdown")

        # Patch the instance's submit to simulate teardown.
        pool.submit = _boom  # type: ignore[assignment]
        # Must return None (not raise) so the delivery caller can skip.
        assert sched._safe_submit_threadpool(pool, lambda: 1) is None
    finally:
        pool.shutdown(wait=True)


def test_safe_submit_threadpool_reraises_other_runtime_error():
    sched = _load()
    pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    try:

        def _boom(*a, **k):
            raise RuntimeError("some other crash")

        pool.submit = _boom  # type: ignore[assignment]
        with pytest.raises(RuntimeError):
            sched._safe_submit_threadpool(pool, lambda: 1)
    finally:
        pool.shutdown(wait=True)
