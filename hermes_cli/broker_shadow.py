"""Canonical, bounded Hermes broker pass in shadow mode only.

This is a service *wrapper*, not a daemon.  It performs exactly one explicit
native broker pass when called by an already-authorised host.  It neither
schedules itself nor starts/resumes providers, sends notifications, or mutates
task terminal state.  Its sole durable outputs are the native route-decision
events emitted by :func:`hermes_cli.kanban_db.run_native_broker_pass`.
"""

from __future__ import annotations

from dataclasses import dataclass

from . import kanban_db as kb


CANONICAL_SHADOW_CONSUMER = "hermes-shadow-broker-v1"
SHADOW_BROKER_MAX_LIMIT = 32


class ShadowBrokerDisabled(kb.BrokerUnsafeError):
    """Shadow service wrapper is opt-in; no ambient activation exists."""


@dataclass(frozen=True)
class ShadowBrokerReceipt:
    """Bounded summary suitable for a control-plane health projection."""

    consumer: str
    old_cursor: int
    new_cursor: int
    folded_run_ids: tuple[int, ...]
    routes: tuple[str, ...]
    notifications: tuple[str, ...]


class CanonicalShadowBroker:
    """The only supported shadow consumer identity for this rollout.

    A token is deliberately supplied by the caller rather than read from an
    environment variable or config file.  This keeps the source default inert
    and avoids coupling token/credential handling to the broker core.
    """

    consumer = CANONICAL_SHADOW_CONSUMER

    def __init__(self, *, enabled: bool = False, limit: int = 8) -> None:
        if isinstance(limit, bool) or not isinstance(limit, int) or not (1 <= limit <= SHADOW_BROKER_MAX_LIMIT):
            raise ValueError(
                f"limit must be an integer in [1, {SHADOW_BROKER_MAX_LIMIT}]"
            )
        self.enabled = bool(enabled)
        self.limit = limit

    def run_once(self, conn, *, token: str) -> ShadowBrokerReceipt:
        if not self.enabled:
            raise ShadowBrokerDisabled("canonical shadow broker is disabled")
        if not isinstance(token, str) or not token.strip():
            raise kb.BrokerAuthError("shadow broker requires a non-empty consumer token")
        # The canonical identity is provisioned out-of-band.  This wrapper
        # must never let its first enabled caller claim that identity by
        # implicitly creating/binding the native subscription row.
        row = conn.execute(
            "SELECT token_sha256 FROM kanban_broker_subs WHERE consumer = ?",
            (self.consumer,),
        ).fetchone()
        if row is None or not row["token_sha256"]:
            raise kb.BrokerAuthError(
                "canonical shadow consumer is not pre-provisioned with a token"
            )
        kb._authenticate_consumer(conn, self.consumer, token)
        result = kb.run_native_broker_pass(
            conn,
            consumer=self.consumer,
            token=token,
            limit=self.limit,
        )
        return ShadowBrokerReceipt(
            consumer=self.consumer,
            old_cursor=result.old_cursor,
            new_cursor=result.new_cursor,
            folded_run_ids=result.folded_run_ids,
            routes=tuple(decision.route for decision in result.decisions),
            notifications=tuple(
                projection.text for projection in result.notifications
            ),
        )
