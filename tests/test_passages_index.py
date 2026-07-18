"""Structure-aware passages and the versioned search index (issue #140):
segmentation units, the passages.json artifact + provenance, curation
semantics, and the index publish / rollback flow against a mocked cloud.

conftest.py points WHL_DATA_ROOT at a throwaway directory before any tools
module is imported, so nothing here touches live data.
"""
from __future__ import annotations

from itertools import groupby

import pytest

import server


# --- fixtures ---------------------------------------------------------------------

def _ready_build(client, title: str, **fields) -> dict:
    r = client.post("/api/builds", json={"build": {
        "title": title, "status": "ready"}})
    assert r.status_code == 200
    build = r.get_json()["build"]
    if fields:
        r = client.patch(f"/api/builds/{build['id']}", json=fields)
        assert r.status_code == 200
        build = r.get_json()["build"]
    return build


def _write_compiled(bid: str, pages: dict[int, str]) -> None:
    d = server._entry_dir(bid) / "ocr"
    d.mkdir(parents=True, exist_ok=True)
    (d / "compiled.txt").write_text(
        "\n\n".join(f"--- page {n} ---\n{pages[n]}" for n in sorted(pages)),
        encoding="utf-8")


def _inline_jobs(monkeypatch) -> None:
    """Run analyze-style jobs on the calling thread, so completion has
    happened when the POST returns (the test_provenance_manifest pattern)."""
    def run_inline(bid, kind, total, target, decorate=None):
        job = server._an_job_new(bid, kind, total)
        if decorate is not None:
            decorate(job)
        target(job)
        return job
    monkeypatch.setattr(server, "_an_job_start", run_inline)


def _sentences(prefix: str, count: int, words: int = 12) -> str:
    """`count` sentences of `words` whitespace tokens, each capitalized and
    period-terminated so the splitter's heuristic fires on every boundary."""
    return " ".join(
        f"{prefix}{i} " + " ".join(f"w{i}x{j}" for j in range(words - 1)) + "."
        for i in range(count))


class _CloudMock:
    """A recording sbase._rest double for the index tables."""

    def __init__(self):
        self.calls: list = []
        self.versions: list = []

    def rest(self, cfg, method, path, payload=None, prefer=""):
        self.calls.append((method, path, payload, prefer))
        if method == "POST" and path.startswith("index_versions"):
            row = dict(payload[0], id=f"ver-{len(self.versions) + 1}",
                       built_at="2026-07-17T00:00:00+00:00")
            self.versions.append(row)
            return [row]
        if method == "POST" and path.startswith("passages"):
            return None
        if method == "GET" and path.startswith("index_versions"):
            return list(reversed(self.versions))     # newest first
        if method == "DELETE" and path.startswith("index_versions"):
            return None
        raise AssertionError(path)


@pytest.fixture()
def cloud(monkeypatch):
    mock = _CloudMock()
    monkeypatch.setattr(server, "_cloud_cfg",
                        lambda: {"url": "https://cloud.test", "key": "svc"})
    monkeypatch.setattr(server.sbase, "_rest", mock.rest)
    return mock


@pytest.fixture()
def lexical(monkeypatch):
    """No embeddings provider configured — indexes stay lexical-only."""
    monkeypatch.setattr(server, "_embed_cfg",
                        lambda: {"base": "", "model": "", "key": ""})


def _published_build(client, title: str, rights: str = "public-domain") -> str:
    build = _ready_build(client, title)
    bid = build["id"]
    _write_compiled(bid, {1: _sentences("Alpha", 20), 2: _sentences("Beta", 20)})
    r = client.patch(f"/api/builds/{bid}", json={
        "status": "uploaded", "published_slug": f"slug-{bid}", "rights": rights})
    assert r.status_code == 200
    return bid


# --- segmentation units -----------------------------------------------------------

def test_segment_respects_bounds_sentences_and_page_anchors():
    pages = {
        1: _sentences("Alpha", 6) + "\n\n" + _sentences("Beta", 6),
        2: _sentences("Gamma", 40),          # one oversized paragraph
        3: _sentences("Delta", 6),
    }
    out = server._segment_passages(pages, {})
    assert out
    for p in out:
        assert len(p["text"].split()) <= 350          # the hard cap holds
        assert p["text"].rstrip().endswith(".")       # sentences stay whole
        assert p["body"] == server._search_normalize(p["text"])
        assert p["page_from"] <= p["page_to"]
    # nothing lost, nothing doubled
    joined = " ".join(p["text"] for p in out)
    for marker in ("Alpha0", "Beta5", "Gamma0", "Gamma39", "Delta0"):
        assert joined.count(marker) == 1
    # page anchors: the first passage sits on page 1; the last spans 2 -> 3
    # because packing crosses page boundaries (that is the point)
    assert (out[0]["page_from"], out[0]["page_to"]) == (1, 1)
    assert (out[-1]["page_from"], out[-1]["page_to"]) == (2, 3)


def test_segment_is_deterministic_with_stable_ids():
    pages = {1: _sentences("Alpha", 25) + "\n\n" + _sentences("Beta", 3),
             2: _sentences("Gamma", 40)}
    a = server._segment_passages(pages, {})
    b = server._segment_passages(pages, {})
    assert a == b
    ids = [p["id"] for p in a]
    assert len(set(ids)) == len(ids)
    assert all(len(i) == 16 for i in ids)


def test_segment_groups_consecutive_children_into_parent_sections():
    pages = {1: "\n\n".join(_sentences(f"Para{k}A", 15) for k in range(12))}
    out = server._segment_passages(pages, {})
    parents = [p["parent_id"] for p in out]
    runs = [k for k, _ in groupby(parents)]
    assert len(runs) == len(set(parents)) == 2        # consecutive, two groups
    assert parents[0] == "p" + out[0]["id"]
    for _, grp in groupby(out, key=lambda p: p["parent_id"]):
        assert sum(len(p["text"].split()) for p in grp) <= 1200


def test_segment_honors_a_custom_recipe():
    pages = {1: _sentences("Alpha", 30)}
    out = server._segment_passages(pages, {
        "child_min": 10, "child_max": 30, "parent_min": 40, "parent_max": 60})
    assert len(out) > 1
    assert all(len(p["text"].split()) <= 30 for p in out)


# --- the artifact, provenance, and staleness --------------------------------------

def test_segment_job_writes_artifact_manifest_and_staleness(client, monkeypatch):
    build = _ready_build(client, "Passages provenance")
    bid = build["id"]
    _write_compiled(bid, {1: _sentences("Alpha", 20), 2: _sentences("Beta", 20)})
    src_sha = server._file_sha256(server._entry_dir(bid) / "ocr" / "compiled.txt")
    _inline_jobs(monkeypatch)

    r = client.post("/api/knowledge/segment", json={"build_id": bid})
    assert r.status_code == 200 and r.get_json()["ok"]

    doc = server._load_passages(bid)
    assert doc["version"] == 1
    assert doc["generated_from"] == {"doc": "compiled.txt", "sha256": src_sha}
    assert doc["recipe"] == server._passage_recipe(None)
    assert doc["excluded"] == [] and doc["passages"]

    row = server._load_manifest(bid)["artifacts"]["passages.json"]
    assert row["produced_by"]["kind"] == "segment"
    assert row["produced_by"]["recipe"] == server._passage_recipe(None)
    assert row["inputs"] == [{"artifact": "ocr/compiled.txt",
                              "sha256": src_sha}]

    st = client.get(f"/api/builds/{bid}/passages").get_json()["state"]
    assert st["exists"] is True and st["stale"] is False
    assert st["count"] == len(doc["passages"])

    # correcting the OCR text marks the artifact outdated (#135 + hash)
    r = client.post(f"/api/builds/{bid}/ocr", json={
        "name": "compiled.txt",
        "text": "--- page 1 ---\nAn entirely new reading.\n"})
    assert r.status_code == 200
    st = client.get(f"/api/builds/{bid}/passages").get_json()["state"]
    assert st["stale"] is True


def test_segment_requires_a_verified_entry(client):
    r = client.post("/api/builds", json={"build": {"title": "Draft passages"}})
    bid = r.get_json()["build"]["id"]
    res = client.post("/api/knowledge/segment", json={"build_id": bid})
    assert res.status_code == 400
    assert "verified" in res.get_json()["error"]


# --- curation ---------------------------------------------------------------------

def test_curation_exclude_split_merge_and_manifest_rerecord(client, monkeypatch):
    build = _ready_build(client, "Curation")
    bid = build["id"]
    _write_compiled(bid, {1: _sentences("Alpha", 20), 2: _sentences("Beta", 20)})
    _inline_jobs(monkeypatch)
    client.post("/api/knowledge/segment", json={"build_id": bid})
    doc = client.get(f"/api/builds/{bid}/passages").get_json()["doc"]
    first = doc["passages"][0]
    n = len(doc["passages"])

    # exclude + include round-trip, persisted in the artifact
    r = client.patch(f"/api/builds/{bid}/passages",
                     json={"exclude": [first["id"]]})
    assert r.get_json()["doc"]["excluded"] == [first["id"]]
    assert server._load_passages(bid)["excluded"] == [first["id"]]
    r = client.patch(f"/api/builds/{bid}/passages",
                     json={"include": [first["id"]]})
    assert r.get_json()["doc"]["excluded"] == []

    # split: at the middle sentence boundary, tokens preserved verbatim
    r = client.patch(f"/api/builds/{bid}/passages",
                     json={"split": {"id": first["id"]}})
    out = r.get_json()["doc"]
    assert len(out["passages"]) == n + 1
    a, b = out["passages"][0], out["passages"][1]
    assert a["parent_id"] == b["parent_id"] == first["parent_id"]
    assert a["text"].rstrip().endswith(".")
    assert (a["text"] + " " + b["text"]).split() == first["text"].split()

    # curation is a manual edit; the recorded inputs stay for staleness
    row = server._load_manifest(bid)["artifacts"]["passages.json"]
    assert row["produced_by"] == {"kind": "manual-edit"}
    assert row["inputs"] and row["inputs"][0]["artifact"] == "ocr/compiled.txt"

    # merge rejoins the halves (same parent required)
    r = client.patch(f"/api/builds/{bid}/passages",
                     json={"merge": {"id": a["id"]}})
    out = r.get_json()["doc"]
    assert len(out["passages"]) == n
    assert out["passages"][0]["text"].split() == first["text"].split()


def test_curation_rejects_impossible_edits(client, monkeypatch):
    build = _ready_build(client, "Curation limits")
    bid = build["id"]
    _write_compiled(bid, {1: "One lonely sentence."})
    _inline_jobs(monkeypatch)
    client.post("/api/knowledge/segment", json={"build_id": bid})
    doc = client.get(f"/api/builds/{bid}/passages").get_json()["doc"]
    only = doc["passages"][0]["id"]

    r = client.patch(f"/api/builds/{bid}/passages", json={"split": {"id": only}})
    assert r.status_code == 400
    assert "single sentence" in r.get_json()["error"]
    r = client.patch(f"/api/builds/{bid}/passages", json={"merge": {"id": only}})
    assert r.status_code == 400
    assert "same section" in r.get_json()["error"]
    r = client.patch(f"/api/builds/{bid}/passages", json={"exclude": ["nope"]})
    assert r.status_code == 400


def test_curation_split_across_a_parent_boundary_refuses_merge(client, monkeypatch):
    build = _ready_build(client, "Parent boundary")
    bid = build["id"]
    _write_compiled(
        bid, {1: "\n\n".join(_sentences(f"Para{k}A", 15) for k in range(12))})
    _inline_jobs(monkeypatch)
    client.post("/api/knowledge/segment", json={"build_id": bid})
    doc = client.get(f"/api/builds/{bid}/passages").get_json()["doc"]
    parents = [p["parent_id"] for p in doc["passages"]]
    last_of_first = parents.index(parents[-1]) - 1    # last child of parent 1
    r = client.patch(f"/api/builds/{bid}/passages", json={
        "merge": {"id": doc["passages"][last_of_first]["id"]}})
    assert r.status_code == 400
    assert "same section" in r.get_json()["error"]


def test_curation_before_generation_404s(client):
    build = _ready_build(client, "No artifact yet")
    r = client.patch(f"/api/builds/{build['id']}/passages",
                     json={"exclude": ["x"]})
    assert r.status_code == 404


# --- index publish ----------------------------------------------------------------

def test_index_publish_segments_first_and_shapes_the_rows(
        client, monkeypatch, cloud, lexical):
    _inline_jobs(monkeypatch)
    bid = _published_build(client, "Fresh index")
    assert server._load_passages(bid) is None         # nothing pre-segmented

    r = client.post("/api/knowledge/index/publish", json={"build_id": bid})
    data = r.get_json()
    assert r.status_code == 200 and data["ok"] and data["model"] == ""
    assert server._an_jobs[data["job"]]["status"] == "done"

    doc = server._load_passages(bid)                  # the publish segmented
    assert doc and doc["passages"]

    ver_calls = [c for c in cloud.calls
                 if c[0] == "POST" and c[1].startswith("index_versions")]
    assert len(ver_calls) == 1
    assert ver_calls[0][3] == "return=representation"
    row = ver_calls[0][2][0]
    src_sha = server._file_sha256(
        server._entry_dir(bid) / "ocr" / "compiled.txt")
    assert row["slug"] == f"slug-{bid}" and row["channel"] == "stable"
    assert row["source_hash"] == src_sha
    assert row["config"] == {"recipe": server._passage_recipe(None),
                             "normalize": 1, "model": ""}
    assert row["stats"] == {"passages": len(doc["passages"]),
                            "embedded": 0, "excluded": 0}

    rows = [x for c in cloud.calls
            if c[0] == "POST" and c[1].startswith("passages") for x in c[2]]
    assert {x["passage_id"] for x in rows} == {p["id"] for p in doc["passages"]}
    assert all(x["index_id"] == "ver-1" and x["slug"] == f"slug-{bid}"
               for x in rows)
    assert all(x["embedding"] is None and x["body"] for x in rows)
    assert "on_conflict=index_id,slug,passage_id" in [
        c[1] for c in cloud.calls if c[1].startswith("passages")][0]


def test_index_publish_reuses_current_passages_and_skips_excluded(
        client, monkeypatch, cloud, lexical):
    _inline_jobs(monkeypatch)
    bid = _published_build(client, "Curated index")
    client.post("/api/knowledge/segment", json={"build_id": bid})
    doc = server._load_passages(bid)
    skip = doc["passages"][0]["id"]
    client.patch(f"/api/builds/{bid}/passages", json={"exclude": [skip]})

    r = client.post("/api/knowledge/index/publish", json={"build_id": bid})
    assert r.get_json()["ok"]

    row = [c for c in cloud.calls
           if c[0] == "POST" and c[1].startswith("index_versions")][0][2][0]
    assert row["stats"] == {"passages": len(doc["passages"]) - 1,
                            "embedded": 0, "excluded": 1}
    rows = [x for c in cloud.calls
            if c[0] == "POST" and c[1].startswith("passages") for x in c[2]]
    assert skip not in {x["passage_id"] for x in rows}
    # the artifact matched the OCR doc, so the publish did NOT re-segment:
    # the curation edit is still the manifest's producer
    assert server._load_manifest(bid)["artifacts"]["passages.json"][
        "produced_by"] == {"kind": "manual-edit"}


def test_index_publish_embeds_and_records_the_model(client, monkeypatch, cloud):
    _inline_jobs(monkeypatch)
    monkeypatch.setattr(server, "_embed_cfg", lambda: {
        "base": "https://embed.test/v1", "model": "embed-1", "key": "k"})
    seen: list = []

    def fake_embed(cfg, texts):
        assert cfg["model"] == "embed-1"
        seen.append(list(texts))
        return [[0.5, 0.25] for _ in texts]
    monkeypatch.setattr(server, "_embed_texts", fake_embed)

    bid = _published_build(client, "Embedded index")
    r = client.post("/api/knowledge/index/publish", json={"build_id": bid})
    assert r.get_json()["model"] == "embed-1"

    doc = server._load_passages(bid)
    assert seen and seen[0][0] == doc["passages"][0]["body"]   # embeds the body
    version = cloud.versions[0]
    assert version["config"]["model"] == "embed-1"
    assert version["stats"]["embedded"] == version["stats"]["passages"]
    rows = [x for c in cloud.calls
            if c[0] == "POST" and c[1].startswith("passages") for x in c[2]]
    assert all(x["embedding"] == "[0.5,0.25]" for x in rows)


def test_index_publish_rights_gating(client, monkeypatch, cloud, lexical):
    _inline_jobs(monkeypatch)
    # searchable-only is allowed: bodies are never anon-readable and the RPC
    # returns only snippets (docs/rights.md)
    ok = _published_build(client, "Snippets only", rights="searchable-only")
    r = client.post("/api/knowledge/index/publish", json={"build_id": ok})
    assert r.status_code == 200 and r.get_json()["ok"]

    blocked = _published_build(client, "Restricted", rights="no-public-text")
    r = client.post("/api/knowledge/index/publish", json={"build_id": blocked})
    assert r.status_code == 400
    assert "No public text" in r.get_json()["error"]

    undecided = _published_build(client, "Undecided", rights="")
    r = client.post("/api/knowledge/index/publish", json={"build_id": undecided})
    assert r.status_code == 400
    assert "rights decision" in r.get_json()["error"]


def test_index_publish_requires_the_archive_published_first(
        client, monkeypatch, cloud, lexical):
    build = _ready_build(client, "Not yet archived",
                         rights="public-domain")
    _write_compiled(build["id"], {1: _sentences("Alpha", 5)})
    r = client.post("/api/knowledge/index/publish",
                    json={"build_id": build["id"]})
    assert r.status_code == 400
    assert "archive" in r.get_json()["error"]


def test_index_publish_requires_configured_supabase(client, monkeypatch, lexical):
    monkeypatch.setattr(server, "_cloud_cfg", lambda: None)
    bid = _published_build(client, "No cloud")
    r = client.post("/api/knowledge/index/publish", json={"build_id": bid})
    assert r.status_code == 400
    assert "Supabase is not configured" in r.get_json()["error"]


def test_index_publish_cancel_deletes_the_partial_version(
        client, monkeypatch, cloud, lexical):
    _inline_jobs(monkeypatch)
    monkeypatch.setattr(server, "_INDEX_CHUNK", 1)
    inserted: list = []

    def upsert_then_cancel(cfg, table, on_conflict, rows, chunk=200):
        inserted.append(len(rows))
        for jid, job in server._jobs.items():
            if job.get("kind") == "index-publish" and job.get("state") == "running":
                server._jobs_events[jid].set()
        return len(rows)
    monkeypatch.setattr(server.sbase, "upsert_rows", upsert_then_cancel)

    bid = _published_build(client, "Cancelled index")
    r = client.post("/api/knowledge/index/publish", json={"build_id": bid})
    job = server._an_jobs[r.get_json()["job"]]
    assert job["status"] == "cancelled"
    assert "partial index version was removed" in job["note"]
    assert inserted == [1]                     # one chunk landed, then the stop
    deletes = [c for c in cloud.calls if c[0] == "DELETE"]
    assert [c[1] for c in deletes] == ["index_versions?id=eq.ver-1"]


def test_index_publish_names_the_migration_when_tables_are_missing(
        client, monkeypatch, lexical):
    _inline_jobs(monkeypatch)
    monkeypatch.setattr(server, "_cloud_cfg",
                        lambda: {"url": "https://cloud.test", "key": "svc"})

    def rest_missing(cfg, method, path, payload=None, prefer=""):
        raise server.sbase.SyncError(
            'HTTP 404: relation "public.index_versions" does not exist')
    monkeypatch.setattr(server.sbase, "_rest", rest_missing)

    bid = _published_build(client, "Schema behind")
    r = client.post("/api/knowledge/index/publish", json={"build_id": bid})
    job = server._an_jobs[r.get_json()["job"]]
    assert job["status"] == "error"
    assert "004_passages_index.sql" in job["error"]


# --- rollback + status ------------------------------------------------------------

def test_index_rollback_deletes_the_newest_version(client, monkeypatch,
                                                   cloud, lexical):
    _inline_jobs(monkeypatch)
    bid = _published_build(client, "Rollback")
    client.post("/api/knowledge/index/publish", json={"build_id": bid})
    client.post("/api/knowledge/index/publish", json={"build_id": bid})
    assert [v["id"] for v in cloud.versions] == ["ver-1", "ver-2"]

    r = client.post("/api/knowledge/index/rollback", json={"build_id": bid})
    data = r.get_json()
    assert data["ok"] and data["removed"] == "ver-2" and data["remaining"] == 1
    deletes = [c for c in cloud.calls if c[0] == "DELETE"]
    assert [c[1] for c in deletes] == ["index_versions?id=eq.ver-2"]


def test_index_rollback_with_no_versions_refuses(client, monkeypatch,
                                                 cloud, lexical):
    bid = _published_build(client, "Nothing to roll back")
    r = client.post("/api/knowledge/index/rollback", json={"build_id": bid})
    assert r.status_code == 400
    assert "no index versions" in r.get_json()["error"]


def test_index_status_reports_state_versions_and_unconfigured_cloud(
        client, monkeypatch, cloud, lexical):
    _inline_jobs(monkeypatch)
    bid = _published_build(client, "Status")
    data = client.get(f"/api/knowledge/index/status?build_id={bid}").get_json()
    assert data["ok"] and data["slug"] == f"slug-{bid}"
    assert data["versions"] == [] and data["state"]["exists"] is False
    assert data["published"] is True and data["warning"] == ""

    client.post("/api/knowledge/index/publish", json={"build_id": bid})
    data = client.get(f"/api/knowledge/index/status?build_id={bid}").get_json()
    assert [v["id"] for v in data["versions"]] == ["ver-1"]
    assert data["state"]["exists"] is True

    monkeypatch.setattr(server, "_cloud_cfg", lambda: None)
    # a passive status view never nags about cloud config — only the actions
    # that actually need Supabase (publish / rollback) fail loudly
    data = client.get(f"/api/knowledge/index/status?build_id={bid}").get_json()
    assert data["warning"] == ""
    r = client.post("/api/knowledge/index/publish", json={"build_id": bid})
    assert r.status_code == 400
    assert "Supabase is not configured" in r.get_json()["error"]
