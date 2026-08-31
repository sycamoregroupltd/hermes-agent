"""Event envelope for the I2 agent-state signal bus (Redis Streams paper proof).

Per R4/S1: Redis Streams is the one internal cross-process transport for
agent-state events; Kanban DB stays execution truth, Session Bus Markdown
stays human coordination/evidence. This stream is transport + short replay
buffer, not a second source of truth.
"""
from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass, field

SCHEMA_VERSION = "1"

VALID_EVENT_TYPES = {"idle", "working", "heartbeat", "done", "failed"}


@dataclass
class AgentStateEvent:
    event_type: str
    agent_id: str
    session_id: str
    task_id: str | None = None
    data: dict = field(default_factory=dict)
    schema_version: str = SCHEMA_VERSION
    producer_event_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    occurred_at: float = field(default_factory=time.time)

    def __post_init__(self):
        if self.event_type not in VALID_EVENT_TYPES:
            raise ValueError(
                f"invalid event_type {self.event_type!r}, must be one of {VALID_EVENT_TYPES}"
            )

    def to_fields(self) -> dict:
        """Flatten to a Redis XADD field map (all string values)."""
        return {
            "schema_version": self.schema_version,
            "event_type": self.event_type,
            "agent_id": self.agent_id,
            "session_id": self.session_id,
            "task_id": self.task_id or "",
            "producer_event_id": self.producer_event_id,
            "occurred_at": repr(self.occurred_at),
            "data": json.dumps(self.data, separators=(",", ":")),
        }

    @staticmethod
    def from_fields(stream_id: str, fields: dict) -> "AgentStateEvent":
        # redis-py returns bytes keys/values when decode_responses=False.
        fields = {(k.decode() if isinstance(k, bytes) else k): v for k, v in fields.items()}

        def g(k, default=""):
            v = fields.get(k, default)
            return v.decode() if isinstance(v, bytes) else v

        ev = AgentStateEvent(
            event_type=g("event_type"),
            agent_id=g("agent_id"),
            session_id=g("session_id"),
            task_id=g("task_id") or None,
            data=json.loads(g("data", "{}") or "{}"),
            schema_version=g("schema_version", SCHEMA_VERSION),
            producer_event_id=g("producer_event_id"),
            occurred_at=float(g("occurred_at", "0") or 0),
        )
        ev.stream_id = stream_id  # type: ignore[attr-defined]
        return ev
