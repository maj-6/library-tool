import pytest


TITLE = "Fauna and Flora of the Bible"
AUTHOR = "United Bible Societies"
SOURCES = "cprs"
MISS = {"found": False, "sources": [], "match": None}


def _found(year):
    return {
        "found": True,
        "sources": ["cprs"],
        "match": {
            "source": "cprs",
            "reg_number": f"TX-{year}",
            "title": TITLE,
            "author": AUTHOR,
            "year": str(year),
            "record_id": f"voyager-{year}",
        },
    }


def _lookup(client, year=1980):
    return client.get("/api/copyright/registration", query_string={
        "title": TITLE,
        "author": AUTHOR,
        "year": str(year),
        "sources": SOURCES,
    })


@pytest.fixture()
def registration_server(monkeypatch, tmp_path):
    import server

    monkeypatch.setattr(
        server, "_REG_CACHE_PATH", tmp_path / "copyright_reg.json")
    monkeypatch.setattr(server, "_reg_cache", {})
    return server


def test_registration_cache_key_is_versioned_and_year_distinct(
        client, monkeypatch, registration_server):
    calls = []

    def lookup(_title, _author, year, _sources):
        calls.append(str(year))
        return _found(year)

    monkeypatch.setattr(
        registration_server.copyreg, "registration_lookup", lookup)

    assert _lookup(client, 1980).get_json()["match"]["year"] == "1980"
    assert _lookup(client, 1981).get_json()["match"]["year"] == "1981"
    assert _lookup(client, 1980).get_json()["match"]["year"] == "1980"

    assert calls == ["1980", "1981"]
    keys = list(registration_server._reg_cache)
    assert len(keys) == 2
    assert all(
        f"v{registration_server._REG_CACHE_VERSION}" in key for key in keys)
    assert any("|1980|" in key for key in keys)
    assert any("|1981|" in key for key in keys)


def test_registration_cache_bypasses_legacy_exact_title_miss(
        client, monkeypatch, registration_server):
    legacy_key = "fauna and flora of the bible|united bible societies|cprs"
    registration_server._reg_cache[legacy_key] = MISS
    calls = []

    def lookup(*_args):
        calls.append(True)
        return _found(1980)

    monkeypatch.setattr(
        registration_server.copyreg, "registration_lookup", lookup)

    response = _lookup(client, 1980)

    assert response.status_code == 200
    assert response.get_json()["match"]["reg_number"] == "TX-1980"
    assert calls == [True]


def test_registration_source_failure_is_not_cached(
        client, monkeypatch, registration_server):
    calls = []

    def unavailable(*_args):
        calls.append(True)
        raise registration_server.copyreg.RegistrationLookupError("CPRS offline")

    monkeypatch.setattr(
        registration_server.copyreg, "registration_lookup", unavailable)

    first = _lookup(client, 1980)
    second = _lookup(client, 1980)

    assert first.status_code == 503
    assert second.status_code == 503
    assert calls == [True, True]
    assert registration_server._reg_cache == {}


def test_negative_registration_cache_entry_expires(
        client, monkeypatch, registration_server):
    now = [10_000.0]
    calls = []

    monkeypatch.setattr(registration_server, "_REG_NEGATIVE_TTL", 60.0)
    monkeypatch.setattr(registration_server.time, "time", lambda: now[0])

    def no_match(*_args):
        calls.append(True)
        return MISS

    monkeypatch.setattr(
        registration_server.copyreg, "registration_lookup", no_match)

    assert _lookup(client).get_json() == MISS
    now[0] += 59
    assert _lookup(client).get_json() == MISS
    assert calls == [True]

    now[0] += 2
    assert _lookup(client).get_json() == MISS
    assert calls == [True, True]
