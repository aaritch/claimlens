"""Redis fast path: "have we already handled this exact document under this exact recipe?"

Analogy: a sticky note on the front desk. If the note says "already done, file #123", the clerk skips
the trip to the archive. If the note is missing or wrong the clerk simply checks the archive: the
sticky note is a speed-up, NEVER the source of truth. So every failure here is swallowed ("fail open").

Privacy: the cache holds only {document_id, status}, never extracted values.
"""
from __future__ import annotations

import json
import logging
import time
from typing import Callable, Optional, Protocol

import redis

log = logging.getLogger(__name__)


class ResultCache(Protocol):
    def get(self, key: str) -> Optional[dict]: ...
    def put(self, key: str, value: dict) -> None: ...


class NullCache:
    def get(self, key):
        return None

    def put(self, key, value):
        pass


class InMemoryCache:
    def __init__(self, ttl_seconds: int = 3600, clock: Callable[[], float] = time.time):
        self._data: dict[str, tuple[float, dict]] = {}
        self._ttl, self._clock = ttl_seconds, clock

    def get(self, key):
        hit = self._data.get(key)
        if not hit or hit[0] < self._clock():
            self._data.pop(key, None)
            return None
        return hit[1]

    def put(self, key, value):
        self._data[key] = (self._clock() + self._ttl, value)


class RedisCache:
    PREFIX = "claimlens:v1:"

    def __init__(self, url: str = "", ttl_seconds: int = 86400, client=None):
        self._r = client or redis.Redis.from_url(url, socket_timeout=0.25, socket_connect_timeout=0.25)
        self._ttl = ttl_seconds
        self.errors = 0

    def get(self, key):
        try:
            raw = self._r.get(self.PREFIX + key)
            return json.loads(raw) if raw else None
        except (redis.RedisError, ValueError) as exc:
            self._note(exc)
            return None

    def put(self, key, value):
        try:
            self._r.set(self.PREFIX + key, json.dumps(value), ex=self._ttl)
        except redis.RedisError as exc:
            self._note(exc)

    def _note(self, exc: Exception) -> None:
        self.errors += 1
        log.warning("redis cache unavailable (%s); continuing without it", exc.__class__.__name__)  # class only
