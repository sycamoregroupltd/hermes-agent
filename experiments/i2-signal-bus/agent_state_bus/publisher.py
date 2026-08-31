"""Publisher side of the I2 agent-state signal bus."""
from __future__ import annotations

import os

import redis

from .schema import AgentStateEvent

STREAM_KEY = os.environ.get("I2_BUS_STREAM", "hermes:agent-state:v1")
MAXLEN = int(os.environ.get("I2_BUS_MAXLEN", "10000"))


def connect(url: str | None = None) -> "redis.Redis":
    url = url or os.environ.get("I2_BUS_REDIS_URL", "redis://127.0.0.1:6479/0")
    return redis.Redis.from_url(url, decode_responses=False)


class AgentStateBus:
    def __init__(self, r: "redis.Redis | None" = None, stream_key: str = STREAM_KEY):
        self.r = r or connect()
        self.stream_key = stream_key

    def publish(self, event: AgentStateEvent) -> str:
        stream_id = self.r.xadd(
            self.stream_key,
            event.to_fields(),
            maxlen=MAXLEN,
            approximate=True,
        )
        return stream_id.decode() if isinstance(stream_id, bytes) else stream_id
