import fakeredis
import pytest
import redis

from claimlens.storage.cache import InMemoryCache, NullCache, RedisCache


def test_redis_roundtrip_and_ttl():
    r = fakeredis.FakeRedis()
    cache = RedisCache(client=r, ttl_seconds=60)
    assert cache.get("k") is None
    cache.put("k", {"document_id": "d1", "status": "validated"})
    assert cache.get("k") == {"document_id": "d1", "status": "validated"}
    assert 0 < r.ttl("claimlens:v1:k") <= 60


class BrokenRedis:
    def get(self, *a, **k):
        raise redis.ConnectionError("down")

    def set(self, *a, **k):
        raise redis.TimeoutError("slow")


def test_cache_fails_open_when_redis_is_down():
    cache = RedisCache(client=BrokenRedis())
    cache.put("k", {"x": 1})            # must not raise
    assert cache.get("k") is None        # a miss, not a crash
    assert cache.errors == 2


def test_corrupt_cache_entry_is_just_a_miss():
    r = fakeredis.FakeRedis()
    r.set("claimlens:v1:k", b"{not json")
    assert RedisCache(client=r).get("k") is None


def test_in_memory_cache_expires():
    now = [1000.0]
    cache = InMemoryCache(ttl_seconds=10, clock=lambda: now[0])
    cache.put("k", {"a": 1})
    assert cache.get("k") == {"a": 1}
    now[0] += 11
    assert cache.get("k") is None


def test_null_cache_never_stores():
    c = NullCache()
    c.put("k", {"a": 1})
    assert c.get("k") is None


def test_real_redis_if_available():
    try:
        client = redis.Redis(port=6399, socket_connect_timeout=0.2)
        client.ping()
    except redis.RedisError:
        pytest.skip("no local redis on :6399")
    cache = RedisCache(client=client, ttl_seconds=30)
    cache.put("real", {"document_id": "d"})
    assert cache.get("real") == {"document_id": "d"}
    client.delete("claimlens:v1:real")
