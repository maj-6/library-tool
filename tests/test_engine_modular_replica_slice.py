from __future__ import annotations

import copy
import hashlib
import json
import re
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

import pytest


SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))

from librarytool.adapters.filesystem import FilesystemReplicaRepository
from librarytool.engine.contracts import (
    ItemDescriptor,
    LayoutFamilyQuery,
    PageKey,
    ProposalAction,
    RecompileRegionPagesCommand,
    ReplaceRegionPageCommand,
    ReviewRegionProposalCommand,
)
from librarytool.engine.errors import ConflictError
from librarytool.engine.replica import ReplicaApplicationService
from librarytool.engine.text_layers import TextLayerService
from librarytool.engine.translations import (
    TranslationProvenanceService,
)


class Items:
    def __init__(self):
        self.items = {
            "book": ItemDescriptor("book", ("primary", "scan-b"), {"title": "Book"})
        }

    def get(self, item_id):
        return self.items.get(item_id)


class Policies:
    def __init__(self):
        self.family_input = None

    def content_revision(self, value, prefix="rr"):
        raw = json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            default=str,
        ).encode()
        return f"{prefix}-" + hashlib.sha256(raw).hexdigest()

    def proposal_revision(self, proposal):
        if proposal and proposal.get("revision"):
            return proposal["revision"]
        value = {k: v for k, v in (proposal or {}).items() if k != "revision"}
        return self.content_revision(value, "rp")

    def duplicate_rids(self, items):
        seen, duplicates = set(), set()
        for item in items:
            rid = self.clean_rid(item.get("rid")) if isinstance(item, dict) else ""
            if rid in seen:
                duplicates.add(rid)
            if rid:
                seen.add(rid)
        return duplicates

    def clean_rid(self, value):
        value = str(value or "")
        return value if re.fullmatch(r"[A-Za-z0-9_-]{1,64}", value) else ""

    def sanitize_region_items(self, items, *, source_type):
        result = []
        for index, item in enumerate(items):
            if not isinstance(item, dict):
                continue
            result.append(
                {
                    "rid": self.clean_rid(item.get("rid")) or f"new-{index}",
                    "role": str(item.get("role") or "body"),
                    "order": index,
                    "box": copy.deepcopy(item.get("box") or {}),
                    "text": str(item.get("text") or ""),
                    "src_type": source_type,
                }
            )
        return result

    def sanitize_dims(self, dims):
        return copy.deepcopy(dict(dims or {}))

    def sanitize_ext(self, ext):
        return copy.deepcopy(dict(ext or {}))

    def sanitize_document_name(self, value):
        value = re.sub(r"[^\w.\- ]", "_", str(value or "").strip()) or "ocr"
        return value if value.lower().endswith(".txt") else value + ".txt"

    def normalize_language(self, value):
        return re.sub(r"[^a-z\-]", "", str(value or "").lower())[:12]

    def accept_region_proposal(self, current, proposal):
        items = copy.deepcopy(proposal.get("items") or [])
        if not items:
            return None
        return {
            "doc": proposal.get("doc") or "compiled.txt",
            "dims": copy.deepcopy(proposal.get("dims") or {}),
            "items": items,
            "origin": "machine",
        }

    def dismiss_region_proposal(self, current):
        if current is None:
            return None
        result = copy.deepcopy(dict(current))
        result.pop("stale", None)
        return result

    def compose_text(self, items, *, layer="text"):
        field = "norm" if layer == "norm" else "text"
        return "\n\n".join(str(item.get(field) or "") for item in items).strip()

    def propose_layout_families(self, pages, **options):
        self.family_input = pages
        pages["mutated-by-policy"] = {}
        return {"input_revision": self.content_revision(pages), "options": options}


class TextStore:
    def __init__(self, *, fail=False):
        self.fail = fail
        self.pages = {}
        self.bindings = {}

    def merge_page(self, item_id, document, page, text):
        if self.fail:
            raise OSError("disk unavailable")
        self.pages[(item_id, document, page)] = text

    def bind_document_source(self, item_id, document, source_id):
        self.bindings[(item_id, document)] = source_id


def make_replica(tmp_path, *, fail_text=False):
    policies = Policies()
    repository = FilesystemReplicaRepository(
        lambda item_id: tmp_path / item_id / "ocr" / "layout.json"
    )
    text_store = TextStore(fail=fail_text)
    text_layers = TextLayerService(text_store, policies)
    service = ReplicaApplicationService(
        Items(),
        repository,
        policies,
        text_layers,
        clock=lambda: datetime(2026, 7, 19, 12, 0, tzinfo=timezone.utc),
    )
    return service, repository, policies, text_store


def seed(repository, value):
    with repository.unit_of_work("book") as uow:
        uow.workspace.update(copy.deepcopy(value))
        uow.commit()


def test_filesystem_unit_of_work_is_explicit_and_supports_multiple_commits(tmp_path):
    writes = []
    path = tmp_path / "layout.json"

    def write_json(dest, value):
        writes.append(copy.deepcopy(value))
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(json.dumps(value), encoding="utf-8")

    repository = FilesystemReplicaRepository(
        lambda _item: path,
        write_json=write_json,
    )
    with repository.unit_of_work("book") as uow:
        uow.workspace["first"] = True
        uow.commit()
        uow.workspace["second"] = True
        uow.commit()
        assert uow.commit_count == 2
    assert writes == [{"first": True}, {"first": True, "second": True}]
    snapshot = repository.snapshot("book")
    snapshot["outside"] = True
    assert "outside" not in repository.snapshot("book")


def test_conditional_replace_enforces_page_and_cross_page_identity(tmp_path):
    service, _repository, _policies, _text = make_replica(tmp_path)
    key = PageKey("book", "primary", 1)
    empty = service.get_region_page(key)
    saved = service.replace_region_page(
        ReplaceRegionPageCommand(
            key=key,
            expected_revision=empty.revision,
            items=[{"rid": "same", "text": "Alpha"}],
            dims={"w": 100, "h": 200},
        )
    )
    assert saved.found and saved.items[0]["text"] == "Alpha"

    with pytest.raises(ConflictError) as stale:
        service.replace_region_page(
            ReplaceRegionPageCommand(
                key=key,
                expected_revision=empty.revision,
                items=[{"rid": "fresh", "text": "Changed"}],
            )
        )
    assert stale.value.as_dict()["code"] == "stale_region_revision"
    assert stale.value.details["current_revision"] == saved.revision

    other = PageKey("book", "primary", 2)
    with pytest.raises(ConflictError) as duplicate:
        service.replace_region_page(
            ReplaceRegionPageCommand(
                key=other,
                expected_revision=service.get_region_page(other).revision,
                items=[{"rid": "same", "text": "Collision"}],
            )
        )
    assert duplicate.value.code == "cross_page_region_identity"
    assert duplicate.value.details["duplicate_rids"] == ["same"]


def test_two_headless_clients_cannot_both_replace_one_revision(tmp_path):
    service, _repository, _policies, _text = make_replica(tmp_path)
    key = PageKey("book", "primary", 1)
    revision = service.get_region_page(key).revision
    barrier = threading.Barrier(2)

    def replace(rid):
        barrier.wait()
        try:
            page = service.replace_region_page(ReplaceRegionPageCommand(
                key=key,
                expected_revision=revision,
                items=[{"rid": rid, "text": rid}],
            ))
            return "saved", page.items[0]["rid"]
        except ConflictError as exc:
            return "conflict", exc.code

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(replace, ("client-a", "client-b")))

    assert sorted(result[0] for result in results) == ["conflict", "saved"]
    assert service.get_region_page(key).items[0]["rid"] in {
        "client-a", "client-b"}


def test_proposal_apply_commits_pending_journal_before_derived_failure(tmp_path):
    service, repository, policies, _text = make_replica(tmp_path, fail_text=True)
    key = PageKey("book", "primary", 4)
    current = {
        "doc": "compiled.txt",
        "items": [{"rid": "old", "text": "Old"}],
        "origin": "human",
    }
    region_revision = policies.content_revision(current, "rr")
    proposal = {
        "doc": "compiled.txt",
        "items": [{"rid": "new", "text": "New"}],
        "provider": "detector",
        "base_revision": region_revision,
    }
    proposal["revision"] = policies.content_revision(proposal, "rp")
    seed(
        repository,
        {
            "regions": {"primary": {"4": current}},
            "region_proposals": {"primary": {"4": proposal}},
        },
    )

    result = service.review_region_proposal(
        ReviewRegionProposalCommand(
            key=key,
            action="apply",
            expected_region_revision=region_revision,
            expected_proposal_revision=proposal["revision"],
        )
    )
    assert result.compiled is False
    assert result.derived_failure.code == "compiled_text_pending"
    assert result.derived_failure.details["document"] == "compiled.txt"
    stored = repository.snapshot("book")
    assert stored["regions"]["primary"]["4"]["items"][0]["text"] == "New"
    assert "region_proposals" not in stored
    pending = stored["region_compile_pending"]["primary"]["4"]
    assert pending["text"] == "New"
    assert pending["at"] == "2026-07-19T12:00:00+00:00"


def test_proposal_apply_clears_journal_after_success(tmp_path):
    service, repository, policies, text = make_replica(tmp_path)
    key = PageKey("book", "primary", 7)
    current = {"doc": "compiled.txt", "items": [], "origin": "machine"}
    region_revision = policies.content_revision(current, "rr")
    proposal = {
        "doc": "compiled.txt",
        "items": [{"rid": "new", "text": "Accepted"}],
        "base_revision": region_revision,
        "provider": "detector",
    }
    proposal["revision"] = policies.content_revision(proposal, "rp")
    seed(
        repository,
        {
            "regions": {"primary": {"7": current}},
            "region_proposals": {"primary": {"7": proposal}},
        },
    )
    result = service.review_region_proposal(
        ReviewRegionProposalCommand(
            key,
            ProposalAction.APPLY,
            region_revision,
            proposal["revision"],
        )
    )
    assert result.compiled is True
    assert result.page.compile_pending is None
    assert "region_compile_pending" not in repository.snapshot("book")
    assert text.pages[("book", "compiled.txt", 7)] == "Accepted"


def test_layout_family_query_is_read_only_even_if_policy_mutates_input(tmp_path):
    service, repository, policies, _text = make_replica(tmp_path)
    seed(repository, {"regions": {"primary": {"1": {"items": []}}}})
    before = repository.snapshot("book")
    result = service.propose_layout_families(
        LayoutFamilyQuery("book", "primary", {"min_family_size": 2})
    )
    assert result.capability == "replica.layout-families.propose@1"
    assert result.proposal["options"] == {"min_family_size": 2}
    assert "mutated-by-policy" in policies.family_input
    assert repository.snapshot("book") == before


def test_recompile_recovers_pending_and_uses_source_scoped_normalized_target(
    tmp_path,
):
    service, repository, _policies, text = make_replica(tmp_path)
    seed(repository, {
        "regions": {"scan-b": {
            "2": {"doc": "compiled-b.txt", "items": [
                {"rid": "r2", "text": "Diplomatic", "norm": "Modern"}
            ]},
        }},
        "region_compile_pending": {"scan-b": {
            "1": {"doc": "compiled-b.txt", "text": "Recovered"},
        }},
    })

    recovered = service.recompile_region_pages(
        RecompileRegionPagesCommand("book", "scan-b", "text")
    )
    assert recovered.pages == 2
    assert recovered.documents == ("compiled-b.txt",)
    assert text.pages[("book", "compiled-b.txt", 1)] == "Recovered"
    assert text.pages[("book", "compiled-b.txt", 2)] == "Diplomatic"
    assert "region_compile_pending" not in repository.snapshot("book")

    normalized = service.recompile_region_pages(
        RecompileRegionPagesCommand("book", "scan-b", "norm", page=2)
    )
    assert normalized.pages == 1
    assert normalized.documents == ("normalized-scan-b.txt",)
    assert text.pages[("book", "normalized-scan-b.txt", 2)] == "Modern"
    assert text.bindings[("book", "normalized-scan-b.txt")] == "scan-b"
    assert TextLayerService.distribute("a\n\nb\n\nc", [1, 1, 1]) == [
        "a", "b", "c"]


def test_translation_provenance_preserves_paragraphs_and_classifies_pages():
    service = TranslationProvenanceService()
    assert service.source_hash("alpha\nline\n\nbeta") == \
        service.source_hash("alpha line\n\nbeta")
    assert service.source_hash("alpha line beta") != \
        service.source_hash("alpha line\n\nbeta")

    metadata = {"src": "compiled.txt", "pages": {
        "1": {"source_hash": service.source_hash("Alpha")},
        "2": {"sha1": service.legacy_source_hash("Old beta")},
    }}
    status = service.status(
        metadata,
        {1: "Alpha", 2: "Changed beta", 3: "Gamma"},
        {1: "Un", 2: "Deux", 4: "Orphan"},
        source_layer="compiled.txt",
    )
    assert status.stale == (2,)
    assert status.untracked == (4,)
    assert status.missing == (3,)
    assert status.orphaned == (4,)


def test_engine_package_has_no_transport_or_transitional_tool_imports():
    forbidden = ("flask", "server", "replica_service", "layout_roles", "libformat")
    for path in (SRC / "librarytool").rglob("*.py"):
        source = path.read_text(encoding="utf-8")
        for name in forbidden:
            assert not re.search(rf"^(?:from|import)\s+{name}\b", source, re.M), path
