"""A Kanban worker whose owner retains the admission but cannot settle its
outcome (receipt status 'unknown') must NOT book a crash against the card.

Before the fix, ``_submit_claim`` raised ``GatewayClientError('Kanban execution
unknown; retained by owner')`` (a ``ValueError`` subclass) which
``_submit()`` caught and printed as ``Kanban owner refused: ...``, exiting 1.
That spent the card's failure budget on a host condition, not a card condition,
and cycled the card blocked<->ready.  The owner retains the admission and the
board's dead-worker sweep / TTL reclaim reconcile it later.
"""

import asyncio
from contextlib import asynccontextmanager

import pytest

from hermes_cli import gateway_client, kanban_worker_client


def _owner_with_unknown_receipt(submitted):
    @asynccontextmanager
    async def connect():
        class _Client:
            events = asyncio.Queue()

            async def rpc(self, method, **params):
                submitted.append(method)
                if method == "kanban.run":
                    return {"session_id": "s1", "receipt": {"status": "unknown", "admission_id": "a1"}}
                if method == "prompt.receipt":
                    return {"status": "unknown", "admission_id": "a1"}
                return {}

        yield _Client()

    return connect


@pytest.mark.asyncio
async def test_worker_does_not_crash_when_receipt_is_unknown(monkeypatch, tmp_path):
    submitted = []
    monkeypatch.setattr(gateway_client, "connect_gateway", _owner_with_unknown_receipt(submitted))
    monkeypatch.delenv("HERMES_TUI_GATEWAY_URL", raising=False)

    rc = await kanban_worker_client.run({"task_id": "t1"}, str(tmp_path / "kanban.db"))
    # The owner retained the admission; this must not be a crash.
    assert rc == 0, "unknown receipt must not book a crash (rc=1)"
    assert "kanban.run" in submitted


@pytest.mark.asyncio
async def test_worker_still_crashes_on_genuine_refusal(monkeypatch, tmp_path):
    """A non-unknown GatewayClientError (e.g. connection failure) still fails."""
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def connect():
        raise gateway_client.GatewayClientError("Gateway connection failed; no local fallback")
        yield  # unreachable

    monkeypatch.setattr(gateway_client, "connect_gateway", connect)
    monkeypatch.delenv("HERMES_TUI_GATEWAY_URL", raising=False)

    with pytest.raises(gateway_client.GatewayClientError):
        await kanban_worker_client.run({"task_id": "t1"}, str(tmp_path / "kanban.db"))
