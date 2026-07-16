// In-book text search. Pure functions over [{page, body}] rows -- no fetch,
// no DOM -- so the reader and the tests share one code path (issue #138).
//
// The corpus is early-modern print run through OCR, so a literal substring
// match would miss most of it: "physic" must find "physick", "phyſick", and
// "phy-\nsick" split across a line break. Matching therefore runs over a
// folded shadow of the text (lowercase, long s and ligatures expanded,
// diacritics stripped, hyphenated line breaks joined, whitespace collapsed)
// while an offset map carries every folded character back to its position in
// the original string -- snippets and <mark> ranges are always cut from the
// text the reader actually sees.

// Early-modern glyphs folded to their modern search forms. U+FB05 is the
// long-s+t ligature, so it lands on "st" like every other long s.
const LIGATURES = {
  "ſ": "s",                       // long s
  "ﬀ": "ff", "ﬁ": "fi", "ﬂ": "fl",
  "ﬃ": "ffi", "ﬄ": "ffl",
  "ﬅ": "st", "ﬆ": "st",
  "æ": "ae", "œ": "oe",      // ae / oe ligature vowels
};

const isSpace = (c) => /\s/.test(c);

// One character -> its folded form: lowercase, ligature-expanded, then NFD
// with the combining marks stripped ("ú" -> "u"). May emit 0..3 characters.
function foldChar(c) {
  const lower = c.toLowerCase();
  const base = LIGATURES[lower] !== undefined ? LIGATURES[lower] : lower;
  return base.normalize("NFD").replace(/[\u0300-\u036f]/g, "");
}

/** The folded shadow of a string plus its offset map: text[i] came from
 *  original[starts[i] .. ends[i]) -- ends exclusive, so a match [a, b) in the
 *  folded text is original.slice(starts[a], ends[b - 1]). */
function buildSearchIndex(raw) {
  const s = String(raw || "");
  let text = "";
  const starts = [];
  const ends = [];
  let i = 0;
  while (i < s.length) {
    const c = s[i];
    // A hyphen carried across a line break is a typesetting artefact, not a
    // character of the word: "phy-\nsick" folds to "physick". The soft hyphen
    // (U+00AD) and the dedicated hyphen (U+2010) count too.
    if (c === "-" || c === "\u00ad" || c === "\u2010") {
      let j = i + 1;
      while (j < s.length && (s[j] === " " || s[j] === "\t" || s[j] === "\r")) j++;
      if (j < s.length && s[j] === "\n") {
        j++;
        while (j < s.length && isSpace(s[j])) j++;
        i = j;
        continue;
      }
      if (c === "\u00ad") { i++; continue; }   // a soft hyphen is invisible anywhere
    }
    if (isSpace(c)) {
      let j = i + 1;
      while (j < s.length && isSpace(s[j])) j++;
      if (text.length && text[text.length - 1] !== " ") {   // collapse runs, never lead
        text += " ";
        starts.push(i);
        ends.push(j);
      }
      i = j;
      continue;
    }
    for (const f of foldChar(c)) {
      text += f;
      starts.push(i);
      ends.push(i + 1);
    }
    i++;
  }
  if (text.endsWith(" ")) {                    // never trail either
    text = text.slice(0, -1);
    starts.pop();
    ends.pop();
  }
  return { text, starts, ends };
}

/** A string as the matcher sees it: lowercased, folded, single-spaced. */
export function normalizeSearchText(s) {
  return buildSearchIndex(s).text;
}

/** Every non-overlapping match of `query` in `body`, as [start, end) offset
 *  pairs into the ORIGINAL body -- ready for an escape-then-<mark> splice. */
export function findMatchRanges(body, query) {
  const q = normalizeSearchText(query);
  const ranges = [];
  if (!q) return ranges;
  const idx = buildSearchIndex(body);
  let at = idx.text.indexOf(q);
  while (at !== -1) {
    const last = at + q.length - 1;
    ranges.push([idx.starts[at], idx.ends[last]]);
    at = idx.text.indexOf(q, at + q.length);
  }
  return ranges;
}

const HITS_PER_PAGE = 3;      // beyond this a page reports "+N more"
const SNIPPET_CONTEXT = 60;   // characters of original text kept on each side

// One hit: the snippet is cut from the original body around [start, end),
// trimmed to word boundaries, with the match located inside it for <mark>.
function snippetHit(page, body, start, end) {
  let s = Math.max(0, start - SNIPPET_CONTEXT);
  let e = Math.min(body.length, end + SNIPPET_CONTEXT);
  if (s > 0 && !isSpace(body[s - 1])) {        // landed mid-word: start at the next one
    const sp = body.slice(s, start).search(/\s/);
    if (sp >= 0) s += sp + 1;
  }
  if (e < body.length && !isSpace(body[e])) {  // landed mid-word: drop the fragment
    const sp = body.slice(end, e).search(/\s\S*$/);
    if (sp >= 0) e = end + sp;
  }
  return {
    page,
    snippet: body.slice(s, e),
    matchStart: start - s,
    matchEnd: end - s,
    cutStart: s > 0,
    cutEnd: e < body.length,
    more: 0,
  };
}

/** Search [{page, body}] rows: hits in page order, at most HITS_PER_PAGE per
 *  page, the page's last hit carrying the count of matches left unreported. */
export function searchPages(pages, query) {
  const q = normalizeSearchText(query);
  const hits = [];
  if (!q) return hits;
  for (const row of pages || []) {
    const body = String(row.body || "");
    const ranges = findMatchRanges(body, q);
    if (!ranges.length) continue;
    for (const [s, e] of ranges.slice(0, HITS_PER_PAGE)) {
      hits.push(snippetHit(row.page, body, s, e));
    }
    hits[hits.length - 1].more = Math.max(0, ranges.length - HITS_PER_PAGE);
  }
  return hits;
}
