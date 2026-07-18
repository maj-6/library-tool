"""Typed page regions from Mistral OCR-4 blocks — the Phase 1 substrate of
docs/facsimile-workbench-plan.md.

OCR-4 (`include_blocks`) returns paragraph-grain bounding boxes in reading
order, but its type labels are trained on modern print: on hand-press pages
marginalia, catchwords, and signature marks all arrive typed plain `text`
(Phase 0 findings — `aside_text` never fired on 1605–1792 samples). The
geometry, though, is excellent. So the mapping here trusts Mistral for the
roles it does get right (header/footer/title/caption/figure/table) and
assigns the early-print roles geometrically from the page's own shape.

Role names follow PAGE XML's early-print vocabulary (marginalia, catch-word,
signature-mark, page-number, …) so a future PAGE/TEI export maps 1:1. The
vocabulary is open: an unmapped block type degrades to `body`, never fails.
"""
from __future__ import annotations

import re

# Mistral block type -> role. Types absent here (and future unknown ones)
# flow as body text rather than getting lost.
MISTRAL_ROLES = {
    "text": "body",
    "list": "body",
    "references": "body",   # OCR-4 mislabels early-print body text this way
    "equation": "body",
    "code": "body",
    "title": "title",
    "caption": "caption",
    "table": "table",
    "image": "figure",
    "header": "header",
    "footer": "footer",
    "aside_text": "marginalia",
    "signature": "signature-mark",
}

# Page furniture: excluded from the body flow that feeds compiled text,
# translations, and volume_pages. Everything else is content.
SECONDARY_ROLES = {"marginalia", "header", "footer", "page-number",
                   "catch-word", "signature-mark"}

_PAGENO = re.compile(r"^[\divxlc]{1,7}[.\s]*$", re.I)      # 102 / xvii / 42.
_SIGMARK = re.compile(r"^[A-Za-z]{1,3}\.?\s?\d{0,2}\.?$")  # B2 / Aa3 / C.


def regions_from_blocks(blocks: list | None, dims: dict | None) -> list[dict]:
    """Convert one page's Mistral blocks (pixel corners) into region records
    with boxes normalised to 0..1 of the page — the same convention as the
    word/figure sidecars — then classify them. Region: {id, role, src_type,
    order, box: {x,y,w,h}, text}."""
    pw = float((dims or {}).get("width") or 0)
    ph = float((dims or {}).get("height") or 0)
    out: list[dict] = []
    if pw <= 0 or ph <= 0:
        return out
    for i, blk in enumerate(blocks or []):
        try:
            x0 = float(blk.get("top_left_x") or 0)
            y0 = float(blk.get("top_left_y") or 0)
            x1 = float(blk.get("bottom_right_x") or 0)
            y1 = float(blk.get("bottom_right_y") or 0)
        except (TypeError, ValueError):
            continue
        if x1 <= x0 or y1 <= y0:
            continue
        t = str(blk.get("type") or "").lower()
        out.append({
            "id": f"r{i}",
            "role": MISTRAL_ROLES.get(t, "body"),
            "src_type": t,
            "order": i,
            "box": {"x": round(x0 / pw, 5), "y": round(y0 / ph, 5),
                    "w": round((x1 - x0) / pw, 5), "h": round((y1 - y0) / ph, 5)},
            "text": str(blk.get("content") or ""),
        })
    classify(out)
    return out


def _column_band(body: list[dict]) -> tuple[float, float]:
    """The main text column's x-extent, from clustering the body blocks.

    Blocks whose x-intervals overlap by >=50% of the narrower one belong to
    one column; clusters merge to a fixpoint (a single extension pass made
    the outcome depend on the order Mistral emitted blocks). The band is the
    cluster that spans the page's horizontal centre — a margin note hugs an
    edge, a text column almost never does — falling back to the largest
    total ink area when no cluster reaches the centre (a plates-and-notes
    page: erring toward keeping text as body, never losing it)."""
    clusters: list[dict] = []
    for r in sorted(body, key=lambda r: (r["box"]["x"], r["box"]["w"])):
        x0, x1 = r["box"]["x"], r["box"]["x"] + r["box"]["w"]
        clusters.append({"x0": x0, "x1": x1,
                         "area": r["box"]["w"] * r["box"]["h"]})
    merged = True
    while merged:
        merged = False
        for i in range(len(clusters)):
            for j in range(i + 1, len(clusters)):
                a, b = clusters[i], clusters[j]
                ov = min(a["x1"], b["x1"]) - max(a["x0"], b["x0"])
                narrower = max(1e-6, min(a["x1"] - a["x0"], b["x1"] - b["x0"]))
                if ov > 0 and ov / narrower >= 0.5:
                    a["x0"] = min(a["x0"], b["x0"])
                    a["x1"] = max(a["x1"], b["x1"])
                    a["area"] += b["area"]
                    del clusters[j]
                    merged = True
                    break
            if merged:
                break
    best = max(clusters,
               key=lambda c: (c["x0"] <= 0.5 <= c["x1"], c["area"]))
    return best["x0"], best["x1"]


def classify(regions: list[dict]) -> None:
    """Reassign early-print roles among the body-typed regions, in place.

    The main text column (the "band") comes from _column_band. Then:
    marginalia sits mostly outside the band AND is much narrower than it
    (the width guard keeps two-column index pages intact — a second column
    is band-wide, a margin note is not); a catchword is a lone bottom token
    at the band's right edge; a signature mark is a short letter+digit
    compositor code at the bottom (checked before page-number when it has a
    digit — "B2" — since a bare roman letter like "C." is indistinguishable
    from a folio number without book-level context and defaults to
    page-number); a page number is a small numeral in the top or bottom
    margin; a drop capital is a tiny one-letter block with body text
    starting at its right shoulder; a chapter heading is a single line set
    visibly larger than the band's body; a footnote is band text set
    visibly smaller, low on the page."""
    body = [r for r in regions if r["role"] == "body"]
    if not body:
        return
    bx0, bx1 = _column_band(body)
    band_w = max(1e-6, bx1 - bx0)

    def lines_of(r):
        return max(1, len([ln for ln in r["text"].split("\n") if ln.strip()]))

    def line_h(r):
        return r["box"]["h"] / lines_of(r)

    # the band's body line height, from its multi-line blocks — headings and
    # stray one-liners must not pollute the yardstick they're measured by
    def band_ov(r):
        x0, x1 = r["box"]["x"], r["box"]["x"] + r["box"]["w"]
        return max(0.0, min(x1, bx1) - max(x0, bx0)) / max(1e-6, r["box"]["w"])
    yard = sorted(line_h(r) for r in body
                  if band_ov(r) >= 0.5 and lines_of(r) >= 2)
    med_lh = yard[len(yard) // 2] if yard else 0.0

    for r in body:
        box = r["box"]
        word = re.sub(r"\s+", " ", r["text"].strip())
        x0, x1 = box["x"], box["x"] + box["w"]
        ov = max(0.0, min(x1, bx1) - max(x0, bx0)) / max(1e-6, box["w"])
        top = box["y"] + box["h"] < 0.12
        bottom = box["y"] > 0.82
        cy = box["y"] + box["h"] / 2
        # a one-or-two-letter block with a body block starting at its right
        # shoulder: the letter the compositor set large to open the paragraph
        is_cap = (len(word) in (1, 2) and word.isalpha()
                  and box["w"] < 0.12 and box["h"] < 0.15
                  and 0.3 < box["w"] / max(1e-6, box["h"]) < 3.0
                  and any(o is not r and o["role"] == "body"
                          and lines_of(o) >= 2
                          and o["box"]["x"] >= x1 - 0.02
                          and o["box"]["x"] <= x1 + 0.06
                          and o["box"]["y"] <= cy <=
                              o["box"]["y"] + o["box"]["h"]
                          for o in body))
        if is_cap:
            r["role"] = "drop-capital"
        elif (bottom and box["w"] < 0.12 and _SIGMARK.match(word or " ")
                and any(ch.isdigit() for ch in word)):
            r["role"] = "signature-mark"
        elif (top or bottom) and box["w"] < 0.15 and _PAGENO.match(word or " "):
            r["role"] = "page-number"
        elif (bottom and box["w"] < 0.25 and word and " " not in word
                and x1 >= bx1 - 0.15 * band_w):
            r["role"] = "catch-word"
        elif (bottom and box["w"] < 0.12 and _SIGMARK.match(word or " ")):
            r["role"] = "signature-mark"
        elif (med_lh > 0 and ov >= 0.5 and lines_of(r) == 1 and word
                and box["w"] < 0.8 * band_w and line_h(r) > 1.35 * med_lh):
            r["role"] = "title"
        elif (med_lh > 0 and ov >= 0.5 and box["y"] > 0.7
                and line_h(r) < 0.75 * med_lh and lines_of(r) >= 1 and word):
            r["role"] = "footnote"
        elif ov < 0.3 and box["w"] < 0.5 * band_w:
            r["role"] = "marginalia"


def compose_text(regions: list[dict], layer: str = "text") -> str:
    """The body flow: every non-furniture region's text in reading order.
    Figure regions keep their place — their block content IS the markdown
    ![id](id) placeholder, so downstream figure-reference rewriting keeps
    working on the composed text. `layer` picks an alternate text layer per
    region (e.g. "norm", the human-curated normalized reading), falling back
    to the diplomatic `text` where the layer is empty — a partially
    normalized page still composes complete."""
    def txt(r):
        if layer != "text":
            v = str(r.get(layer) or "").strip()
            if v:
                return v
        return str(r.get("text") or "").strip()
    parts = []
    pending_cap = ""
    for r in sorted(regions, key=lambda r: r.get("order", 0)):
        if r["role"] in SECONDARY_ROLES:
            continue
        t = txt(r)
        if not t:
            continue
        # a drop capital is the first letter of the paragraph it opens —
        # it joins the NEXT text region seamlessly, never stands alone.
        # Figures pass through untouched: prefixing a letter onto an
        # ![id](id) placeholder would corrupt the reference
        if r["role"] == "drop-capital":
            pending_cap += t
            continue
        if pending_cap and r["role"] != "figure":
            t = pending_cap + t
            pending_cap = ""
        parts.append(t)
    if pending_cap:
        parts.append(pending_cap)
    return "\n\n".join(parts)


def box_iou(a: dict, b: dict) -> float:
    """Intersection-over-union of two {x,y,w,h} boxes (0..1 fractions)."""
    ax1, ay1 = a["x"] + a["w"], a["y"] + a["h"]
    bx1, by1 = b["x"] + b["w"], b["y"] + b["h"]
    iw = min(ax1, bx1) - max(a["x"], b["x"])
    ih = min(ay1, by1) - max(a["y"], b["y"])
    if iw <= 0 or ih <= 0:
        return 0.0
    inter = iw * ih
    union = a["w"] * a["h"] + b["w"] * b["h"] - inter
    return inter / union if union > 0 else 0.0


def template_score(tpl_items: list[dict], page_items: list[dict]) -> float:
    """How well a page's regions fit a layout template: the mean, over the
    template's regions, of the best IoU any page region achieves against it.
    1.0 = the grid held; low = the page broke the grid (a plate, a chapter
    opening) and deserves the human's attention. Geometry only — roles may
    legitimately differ after reclassification."""
    if not tpl_items:
        return 1.0
    best = [max((box_iou(t.get("box") or {}, p.get("box") or {})
                 for p in page_items), default=0.0) for t in tpl_items]
    return sum(best) / len(best)


def distribute_text(text: str, weights: list) -> list:
    """Split page-aligned text across body regions, weighted by each
    region's diplomatic length, breaking only at paragraph boundaries — the
    server-side twin of the workbench preview's distribution, used by the
    print renderer for translated editions."""
    if not weights:
        return []
    paras = [p for p in str(text or "").split("\n\n") if p.strip()]
    out = [[] for _ in weights]
    if not paras:
        return ["" for _ in weights]
    total = sum(weights) or 1
    total_chars = sum(len(p) for p in paras) or 1
    wi, acc = 0, 0
    for p in paras:
        out[min(wi, len(out) - 1)].append(p)
        acc += len(p)
        filled = sum(weights[:wi + 1]) / total
        while wi < len(weights) - 1 and acc / total_chars >= filled:
            wi += 1
    return ["\n\n".join(chunk) for chunk in out]


def clip_words_to_box(words: list, box: dict) -> str:
    """The text of every word box whose centre falls inside `box`, rebuilt
    into lines: grouped by the OCR engine's line id when present, else by
    1%-of-page-height bands; lines top-to-bottom, words left-to-right. This
    is the server-side twin of the workbench's Clip words — what template
    application uses to pre-fill a region's text from existing geometry."""
    inside = []
    for w in words or []:
        t = str((w or {}).get("t") or "")
        if not t.strip():
            continue
        try:
            x = float(w.get("x") or 0)
            y = float(w.get("y") or 0)
            ww = float(w.get("w") or 0)
            h = float(w.get("h") or 0)
        except (TypeError, ValueError):
            continue
        cx, cy = x + ww / 2, y + h / 2
        if (box["x"] <= cx <= box["x"] + box["w"]
                and box["y"] <= cy <= box["y"] + box["h"]):
            inside.append((w.get("l"), y, x, t))
    if not inside:
        return ""
    lines: dict = {}
    for lid, y, x, t in inside:
        key = lid if isinstance(lid, int) else ("y", round(y * 100))
        lines.setdefault(key, []).append((y, x, t))
    out = []
    for key in sorted(lines, key=lambda k: min(r[0] for r in lines[k])):
        out.append(" ".join(t for _y, _x, t in sorted(lines[key],
                                                      key=lambda r: r[1])))
    return "\n".join(out)


def coverage(regions: list[dict], markdown: str) -> float:
    """Fraction (0..1) of the markdown's characters the blocks carry.
    Guards the compiled text: when segmentation misses too much of the page,
    the caller keeps the full markdown rather than silently lose text."""
    md = re.sub(r"\s+", "", markdown or "")
    if not md:
        return 1.0
    blk = re.sub(r"\s+", "", "".join(r["text"] for r in regions))
    return min(1.0, len(blk) / len(md))
