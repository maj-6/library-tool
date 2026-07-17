"""Resumability of the online-availability pass in build_catalog_report.

The bug (issue #114): every processed row decremented the rate-limit budget and
slept — including cache hits — so a resumed ``--limit`` run re-spent budget on the
already-cached leading rows and never advanced, and the cache was written only
once at the very end, so any interruption discarded completed lookups.

These tests exercise the budget/cache/save decision path with the network mocked,
so they are deterministic and never touch archive.org.
"""
from __future__ import annotations

import json

import pytest

import build_catalog_report as bcr


# --- helpers ---------------------------------------------------------------

def _key(title: str, author: str) -> str:
    """The exact cache key check_online derives for a (title, author)."""
    return bcr.whl._normalize(title) + "|" + bcr.whl._normalize(author)


class _FakeResp:
    def __init__(self, body: bytes):
        self._body = body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return self._body


def _mock_network(monkeypatch, *, num_found=1, raises=False):
    """Patch urlopen; return a list that records one entry per real call."""
    calls: list = []

    def _open(req, timeout=None):
        calls.append(getattr(req, "full_url", req))
        if raises:
            raise OSError("simulated transient network failure")
        return _FakeResp(json.dumps({"response": {"numFound": num_found}}).encode())

    monkeypatch.setattr(bcr.urllib.request, "urlopen", _open)
    return calls


# --- _cache_entry coercion -------------------------------------------------

def test_cache_entry_coerces_legacy_and_dict_shapes():
    assert bcr._cache_entry("yes") == ("yes", None)
    assert bcr._cache_entry("no") == ("no", None)
    assert bcr._cache_entry("error") == ("error", None)
    assert bcr._cache_entry({"status": "no", "ts": 123.0}) == ("no", 123.0)
    assert bcr._cache_entry({"status": "error", "ts": None}) == ("error", None)
    # Unrecognised shapes coerce to a stale error so they get re-checked.
    assert bcr._cache_entry("garbage") == ("error", None)
    assert bcr._cache_entry({"status": "weird"}) == ("error", None)
    assert bcr._cache_entry(42) == ("error", None)


# --- check_online primitive ------------------------------------------------

def test_cached_yes_resolves_free(monkeypatch):
    calls = _mock_network(monkeypatch)
    cache = {_key("A Title", "Smith"): {"status": "yes", "ts": 1.0}}
    status, did = bcr.check_online("A Title", "Smith", cache)
    assert (status, did) == ("yes", False)
    assert calls == []  # never hit the network for a cached answer


def test_legacy_bare_yes_resolves_free(monkeypatch):
    calls = _mock_network(monkeypatch)
    cache = {_key("A Title", "Smith"): "yes"}  # legacy bare-string cache
    status, did = bcr.check_online("A Title", "Smith", cache)
    assert (status, did) == ("yes", False)
    assert calls == []


def test_uncached_hits_network_and_stores_timestamped_entry(monkeypatch):
    calls = _mock_network(monkeypatch, num_found=3)
    cache: dict = {}
    status, did = bcr.check_online("Fresh Book", "Jones", cache)
    assert (status, did) == ("yes", True)
    assert len(calls) == 1
    entry = cache[_key("Fresh Book", "Jones")]
    assert entry["status"] == "yes" and isinstance(entry["ts"], float)


def test_zero_hits_records_no(monkeypatch):
    _mock_network(monkeypatch, num_found=0)
    cache: dict = {}
    assert bcr.check_online("Nowhere", "", cache) == ("no", True)


def test_network_error_records_error(monkeypatch):
    _mock_network(monkeypatch, raises=True)
    cache: dict = {}
    status, did = bcr.check_online("Boom", "", cache)
    assert (status, did) == ("error", True)
    assert cache[_key("Boom", "")]["status"] == "error"


def test_disallowed_request_returns_not_checked(monkeypatch):
    calls = _mock_network(monkeypatch)
    cache: dict = {}
    status, did = bcr.check_online("Uncached", "", cache, allow_request=False)
    assert (status, did) == ("not checked", False)
    assert calls == []  # budget exhausted → never touch the network


def test_fresh_error_within_ttl_not_retried(monkeypatch):
    calls = _mock_network(monkeypatch)
    ts = _now_via(bcr)
    cache = {_key("Flaky", ""): {"status": "error", "ts": ts}}
    status, did = bcr.check_online("Flaky", "", cache)
    assert (status, did) == ("error", False)
    assert calls == []


def test_stale_error_is_retried(monkeypatch):
    calls = _mock_network(monkeypatch, num_found=1)
    ts = _now_via(bcr) - bcr.ERROR_RETRY_TTL - 100
    cache = {_key("Flaky", ""): {"status": "error", "ts": ts}}
    status, did = bcr.check_online("Flaky", "", cache)
    assert (status, did) == ("yes", True)  # retried and succeeded this time
    assert len(calls) == 1


def test_legacy_bare_error_is_retried(monkeypatch):
    calls = _mock_network(monkeypatch, num_found=0)
    cache = {_key("Flaky", ""): "error"}  # no timestamp → always eligible to retry
    status, did = bcr.check_online("Flaky", "", cache)
    assert (status, did) == ("no", True)
    assert len(calls) == 1


# --- resolve_online orchestration ------------------------------------------

def test_budget_spent_and_saved_only_on_real_requests(monkeypatch):
    calls = _mock_network(monkeypatch, num_found=1)
    saves: list[int] = []
    pairs = [("t1", "a"), ("t2", "b"), ("t3", "c"), ("t4", "d")]
    cache: dict = {}
    flags = bcr.resolve_online(
        pairs, cache, limit=2, sleep_s=0.0, save=lambda c: saves.append(len(c))
    )
    # First two rows spend the budget; the rest are "not checked", untouched.
    assert flags == ["yes", "yes", "not checked", "not checked"]
    assert len(calls) == 2          # only genuine requests hit the network
    assert len(saves) == 2          # cache flushed once per real request, not per row


def test_successive_limited_runs_advance_through_uncached_rows(monkeypatch):
    calls = _mock_network(monkeypatch, num_found=1)
    pairs = [("t1", "a"), ("t2", "b"), ("t3", "c"), ("t4", "d")]
    cache: dict = {}

    run1 = bcr.resolve_online(pairs, cache, limit=2, sleep_s=0.0)
    assert run1 == ["yes", "yes", "not checked", "not checked"]
    assert len(calls) == 2

    # Second run: the first two are cached (free), so the budget now advances to
    # the rows the first run never reached — the resumability the issue demands.
    run2 = bcr.resolve_online(pairs, cache, limit=2, sleep_s=0.0)
    assert run2 == ["yes", "yes", "yes", "yes"]
    assert len(calls) == 4  # exactly two additional network calls


def test_unlimited_processes_every_uncached_pair(monkeypatch):
    calls = _mock_network(monkeypatch, num_found=1)
    pairs = [(f"t{i}", "a") for i in range(5)]
    flags = bcr.resolve_online(pairs, {}, limit=0, sleep_s=0.0)
    assert flags == ["yes"] * 5
    assert len(calls) == 5  # limit 0 == unlimited


def test_interruption_preserves_completed_lookups_on_disk(monkeypatch):
    _mock_network(monkeypatch, num_found=1)
    pairs = [("t1", "a"), ("t2", "b"), ("t3", "c"), ("t4", "d")]
    cache: dict = {}

    # Only two of four resolve before "interruption"; save writes to the real
    # (throwaway-DATA_ROOT) cache path after each request.
    bcr.resolve_online(
        pairs, cache, limit=2, sleep_s=0.0,
        save=lambda c: bcr.lib.save_json(bcr.ONLINE_CACHE, c),
    )
    on_disk = bcr.lib.load_json(bcr.ONLINE_CACHE, {})
    assert len(on_disk) == 2                       # the two completed lookups survived
    assert set(on_disk) == {_key("t1", "a"), _key("t2", "b")}
    assert all(v["status"] == "yes" for v in on_disk.values())


def _now_via(mod) -> float:
    """Read the module's own clock so ts math matches check_online exactly."""
    return mod.time.time()


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
