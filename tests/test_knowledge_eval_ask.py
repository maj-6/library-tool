"""Knowledge Test view and Ask this book (issues #142/#143): the local
retrieval core (scorer, folding parity, snippets), the per-volume evaluation
set (CRUD, judgments, provenance), the metrics and the tracked run job, the
promotion-visibility fold into a published index version's stats, and the
Ask retrieval + answer routes with their grounding contract.

conftest.py points WHL_DATA_ROOT at a throwaway directory before any tools
module is imported, so nothing here touches live data.
"""
from __future__ import annotations

import math

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
    """Run analyze-style jobs on the calling thread (the house pattern)."""
    def run_inline(bid, kind, total, target, decorate=None):
        job = server._an_job_new(bid, kind, total)
        if decorate is not None:
            decorate(job)
        target(job)
        return job
    monkeypatch.setattr(server, "_an_job_start", run_inline)


# A tight recipe keeps the three topical pages apart (each page ~23-26
# tokens; any two together blow the 30-token cap), so page anchors and
# per-passage judgments stay meaningful in a tiny corpus.
_RECIPE = {"child_min": 20, "child_max": 30, "parent_min": 40,
           "parent_max": 60}
_PAGES = {
    1: "The vertues of phyſick are many and great. Phyſick heals the body. "
       "It purges the humours and restores the strength of the sick.",
    2: "Of the herb rosemary and its uses in the garden. Rosemary comforts "
       "the head. Rosemary strengthens memory. It grows in every good "
       "garden of this land.",
    3: "A table of weights and measures for the apothecary. Twenty grains "
       "make one scruple, three scruples one dram. Eight drams make one "
       "ounce.",
}


def _segmented_build(client, monkeypatch, title: str,
                     pages: dict[int, str]) -> str:
    build = _ready_build(client, title)
    bid = build["id"]
    _write_compiled(bid, pages)
    _inline_jobs(monkeypatch)
    r = client.post("/api/knowledge/segment",
                    json={"build_id": bid, "recipe": _RECIPE})
    assert r.status_code == 200 and r.get_json()["ok"]
    return bid


class _CloudMock:
    """A recording sbase._rest double covering index_versions reads and the
    search_passages RPC (canned rows set per test)."""

    def __init__(self):
        self.calls: list = []
        self.versions: list = []
        self.rpc_rows: list = []

    def rest(self, cfg, method, path, payload=None, prefer=""):
        self.calls.append((method, path, payload, prefer))
        if method == "POST" and path.startswith("rpc/search_passages"):
            return list(self.rpc_rows)
        if method == "POST" and path.startswith("index_versions"):
            row = dict(payload[0], id=f"ver-{len(self.versions) + 1}",
                       built_at="2026-07-17T00:00:00+00:00")
            self.versions.append(row)
            return [row]
        if method == "POST" and path.startswith("passages"):
            return None
        if method == "GET" and path.startswith("index_versions"):
            return list(reversed(self.versions))
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
def no_cloud(monkeypatch):
    monkeypatch.setattr(server, "_cloud_cfg", lambda: None)


@pytest.fixture()
def lexical(monkeypatch):
    monkeypatch.setattr(server, "_embed_cfg",
                        lambda: {"base": "", "model": "", "key": ""})


# --- the scorer: ranking, folding parity, snippets --------------------------------

def test_score_passages_ranks_by_coverage_and_folds_like_the_index():
    ps = [{"id": "a", "page_from": 1, "page_to": 1,
           "text": "The vertues of phyſick are many. Phyſick heals.",
           "body": server._search_normalize(
               "The vertues of phyſick are many. Phyſick heals.")},
          {"id": "b", "page_from": 2, "page_to": 2,
           "text": "Of herbs and their vertues in the garden.",
           "body": server._search_normalize(
               "Of herbs and their vertues in the garden.")},
          {"id": "c", "page_from": 3, "page_to": 3,
           "text": "A table of weights and measures.",
           "body": server._search_normalize(
               "A table of weights and measures.")}]

    # folding parity: the modern spelling finds the long-s original
    out = server._score_passages(ps, "physick", 10)
    assert [r["passage_id"] for r in out] == ["a"]
    assert out[0]["score"] == round(1.0 + math.log(2), 4)   # tf=2, coverage 1

    # coverage ranks the passage holding BOTH terms above the partial hit
    out = server._score_passages(ps, "vertues of physick", 10)
    assert [r["passage_id"] for r in out][:2] == ["a", "b"]
    assert out[0]["score"] > out[1]["score"]

    # the exact folded phrase doubles the score
    phrase = server._score_passages(ps, "weights and measures", 10)[0]
    assert phrase["passage_id"] == "c"
    base = 3.0                                   # three terms, tf=1 each
    assert phrase["score"] == round(base * 2.0, 4)

    # shape: every row carries what the evidence UI renders
    for r in out:
        assert set(r) == {"passage_id", "page_from", "page_to", "score",
                          "snippet", "text"}


def test_score_passages_marks_snippets_and_skips_excluded():
    body = server._search_normalize(
        "filler " * 40 + "the phyſick chapter begins here " + "tail " * 40)
    ps = [{"id": "a", "page_from": 1, "page_to": 2,
           "text": "irrelevant", "body": body},
          {"id": "x", "page_from": 3, "page_to": 3,
           "text": "physick too", "body": server._search_normalize("physick too")}]
    out = server._score_passages(ps, "physick", 10, excluded={"x"})
    assert [r["passage_id"] for r in out] == ["a"]          # excluded stays out
    snip = out[0]["snippet"]
    assert "«physick»" in snip                              # ts_headline's «» marks
    assert snip.startswith("… ") and snip.endswith(" …")    # a cut window
    assert len(snip.split()) <= server._SNIPPET_WORDS + 2


def test_score_passages_empty_query_returns_nothing():
    assert server._score_passages([{"id": "a", "body": "words"}], "  !! ") == []


# --- metrics math (hand-computed on a tiny fixture) -------------------------------

def test_eval_metrics_hand_computed():
    # ranked a,b,c,d against relevant {a,c,x}:
    #   Recall@10 = 2/3
    #   DCG = 1/log2(2) + 1/log2(4) = 1.5 ; IDCG = 1 + 1/log2(3) + 0.5
    #   MRR = 1 (first hit at rank 1)
    m = server._eval_metrics(["a", "b", "c", "d"], {"a", "c", "x"}, 10)
    idcg = 1 + 1 / math.log2(3) + 0.5
    assert m["recall"] == round(2 / 3, 4)
    assert m["ndcg"] == round(1.5 / idcg, 4)
    assert m["mrr"] == 1.0

    # first relevant at rank 3; the cutoff k=2 hides it entirely
    m = server._eval_metrics(["b", "d", "a"], {"a"}, 2)
    assert m == {"recall": 0.0, "ndcg": 0.0, "mrr": 0.0}
    m = server._eval_metrics(["b", "d", "a"], {"a"}, 3)
    assert m["recall"] == 1.0 and m["mrr"] == round(1 / 3, 4)


def test_eval_query_run_unanswerable_floor_both_ways():
    q = {"kind": "unanswerable", "judgments": {}}
    # local arm: pass when nothing scores ABOVE the floor
    ok = server._eval_query_run(q, [], server._EVAL_K,
                                server._EVAL_UNANSWERABLE_FLOOR)
    assert ok == {"kind": "unanswerable", "pass": True, "top": 0.0}
    bad = server._eval_query_run(
        q, [{"passage_id": "a", "score": 2.5}], server._EVAL_K,
        server._EVAL_UNANSWERABLE_FLOOR)
    assert bad["pass"] is False and bad["top"] == 2.5
    # published arm (floor None): ts_rank scales differ — only zero rows pass
    assert server._eval_query_run(q, [], server._EVAL_K, None)["pass"] is True
    assert server._eval_query_run(
        q, [{"passage_id": "a", "score": 0.01}], server._EVAL_K,
        None)["pass"] is False


def test_eval_query_run_judged_and_unjudged():
    judged = {"kind": "factual", "judgments": {"a": 1, "b": 0}}
    out = server._eval_query_run(
        judged, [{"passage_id": "a", "score": 3.0},
                 {"passage_id": "b", "score": 1.0}], 10, 1.0)
    assert out["recall"] == 1.0 and out["mrr"] == 1.0 and out["relevant"] == 1
    # negative-only judgments cannot anchor Recall/nDCG — reported unjudged
    negative = {"kind": "factual", "judgments": {"b": 0}}
    assert server._eval_query_run(negative, [], 10, 1.0) == {"judged": False}


def test_eval_overall_means_and_tallies():
    per = {"q1": {"recall": 1.0, "ndcg": 1.0, "mrr": 1.0, "relevant": 1},
           "q2": {"recall": 0.5, "ndcg": 0.7039, "mrr": 0.5, "relevant": 2},
           "q3": {"judged": False},
           "q4": {"kind": "unanswerable", "pass": True, "top": 0.0},
           "q5": {"kind": "unanswerable", "pass": False, "top": 2.0}}
    o = server._eval_overall(per)
    assert o == {"recall": 0.75, "ndcg": round((1.0 + 0.7039) / 2, 4),
                 "mrr": 0.75, "judged": 2, "unanswerable_pass": 1,
                 "unanswerable": 2, "unjudged": 1}
    empty = server._eval_overall({"q": {"judged": False}})
    assert empty["recall"] is None and empty["judged"] == 0


# --- the evaluation set: CRUD, judgments, provenance ------------------------------

def test_eval_crud_judgments_and_manifest(client, monkeypatch):
    bid = _segmented_build(client, monkeypatch, "Eval CRUD", _PAGES)
    psg_sha = server._file_sha256(server._entry_dir(bid) / "passages.json")

    # empty set for a fresh entry, no file needed
    doc = client.get(f"/api/builds/{bid}/eval").get_json()["doc"]
    assert doc == {"version": 1, "queries": []}

    # add validates text and kind
    r = client.put(f"/api/builds/{bid}/eval", json={"add": {"text": " ",
                                                            "kind": "factual"}})
    assert r.status_code == 400
    r = client.put(f"/api/builds/{bid}/eval", json={"add": {"text": "x",
                                                            "kind": "vibes"}})
    assert r.status_code == 400 and "kind" in r.get_json()["error"]

    r = client.put(f"/api/builds/{bid}/eval", json={
        "add": {"text": "what heals the body?", "kind": "factual"}})
    q = r.get_json()["doc"]["queries"][0]
    assert q["kind"] == "factual" and q["judgments"] == {} and q["id"]

    # edit text + kind
    r = client.put(f"/api/builds/{bid}/eval", json={
        "update": {"id": q["id"], "text": "what heals?", "kind": "thematic"}})
    q2 = r.get_json()["doc"]["queries"][0]
    assert (q2["text"], q2["kind"]) == ("what heals?", "thematic")
    assert client.put(f"/api/builds/{bid}/eval", json={
        "update": {"id": "nope", "text": "x"}}).status_code == 400

    # query edits keep the manifest row free of inputs (intent, not derivation)
    row = server._load_manifest(bid)["artifacts"]["eval.json"]
    assert row["produced_by"] == {"kind": "eval"} and row["inputs"] == []

    # judging persists and binds the set to the passages as they stand
    r = client.put(f"/api/builds/{bid}/eval", json={
        "judge": {"id": q["id"], "passage_id": "p-one", "rel": 1}})
    assert r.get_json()["doc"]["queries"][0]["judgments"] == {"p-one": 1}
    client.put(f"/api/builds/{bid}/eval", json={
        "judge": {"id": q["id"], "passage_id": "p-two", "rel": 0}})
    assert server._load_eval(bid)["queries"][0]["judgments"] == \
        {"p-one": 1, "p-two": 0}
    row = server._load_manifest(bid)["artifacts"]["eval.json"]
    assert row["inputs"] == [{"artifact": "passages.json", "sha256": psg_sha}]

    # rel null clears one judgment; bad rel is refused
    client.put(f"/api/builds/{bid}/eval", json={
        "judge": {"id": q["id"], "passage_id": "p-two", "rel": None}})
    assert server._load_eval(bid)["queries"][0]["judgments"] == {"p-one": 1}
    assert client.put(f"/api/builds/{bid}/eval", json={
        "judge": {"id": q["id"], "passage_id": "p-one",
                  "rel": 7}}).status_code == 400

    # remove deletes the query with its judgments
    r = client.put(f"/api/builds/{bid}/eval", json={"remove": q["id"]})
    assert r.get_json()["doc"]["queries"] == []


def test_eval_put_requires_a_verified_entry(client):
    r = client.post("/api/builds", json={"build": {"title": "Draft eval"}})
    bid = r.get_json()["build"]["id"]
    res = client.put(f"/api/builds/{bid}/eval",
                     json={"add": {"text": "x", "kind": "factual"}})
    assert res.status_code == 400
    assert "verified" in res.get_json()["error"]


# --- the run job: metrics cached under last_run -----------------------------------

def _seed_queries(client, bid: str) -> dict:
    """One judged factual query, one passing and one failing unanswerable.
    Returns {kind: query} for the three."""
    doc = client.get(f"/api/builds/{bid}/passages").get_json()["doc"]
    rosemary = next(p for p in doc["passages"] if "rosemary" in p["text"])
    client.put(f"/api/builds/{bid}/eval", json={
        "add": {"text": "rosemary memory", "kind": "factual"}})
    client.put(f"/api/builds/{bid}/eval", json={
        "add": {"text": "unicorn horn dosage", "kind": "unanswerable"}})
    # "rosemary" appears three times in the corpus — well above the floor
    client.put(f"/api/builds/{bid}/eval", json={
        "add": {"text": "rosemary", "kind": "unanswerable"}})
    queries = client.get(f"/api/builds/{bid}/eval").get_json()["doc"]["queries"]
    factual = next(q for q in queries if q["kind"] == "factual")
    client.put(f"/api/builds/{bid}/eval", json={
        "judge": {"id": factual["id"], "passage_id": rosemary["id"], "rel": 1}})
    return {q["text"]: q for q in queries}


def test_eval_run_caches_last_run_with_both_unanswerable_outcomes(
        client, monkeypatch, no_cloud):
    bid = _segmented_build(client, monkeypatch, "Eval run", _PAGES)
    seeded = _seed_queries(client, bid)

    r = client.post("/api/knowledge/eval/run", json={"build_id": bid})
    assert r.status_code == 200 and r.get_json()["ok"]
    job = server._an_jobs[r.get_json()["job"]]
    assert job["status"] == "done" and job["done"] == 3
    assert "R@10 1.00" in job["note"] and "unanswerable 1/2" in job["note"]

    run = server._load_eval(bid)["last_run"]
    assert run["k"] == 10 and run["floor"] == 1.0
    assert "published" not in run                    # no cloud configured
    per = run["local"]["queries"]
    factual = per[seeded["rosemary memory"]["id"]]
    assert factual["recall"] == 1.0 and factual["mrr"] == 1.0
    assert per[seeded["unicorn horn dosage"]["id"]]["pass"] is True
    assert per[seeded["rosemary"]["id"]]["pass"] is False   # tf pushes past 1.0
    overall = run["local"]["overall"]
    assert overall["judged"] == 1 and overall["unanswerable"] == 2
    assert overall["unanswerable_pass"] == 1 and overall["unjudged"] == 0

    # re-scoring against the same set is idempotent apart from the timestamp
    r = client.post("/api/knowledge/eval/run", json={"build_id": bid})
    run2 = server._load_eval(bid)["last_run"]
    assert run2["local"] == run["local"]


def test_eval_run_refuses_without_queries_or_passages(client, monkeypatch):
    bid = _segmented_build(client, monkeypatch, "Nothing to run", _PAGES)
    r = client.post("/api/knowledge/eval/run", json={"build_id": bid})
    assert r.status_code == 400 and "queries" in r.get_json()["error"]

    bare = _ready_build(client, "No passages")["id"]
    _write_compiled(bare, _PAGES)
    client.put(f"/api/builds/{bare}/eval",
               json={"add": {"text": "x", "kind": "factual"}})
    r = client.post("/api/knowledge/eval/run", json={"build_id": bare})
    assert r.status_code == 400 and "passages" in r.get_json()["error"]


def test_eval_run_scores_the_published_arm_when_cloud_is_configured(
        client, monkeypatch, cloud):
    bid = _segmented_build(client, monkeypatch, "Two arms", _PAGES)
    client.patch(f"/api/builds/{bid}", json={
        "status": "uploaded", "published_slug": f"slug-{bid}",
        "rights": "public-domain"})
    cloud.versions.append({"id": "ver-1", "slug": f"slug-{bid}"})
    seeded = _seed_queries(client, bid)
    doc = client.get(f"/api/builds/{bid}/passages").get_json()["doc"]
    rosemary = next(p for p in doc["passages"] if "rosemary" in p["text"])
    cloud.rpc_rows = [{"passage_id": rosemary["id"], "page_from": 2,
                       "page_to": 2, "rank": 0.09,
                       "snippet": "«rosemary» strengthens memory"}]

    r = client.post("/api/knowledge/eval/run", json={"build_id": bid})
    assert r.get_json()["ok"]
    run = server._load_eval(bid)["last_run"]
    pub = run["published"]
    assert pub["version"] == 1
    factual = pub["queries"][seeded["rosemary memory"]["id"]]
    assert factual["recall"] == 1.0 and factual["mrr"] == 1.0
    # the RPC returned rows for every query, so both unanswerables fail there
    assert pub["overall"]["unanswerable_pass"] == 0
    # and the local arm scored beside it, unchanged
    assert run["local"]["overall"]["judged"] == 1


# --- promotion visibility: stats.eval on the published version --------------------

def test_index_publish_folds_last_run_metrics_into_stats(
        client, monkeypatch, cloud, lexical):
    bid = _segmented_build(client, monkeypatch, "Promoted with metrics",
                           _PAGES)
    client.patch(f"/api/builds/{bid}", json={
        "status": "uploaded", "published_slug": f"slug-{bid}",
        "rights": "public-domain"})
    _seed_queries(client, bid)
    r = client.post("/api/knowledge/eval/run", json={"build_id": bid})
    assert r.get_json()["ok"]
    overall = server._load_eval(bid)["last_run"]["local"]["overall"]

    r = client.post("/api/knowledge/index/publish", json={"build_id": bid})
    assert r.get_json()["ok"]
    stats = cloud.versions[-1]["stats"]
    assert stats["eval"]["recall"] == overall["recall"]
    assert stats["eval"]["judged"] == 1 and stats["eval"]["unanswerable"] == 2
    assert stats["eval"]["at"]                       # when it was measured

    # ...and the status route hands the card exactly those rows back
    data = client.get(f"/api/knowledge/index/status?build_id={bid}").get_json()
    assert data["versions"][0]["stats"]["eval"]["recall"] == overall["recall"]


def test_index_publish_without_a_run_records_no_eval_stats(
        client, monkeypatch, cloud, lexical):
    bid = _segmented_build(client, monkeypatch, "Unevaluated", _PAGES)
    client.patch(f"/api/builds/{bid}", json={
        "status": "uploaded", "published_slug": f"slug-{bid}",
        "rights": "public-domain"})
    r = client.post("/api/knowledge/index/publish", json={"build_id": bid})
    assert r.get_json()["ok"]
    assert "eval" not in cloud.versions[-1]["stats"]


# --- Ask: retrieval, then the grounded answer -------------------------------------

def test_ask_returns_ranked_evidence_rows(client, monkeypatch):
    bid = _segmented_build(client, monkeypatch, "Ask evidence", _PAGES)
    r = client.post("/api/knowledge/ask", json={
        "build_id": bid, "question": "what is rosemary good for?"})
    data = r.get_json()
    assert r.status_code == 200 and data["ok"]
    assert data["published"] is None                 # not requested
    rows = data["results"]
    assert rows and rows[0]["page_from"] == 2        # the rosemary passage
    top = rows[0]
    assert set(top) == {"passage_id", "page_from", "page_to", "score",
                        "snippet", "text"}
    assert "«rosemary»" in top["snippet"]
    assert "Rosemary comforts the head." in top["text"]   # verbatim preview
    assert all(rows[i]["score"] >= rows[i + 1]["score"]
               for i in range(len(rows) - 1))


def test_ask_requires_question_and_passages(client, monkeypatch):
    bid = _segmented_build(client, monkeypatch, "Ask gates", _PAGES)
    r = client.post("/api/knowledge/ask", json={"build_id": bid,
                                                "question": "  "})
    assert r.status_code == 400
    bare = _ready_build(client, "Ask, no passages")["id"]
    r = client.post("/api/knowledge/ask", json={"build_id": bare,
                                                "question": "anything"})
    assert r.status_code == 404
    assert "passages" in r.get_json()["error"]


def test_ask_answer_without_a_key_is_a_clean_409(client, monkeypatch):
    bid = _segmented_build(client, monkeypatch, "No key", _PAGES)
    monkeypatch.setattr(server, "_ai_cfg", lambda: {
        "base": "https://ai.test", "model": "m",
        "instructions": "", "temperature": None, "timeout": None})
    monkeypatch.setattr(server, "_secret_is_configured", lambda _key: False)
    r = client.post("/api/knowledge/ask/answer", json={
        "build_id": bid, "question": "q", "passage_ids": ["x"]})
    assert r.status_code == 409
    assert "no AI key" in r.get_json()["error"]


def test_ask_answer_prompt_carries_the_grounding_contract(client, monkeypatch):
    bid = _segmented_build(client, monkeypatch, "Grounded answer", _PAGES)
    client.patch(f"/api/builds/{bid}", json={"year": "1652"})
    doc = client.get(f"/api/builds/{bid}/passages").get_json()["doc"]
    rosemary = next(p for p in doc["passages"] if "rosemary" in p["text"])
    seen: list = []

    def fake_chat(cfg, messages, json_mode=False, temperature=0.3,
                  timeout=240.0):
        seen.append(messages)
        return "This 1652 edition states rosemary comforts the head [p2]."
    monkeypatch.setattr(server, "_ai_cfg", lambda: {
        "base": "https://ai.test", "model": "m",
        "instructions": "", "temperature": None, "timeout": None})
    monkeypatch.setattr(server, "_secret_is_configured",
                        lambda key: key == "aiKey")
    monkeypatch.setattr(server, "_ai_chat", fake_chat)

    r = client.post("/api/knowledge/ask/answer", json={
        "build_id": bid, "question": "what is rosemary good for?",
        "passage_ids": [rosemary["id"], "unknown-id"]})
    data = r.get_json()
    assert r.status_code == 200 and data["ok"]
    assert data["answer"].endswith("[p2].") and data["abstained"] is False

    system = seen[0][0]["content"]
    # the four load-bearing clauses, asserted on the actual prompt text
    assert ("The archive does not contain enough evidence to answer this."
            in system)                                        # abstention
    assert "[p<page>]" in system and "[p12]" in system        # citations
    assert "this 1652 edition states" in system.lower()       # edition framing
    assert "modern medical advice" in system                  # the guard
    assert "refuse" in system

    user = seen[0][1]["content"]
    assert "Question: what is rosemary good for?" in user
    assert "[1] (page 2)" in user                             # page labels
    assert rosemary["text"] in user                           # verbatim passage
    assert "unknown-id" not in user                           # dropped, not sent

    # transient by design: nothing new landed in the entry folder
    names = sorted(p.name for p in server._entry_dir(bid).iterdir())
    assert names == ["manifest.json", "ocr", "passages.json"]


def test_ask_answer_abstention_is_flagged_not_an_error(client, monkeypatch):
    bid = _segmented_build(client, monkeypatch, "Abstains", _PAGES)
    doc = client.get(f"/api/builds/{bid}/passages").get_json()["doc"]
    monkeypatch.setattr(server, "_ai_cfg", lambda: {
        "base": "https://ai.test", "model": "m",
        "instructions": "", "temperature": None, "timeout": None})
    monkeypatch.setattr(server, "_secret_is_configured",
                        lambda key: key == "aiKey")
    monkeypatch.setattr(
        server, "_ai_chat",
        lambda cfg, messages, **kw: server._ASK_ABSTAIN + "\n")
    r = client.post("/api/knowledge/ask/answer", json={
        "build_id": bid, "question": "modern dosage?",
        "passage_ids": [doc["passages"][0]["id"]]})
    data = r.get_json()
    assert r.status_code == 200 and data["ok"]
    assert data["answer"] == server._ASK_ABSTAIN and data["abstained"] is True


def test_ask_answer_with_no_known_passages_refuses(client, monkeypatch):
    bid = _segmented_build(client, monkeypatch, "Nothing to cite", _PAGES)
    monkeypatch.setattr(server, "_ai_cfg", lambda: {
        "base": "https://ai.test", "model": "m",
        "instructions": "", "temperature": None, "timeout": None})
    monkeypatch.setattr(server, "_secret_is_configured",
                        lambda key: key == "aiKey")
    r = client.post("/api/knowledge/ask/answer", json={
        "build_id": bid, "question": "q", "passage_ids": ["ghost"]})
    assert r.status_code == 400
    assert "no known passages" in r.get_json()["error"]
