"""Framework-free integrity policies for the Replica working store.

The Flask routes still own filesystem locking during the transition, but the
rules in this module deliberately know nothing about Flask, Electron, paths,
or global application state.  They are the first application-service seam for
moving Replica behind the headless Library Engine described in
``docs/modular-engine-architecture.md``.
"""
from __future__ import annotations

import hashlib
import heapq
import json
import math
from collections import Counter
from datetime import datetime, timezone

import libformat


def content_revision(value, prefix: str = "rr") -> str:
    """A stable optimistic-concurrency token for a JSON-shaped value.

    ``None`` represents an absent page, so even creation has a precondition.
    ``default=str`` keeps a hand-edited legacy sidecar inspectable; the normal
    write paths already reject/sanitize values that strict JSON cannot carry.
    """
    blob = json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, default=str).encode("utf-8")
    return f"{prefix}-" + hashlib.sha256(blob).hexdigest()


def is_protected(record: dict | None) -> bool:
    """Whether automation must preserve the canonical region record.

    Verified pages, live human edits, and imported editorial work are all
    canonical.  Machine/template drafts remain replaceable until a human saves
    them.  The explicit page origin supports records written after this seam;
    item ``src_type`` keeps legacy records safe too.
    """
    if not isinstance(record, dict):
        return False
    if record.get("state") == "verified":
        return True
    if record.get("origin") in ("human", "import"):
        return True
    return any(isinstance(item, dict) and
               item.get("src_type") in ("human", "import")
               for item in record.get("items") or [])


def duplicate_rids(items) -> set[str]:
    """Valid RIDs repeated within an iterable of region records."""
    seen: set[str] = set()
    duplicates: set[str] = set()
    for item in items or []:
        if not isinstance(item, dict):
            continue
        rid = libformat.clean_rid(item.get("rid"))
        if not rid:
            continue
        if rid in seen:
            duplicates.add(rid)
        seen.add(rid)
    return duplicates


def stable_export_items(items, seed: str, used: set[str] | None = None) -> list:
    """Copy items and fill legacy missing/duplicate RIDs deterministically.

    Export is a read, so it must not stamp random identifiers into the working
    sidecar.  New writes receive UUID RIDs at creation; this deterministic
    fallback exists only for pre-RID records and stays stable across unchanged
    exports.  ``used`` may be shared across pages to guarantee book-wide
    uniqueness.
    """
    used = used if used is not None else set()
    out = []
    for index, item in enumerate(items or []):
        if not isinstance(item, dict):
            continue
        record = dict(item)
        rid = libformat.clean_rid(record.get("rid"))
        if not rid or rid in used:
            attempt = 0
            while True:
                digest = hashlib.sha256(
                    f"{seed}\0{index}\0{attempt}".encode("utf-8")
                ).hexdigest()
                rid = "legacy-" + digest[:32]
                if rid not in used:
                    break
                attempt += 1
            record["rid"] = rid
        used.add(rid)
        out.append(record)
    return out


def make_proposal(*, doc: str, dims: dict, items: list, provider: str,
                  base_revision: str, reason: str = "protected-page",
                  text: str = "") -> dict:
    """Create the provider-neutral proposal envelope stored beside regions."""
    proposal = {
        "doc": doc,
        "dims": dims or {},
        "items": libformat.ensure_rids(items),
        "provider": str(provider or "unknown"),
        "base_revision": base_revision,
        "reason": reason,
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    if text:
        proposal["text"] = str(text)
    proposal["revision"] = content_revision(proposal, "rp")
    return proposal


def proposal_revision(proposal: dict | None) -> str:
    """Return a proposal's self-contained CAS token.

    The revision field is excluded from its own digest.  Legacy/hand-edited
    proposals are therefore still addressable even when the cached token is
    absent.
    """
    if not isinstance(proposal, dict):
        return content_revision(None, "rp")
    cached = str(proposal.get("revision") or "")
    if cached:
        return cached
    value = {k: v for k, v in proposal.items() if k != "revision"}
    return content_revision(value, "rp")


def stale_marker(provider: str, reason: str = "source-text-changed") -> dict:
    """Small review marker added when new automation cannot replace a page."""
    return {
        "reason": reason,
        "provider": str(provider or "unknown"),
        "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }


def accept_region_proposal(current: dict | None,
                           proposal: dict) -> dict | None:
    """Build the canonical record produced by an explicit proposal accept.

    This is deliberately pure: the transport/repository layer performs the
    compare-and-set and persistence. Empty proposed layouts mean an explicit
    removal of the old region layer and therefore return ``None``.
    """
    if not isinstance(proposal, dict):
        raise ValueError("region proposal is missing")
    items = libformat.sanitize_page_items(
        proposal.get("items") if isinstance(proposal.get("items"), list) else [],
        src_type="human")
    if not items:
        return None
    record = {
        "doc": str(proposal.get("doc") or "compiled.txt"),
        "dims": libformat.sanitize_dims(proposal.get("dims")),
        "items": items,
        "origin": "human",
    }
    page_ext = libformat.sanitize_ext(
        (current or {}).get("ext") if isinstance(current, dict) else None)
    if page_ext:
        record["ext"] = page_ext
    return record


def dismiss_region_proposal(current: dict | None) -> dict | None:
    """Copy a canonical record while clearing only its proposal stale mark."""
    if not isinstance(current, dict):
        return None
    record = dict(current)
    record.pop("stale", None)
    return record


# --- automatic page-layout family proposals -------------------------------

# Roles remain distinct in the score, while these broad groups make one noisy
# role classification less important than the page geometry.  Unknown roles
# simply form their own group and therefore remain useful evidence.
_ROLE_GROUPS = {
    "body": "main-text",
    "paragraph": "main-text",
    "drop-capital": "main-text",
    "drop-cap": "main-text",
    "title": "heading",
    "subtitle": "heading",
    "heading": "heading",
    "header": "running-matter",
    "footer": "running-matter",
    "page-number": "running-matter",
    "signature-mark": "running-matter",
    "catch-word": "running-matter",
    "catchword": "running-matter",
    "marginalia": "secondary-text",
    "footnote": "secondary-text",
    "caption": "secondary-text",
    "figure": "non-text",
    "image": "non-text",
    "table": "non-text",
}


def _page_sort_key(page) -> tuple:
    """A total, JSON-friendly order for mixed legacy page identifiers."""
    if isinstance(page, int) and not isinstance(page, bool):
        return (0, page)
    text = str(page)
    if text.isdigit():
        return (0, int(text))
    return (1, text)


def _clean_page_id(page):
    if isinstance(page, int) and not isinstance(page, bool):
        return page
    text = str(page)
    if text.isdigit():
        return int(text)
    return text


def _role_name(value) -> str:
    role = str(value or "body").strip().lower().replace("_", "-")
    return role or "body"


def _finite_number(value):
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def _even_sample(values: list, limit: int) -> list:
    """Keep evidence across the whole page when a detector emits many lines."""
    if len(values) <= limit:
        return values
    if limit == 1:
        return [values[0]]
    indexes = [round(i * (len(values) - 1) / (limit - 1))
               for i in range(limit)]
    return [values[index] for index in indexes]


def _layout_features(record, max_regions: int) -> dict:
    """Copy the layout-bearing portion of one saved page into normalized form."""
    if isinstance(record, dict):
        raw_items = record.get("items") or []
        dims = record.get("dims") or {}
    elif isinstance(record, (list, tuple)):
        raw_items = record
        dims = {}
    else:
        raw_items = []
        dims = {}

    width = _finite_number(dims.get("w")) if isinstance(dims, dict) else None
    height = _finite_number(dims.get("h")) if isinstance(dims, dict) else None
    raw_regions = []
    for index, item in enumerate(raw_items):
        if not isinstance(item, dict):
            continue
        box = item.get("box")
        if not isinstance(box, dict):
            continue
        values = [_finite_number(box.get(name)) for name in ("x", "y", "w", "h")]
        if any(value is None for value in values):
            continue
        x, y, w, h = values
        if w <= 0 or h <= 0:
            continue
        order = _finite_number(item.get("order"))
        raw_regions.append({
            "role": _role_name(item.get("role")),
            "x": x,
            "y": y,
            "w": w,
            "h": h,
            "order": order if order is not None else index,
        })

    # Current sidecars store normalized boxes; legacy/provider records can use
    # pixels.  Normalize once per page so mixed units cannot form false groups.
    uses_page_units = any(
        max(abs(region[name]) for name in ("x", "y", "w", "h")) > 1.0001
        for region in raw_regions
    )
    if uses_page_units:
        if not width or width <= 0:
            width = max((region["x"] + region["w"] for region in raw_regions),
                        default=1.0)
        if not height or height <= 0:
            height = max((region["y"] + region["h"] for region in raw_regions),
                         default=1.0)
    else:
        width = height = 1.0

    regions = []
    for region in raw_regions:
        x = region["x"] / width
        y = region["y"] / height
        w = region["w"] / width
        h = region["h"] / height
        # Cropping can leave a box fractionally outside the page.  Clipping is
        # more robust than discarding otherwise useful layout evidence.
        x = min(1.0, max(0.0, x))
        y = min(1.0, max(0.0, y))
        w = min(1.0 - x, max(0.00001, w))
        h = min(1.0 - y, max(0.00001, h))
        if w <= 0 or h <= 0:
            continue
        regions.append({
            "role": region["role"],
            "x": x, "y": y, "w": w, "h": h,
            "order": region["order"],
        })

    regions.sort(key=lambda region: (
        region["order"], region["y"], region["x"], region["role"]
    ))
    original_count = len(regions)
    regions = _even_sample(regions, max_regions)
    roles = Counter(region["role"] for region in regions)
    return {
        "regions": regions,
        "roles": roles,
        "region_count": len(regions),
        "original_region_count": original_count,
        "truncated": original_count > max_regions,
    }


def _role_similarity(left: str, right: str) -> float:
    if left == right:
        return 1.0
    left_group = _ROLE_GROUPS.get(left, left)
    right_group = _ROLE_GROUPS.get(right, right)
    return 0.55 if left_group == right_group else 0.05


def _box_iou(left: dict, right: dict) -> float:
    x0 = max(left["x"], right["x"])
    y0 = max(left["y"], right["y"])
    x1 = min(left["x"] + left["w"], right["x"] + right["w"])
    y1 = min(left["y"] + left["h"], right["y"] + right["h"])
    intersection = max(0.0, x1 - x0) * max(0.0, y1 - y0)
    union = left["w"] * left["h"] + right["w"] * right["h"] - intersection
    return intersection / union if union > 0 else 0.0


def _region_geometry_similarity(left: dict, right: dict) -> float:
    left_cx = left["x"] + left["w"] / 2
    left_cy = left["y"] + left["h"] / 2
    right_cx = right["x"] + right["w"] / 2
    right_cy = right["y"] + right["h"] / 2
    center_distance = math.hypot(left_cx - right_cx, left_cy - right_cy)
    size_distance = abs(left["w"] - right["w"]) + abs(left["h"] - right["h"])
    center_score = math.exp(-5.0 * center_distance)
    size_score = math.exp(-5.0 * size_distance)
    return 0.50 * _box_iou(left, right) + 0.30 * center_score + 0.20 * size_score


def _layout_similarity(left: dict, right: dict) -> dict:
    """Deterministic one-to-one region matching plus semantic-role overlap."""
    left_regions = left["regions"]
    right_regions = right["regions"]
    if not left_regions or not right_regions:
        return {"score": 0.0, "geometry": 0.0, "role_overlap": 0.0,
                "coverage": 0.0}

    candidates = []
    for left_index, left_region in enumerate(left_regions):
        for right_index, right_region in enumerate(right_regions):
            geometry = _region_geometry_similarity(left_region, right_region)
            role = _role_similarity(left_region["role"], right_region["role"])
            score = 0.78 * geometry + 0.22 * role
            if score >= 0.18:
                candidates.append((score, geometry, left_index, right_index))
    candidates.sort(key=lambda value: (-value[0], value[2], value[3]))

    used_left = set()
    used_right = set()
    scores = []
    geometry_scores = []
    for score, geometry, left_index, right_index in candidates:
        if left_index in used_left or right_index in used_right:
            continue
        used_left.add(left_index)
        used_right.add(right_index)
        scores.append(score)
        geometry_scores.append(geometry)

    larger_count = max(len(left_regions), len(right_regions))
    coverage = len(scores) / larger_count
    matched_quality = sum(scores) / len(scores) if scores else 0.0
    # One missing OCR region should not split a recurring family, but several
    # missing/extra regions should.  This soft coverage penalty captures both.
    matched_score = matched_quality * (0.55 + 0.45 * coverage)
    role_intersection = sum(
        min(left["roles"].get(role, 0), right["roles"].get(role, 0))
        for role in set(left["roles"]) | set(right["roles"])
    )
    role_overlap = role_intersection / larger_count
    score = 0.82 * matched_score + 0.18 * role_overlap
    return {
        "score": min(1.0, max(0.0, score)),
        "geometry": (sum(geometry_scores) / len(geometry_scores)
                     if geometry_scores else 0.0),
        "role_overlap": role_overlap,
        "coverage": coverage,
    }


def _mean(values) -> float:
    values = list(values)
    return sum(values) / len(values) if values else 0.0


def _evidence_factor(feature: dict) -> float:
    count = feature["region_count"]
    return 0.0 if count == 0 else min(1.0, 0.72 + 0.07 * count)


def _average_link_clusters(pages: list, pair_metrics: dict,
                           similarity_threshold: float,
                           stats: dict | None = None) -> list[tuple]:
    """Cluster cached page similarities in quadratic work and storage.

    Average linkage has a useful recurrence: after merging clusters A and B,
    similarity(A+B, C) is the size-weighted mean of the two cached scores.
    The former implementation recomputed every cluster cross-product on every
    iteration, which became effectively cubic for book-length inputs.  This
    priority-queue implementation calculates each newly possible pair once.

    ``stats`` is an optional private test hook.  It records operation counts,
    not timings, so the scaling regression is stable on slow CI machines.
    """
    if stats is not None:
        stats.clear()
        stats.update({
            "initial_pairs": 0,
            "linkage_updates": 0,
            "heap_pushes": 0,
            "heap_pops": 0,
        })
    if len(pages) < 2:
        return [(page,) for page in pages]

    clusters = {
        index: {
            "pages": (page,),
            "size": 1,
            "mask": 1 << (len(pages) - index - 1),
        }
        for index, page in enumerate(pages)
    }
    active = set(clusters)
    similarities = {}
    heap = []

    def pair_key(left_id, right_id):
        return ((left_id, right_id) if left_id < right_id
                else (right_id, left_id))

    def push(left_id, right_id, score):
        # At a given clustering step, candidate unions cannot be strict
        # subsets of one another.  A high-order membership bit therefore has
        # exactly the same tie order as the former tuple-of-pages comparison,
        # without copying long tuples into every heap entry.
        union_mask = clusters[left_id]["mask"] | clusters[right_id]["mask"]
        heapq.heappush(heap, (-score, -union_mask, left_id, right_id))
        if stats is not None:
            stats["heap_pushes"] += 1

    for left_index, left_page in enumerate(pages):
        for right_index in range(left_index + 1, len(pages)):
            right_page = pages[right_index]
            score = pair_metrics[(left_page, right_page)]["score"]
            similarities[(left_index, right_index)] = score
            push(left_index, right_index, score)
            if stats is not None:
                stats["initial_pairs"] += 1

    next_cluster_id = len(pages)
    while len(active) > 1:
        best = None
        while heap:
            negative_score, _negative_mask, left_id, right_id = heapq.heappop(heap)
            if stats is not None:
                stats["heap_pops"] += 1
            # Entries involving a merged cluster are intentionally left in
            # the heap; discarding them lazily is cheaper than heap deletion.
            if left_id in active and right_id in active:
                best = (-negative_score, left_id, right_id)
                break
        # Weighted recurrence can land a few ulps below an exactly equal
        # threshold (for example 0.7799999999999999 for 0.78).  Equality is a
        # merge by contract, so do not let floating addition order split it.
        if best is None or best[0] + 1e-12 < similarity_threshold:
            break

        _best_score, left_id, right_id = best
        left = clusters[left_id]
        right = clusters[right_id]
        other_ids = sorted(active - {left_id, right_id})
        new_id = next_cluster_id
        next_cluster_id += 1
        new_pages = tuple(sorted(left["pages"] + right["pages"],
                                 key=_page_sort_key))
        clusters[new_id] = {
            "pages": new_pages,
            "size": left["size"] + right["size"],
            "mask": left["mask"] | right["mask"],
        }
        active.remove(left_id)
        active.remove(right_id)
        active.add(new_id)

        for other_id in other_ids:
            left_score = similarities[pair_key(left_id, other_id)]
            right_score = similarities[pair_key(right_id, other_id)]
            score = (
                left["size"] * left_score + right["size"] * right_score
            ) / (left["size"] + right["size"])
            similarities[pair_key(new_id, other_id)] = score
            push(new_id, other_id, score)
            if stats is not None:
                stats["linkage_updates"] += 1

    out = [clusters[cluster_id]["pages"] for cluster_id in active]
    out.sort(key=lambda cluster: tuple(_page_sort_key(page) for page in cluster))
    return out


def propose_layout_families(page_records, *, similarity_threshold: float = 0.78,
                            min_family_size: int = 2,
                            low_confidence_threshold: float = 0.62,
                            max_families: int = 12,
                            max_regions_per_page: int = 64) -> dict:
    """Return non-canonical recurring page-layout family proposals.

    ``page_records`` is normally the saved ``page -> {dims, items, ...}``
    mapping.  It may also be a sequence of records carrying a ``page`` field.
    Geometry and semantic roles are normalized into unit-page coordinates,
    then clustered using deterministic average-link agglomeration.  Each
    recurring cluster is represented by its medoid page.  Singleton, weak, or
    overflow clusters remain explicit review exceptions instead of being
    forced into a canonical assignment.

    There are deliberately no timestamps, provider names, filesystem paths,
    or generated region IDs in the result: unchanged input always produces
    byte-for-byte equivalent JSON, and callers are free to store, render, or
    discard the proposal.  The input is never mutated.
    """
    try:
        similarity_threshold = float(similarity_threshold)
        low_confidence_threshold = float(low_confidence_threshold)
        min_family_size = int(min_family_size)
        max_families = int(max_families)
        max_regions_per_page = int(max_regions_per_page)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("layout-family parameters must be numeric") from exc
    if not 0.0 < similarity_threshold <= 1.0:
        raise ValueError("similarity_threshold must be in (0, 1]")
    if not 0.0 <= low_confidence_threshold <= 1.0:
        raise ValueError("low_confidence_threshold must be in [0, 1]")
    if min_family_size < 2:
        raise ValueError("min_family_size must be at least 2")
    if max_families < 1:
        raise ValueError("max_families must be at least 1")
    if not 1 <= max_regions_per_page <= 256:
        raise ValueError("max_regions_per_page must be between 1 and 256")

    if isinstance(page_records, dict):
        entries = [(key, value) for key, value in page_records.items()]
    elif isinstance(page_records, (list, tuple)):
        entries = []
        for index, record in enumerate(page_records):
            page = record.get("page", index + 1) if isinstance(record, dict) else index + 1
            entries.append((page, record))
    else:
        raise TypeError("page_records must be a mapping or sequence")

    # JSON object keys make "1" and 1 equivalent.  Resolve such legacy input
    # deterministically rather than returning duplicate page assignments.
    normalized_entries = {}
    for raw_page, record in sorted(entries, key=lambda item: _page_sort_key(item[0])):
        page = _clean_page_id(raw_page)
        normalized_entries.setdefault(page, record)
    pages = sorted(normalized_entries, key=_page_sort_key)
    input_revision = content_revision([
        {"page": page, "record": normalized_entries[page]} for page in pages
    ], "lfi")
    features = {
        page: _layout_features(normalized_entries[page], max_regions_per_page)
        for page in pages
    }

    parameters = {
        "similarity_threshold": similarity_threshold,
        "min_family_size": min_family_size,
        "low_confidence_threshold": low_confidence_threshold,
        "max_families": max_families,
        "max_regions_per_page": max_regions_per_page,
    }
    base_result = {
        "proposal_type": "replica.layout-families",
        "schema_version": 1,
        "status": "proposal",
        "canonical": False,
        "method": "geometry-role-average-link-medoid-v1",
        "input_revision": input_revision,
        "parameters": parameters,
        "page_count": len(pages),
        "families": [],
        "exceptions": [],
    }
    if not pages:
        return base_result

    pair_metrics = {}
    for left_index, left_page in enumerate(pages):
        for right_page in pages[left_index + 1:]:
            pair_metrics[(left_page, right_page)] = _layout_similarity(
                features[left_page], features[right_page]
            )

    def metrics(left_page, right_page):
        if left_page == right_page:
            return {"score": 1.0, "geometry": 1.0, "role_overlap": 1.0,
                    "coverage": 1.0}
        key = tuple(sorted((left_page, right_page), key=_page_sort_key))
        return pair_metrics[key]

    clusters = _average_link_clusters(
        pages, pair_metrics, similarity_threshold
    )

    def medoid(cluster):
        ranked = []
        for page in cluster:
            similarity = _mean(metrics(page, other)["score"]
                               for other in cluster if other != page)
            ranked.append((similarity, page))
        ranked.sort(key=lambda value: (-value[0], _page_sort_key(value[1])))
        return ranked[0][1]

    recurring = []
    exceptions = []
    member_floor = max(0.0, similarity_threshold - 0.08)
    for cluster in clusters:
        if len(cluster) < min_family_size:
            for page in cluster:
                reasons = (["no-usable-regions"] if not features[page]["region_count"]
                           else ["singleton-layout"])
                if features[page]["truncated"]:
                    reasons.append("region-cap-applied")
                exceptions.append({"page": page, "source_revision": content_revision(
                    normalized_entries[page], "lpr"), "reasons": reasons})
            continue
        representative = medoid(cluster)
        strong = []
        weak = []
        for page in cluster:
            similarity = metrics(page, representative)["score"]
            evidence = _evidence_factor(features[page])
            confidence = similarity * evidence
            if (not features[page]["region_count"] or
                    similarity < member_floor or confidence < low_confidence_threshold):
                weak.append((page, similarity, confidence))
            else:
                strong.append(page)
        if len(strong) < min_family_size:
            weak_pages = {page for page, _similarity, _confidence in weak}
            for page in cluster:
                reasons = ["low-layout-evidence" if page in weak_pages
                           else "singleton-layout"]
                exceptions.append({"page": page, "source_revision": content_revision(
                    normalized_entries[page], "lpr"), "reasons": reasons})
            continue
        # Removal of a weak page can change the most representative member.
        representative = medoid(tuple(strong))
        cohesion = _mean(metrics(left, right)["score"]
                         for index, left in enumerate(strong)
                         for right in strong[index + 1:])
        recurring.append({
            "pages": tuple(sorted(strong, key=_page_sort_key)),
            "representative": representative,
            "cohesion": cohesion,
        })
        for page, similarity, confidence in weak:
            reasons = ["low-layout-evidence" if confidence < low_confidence_threshold
                       else "low-family-similarity"]
            exceptions.append({"page": page, "source_revision": content_revision(
                normalized_entries[page], "lpr"), "reasons": reasons})

    # The cap limits the review surface without inventing similarity.  Smaller
    # recurring clusters beyond it become exceptions, never forced members.
    recurring.sort(key=lambda family: (
        -len(family["pages"]), -family["cohesion"],
        _page_sort_key(family["representative"])
    ))
    kept = recurring[:max_families]
    for family in recurring[max_families:]:
        for page in family["pages"]:
            exceptions.append({"page": page, "source_revision": content_revision(
                normalized_entries[page], "lpr"), "reasons": ["layout-family-cap"]})

    families = []
    for family in kept:
        family_id_payload = {
            "method": base_result["method"],
            "pages": list(family["pages"]),
            "representative": family["representative"],
        }
        family_id = "layout-" + hashlib.sha256(json.dumps(
            family_id_payload, sort_keys=True, separators=(",", ":"),
            ensure_ascii=False
        ).encode("utf-8")).hexdigest()[:16]
        members = []
        for page in family["pages"]:
            page_metrics = metrics(page, family["representative"])
            confidence = page_metrics["score"] * _evidence_factor(features[page])
            reasons = []
            if page == family["representative"]:
                reasons.append("representative-medoid")
            else:
                reasons.append("average-link-family-member")
            if page_metrics["geometry"] >= similarity_threshold:
                reasons.append("geometry-pattern-match")
            if page_metrics["role_overlap"] >= 0.75:
                reasons.append("semantic-role-pattern-match")
            if features[page]["truncated"]:
                reasons.append("region-cap-applied")
            members.append({
                "page": page,
                "similarity": round(page_metrics["score"], 4),
                "confidence": round(confidence, 4),
                "reasons": reasons,
                "source_revision": content_revision(normalized_entries[page], "lpr"),
            })
        family_confidence = _mean(member["confidence"] for member in members)
        families.append({
            "family_id": family_id,
            "representative_page": family["representative"],
            "member_pages": list(family["pages"]),
            "confidence": round(family_confidence, 4),
            "reasons": ["recurring-layout", (
                "high-geometry-role-cohesion" if family["cohesion"] >= 0.88
                else "moderate-geometry-role-cohesion"
            )],
            "members": members,
        })
    families.sort(key=lambda family: _page_sort_key(family["representative_page"]))

    # Give every exception its nearest proposed family so a UI can offer one
    # click correction, without treating that suggestion as an assignment.
    for exception in exceptions:
        page = exception["page"]
        nearest = []
        for family in families:
            representative = family["representative_page"]
            nearest.append((metrics(page, representative)["score"],
                            family["family_id"], representative))
        nearest.sort(key=lambda value: (-value[0], value[1]))
        if nearest:
            similarity, family_id, _representative = nearest[0]
            exception["nearest_family_id"] = family_id
            exception["similarity"] = round(similarity, 4)
            exception["confidence"] = round(
                similarity * _evidence_factor(features[page]), 4
            )
        else:
            exception["similarity"] = 0.0
            exception["confidence"] = 0.0
    exceptions.sort(key=lambda exception: _page_sort_key(exception["page"]))

    base_result["families"] = families
    base_result["exceptions"] = exceptions
    return base_result
