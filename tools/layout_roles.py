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


def classify(regions: list[dict]) -> None:
    """Reassign early-print roles among the body-typed regions, in place.

    The main text column (the "band") is anchored by the widest body block
    and widened by every block that substantially overlaps it. Then:
    marginalia sits mostly outside the band AND is much narrower than it
    (the width guard keeps two-column index pages intact — a second column
    is band-wide, a margin note is not); a catchword is a lone bottom token
    at the band's right edge; a signature mark is a short letter+digit
    compositor code bottom-center; a page number is a small numeral in the
    top or bottom margin."""
    body = [r for r in regions if r["role"] == "body"]
    if not body:
        return
    anchor = max(body, key=lambda r: r["box"]["w"])
    bx0, bx1 = anchor["box"]["x"], anchor["box"]["x"] + anchor["box"]["w"]
    for r in body:
        x0, x1 = r["box"]["x"], r["box"]["x"] + r["box"]["w"]
        ov = max(0.0, min(x1, bx1) - max(x0, bx0))
        if r["box"]["w"] > 0 and ov / r["box"]["w"] >= 0.5:
            bx0, bx1 = min(bx0, x0), max(bx1, x1)
    band_w = max(1e-6, bx1 - bx0)
    for r in body:
        box = r["box"]
        word = re.sub(r"\s+", " ", r["text"].strip())
        x0, x1 = box["x"], box["x"] + box["w"]
        ov = max(0.0, min(x1, bx1) - max(x0, bx0)) / max(1e-6, box["w"])
        top = box["y"] + box["h"] < 0.12
        bottom = box["y"] > 0.82
        if (top or bottom) and box["w"] < 0.15 and _PAGENO.match(word or " "):
            r["role"] = "page-number"
        elif (bottom and box["w"] < 0.25 and word and " " not in word
                and x1 >= bx1 - 0.15 * band_w):
            r["role"] = "catch-word"
        elif (bottom and box["w"] < 0.12 and _SIGMARK.match(word or " ")):
            r["role"] = "signature-mark"
        elif ov < 0.3 and box["w"] < 0.5 * band_w:
            r["role"] = "marginalia"


def compose_text(regions: list[dict]) -> str:
    """The body flow: every non-furniture region's text in reading order.
    Figure regions keep their place — their block content IS the markdown
    ![id](id) placeholder, so downstream figure-reference rewriting keeps
    working on the composed text."""
    parts = [r["text"].strip()
             for r in sorted(regions, key=lambda r: r.get("order", 0))
             if r["role"] not in SECONDARY_ROLES and r["text"].strip()]
    return "\n\n".join(parts)


def coverage(regions: list[dict], markdown: str) -> float:
    """Fraction (0..1) of the markdown's characters the blocks carry.
    Guards the compiled text: when segmentation misses too much of the page,
    the caller keeps the full markdown rather than silently lose text."""
    md = re.sub(r"\s+", "", markdown or "")
    if not md:
        return 1.0
    blk = re.sub(r"\s+", "", "".join(r["text"] for r in regions))
    return min(1.0, len(blk) / len(md))
