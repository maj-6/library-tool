// A deliberately small Markdown renderer for volume_texts.body.
//
// Security stance (see docs/library-analyze-design.md §3): the body is
// attacker-influenceable, so we ESCAPE FIRST — the entire source is HTML-escaped
// before any markdown pattern runs — and there is NO raw-HTML pass-through. The
// only way a "<" reaches the DOM is as &lt;. Link targets additionally go
// through safeHttpUrl(), so a `javascript:` href cannot survive. The worst a
// hostile string can do is render as ugly text.

import { safeHttpUrl } from "./data.js";

const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) =>
  ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

// Undo the basic entity escaping for a captured URL so it can be parsed, then
// it is validated and re-escaped for the attribute — never trusted raw.
const unent = (s) => String(s)
  .replace(/&amp;/g, "&").replace(/&lt;/g, "<").replace(/&gt;/g, ">")
  .replace(/&quot;/g, '"').replace(/&#39;/g, "'");

function inline(text) {
  let t = text;
  // inline code first, so emphasis inside it is left alone
  t = t.replace(/`([^`]+)`/g, (_, c) => `<code>${c}</code>`);
  // bold, then italic (bold consumes the doubled markers first)
  t = t.replace(/\*\*([^*\n]+)\*\*/g, "<strong>$1</strong>");
  t = t.replace(/(^|[^*])\*([^*\n]+)\*/g, "$1<em>$2</em>");
  t = t.replace(/(^|[^_\w])_([^_\n]+)_/g, "$1<em>$2</em>");
  // links [text](url) — url validated by safeHttpUrl, then re-escaped
  t = t.replace(/\[([^\]]+)\]\(([^)\s]+)\)/g, (m, label, url) => {
    const safe = safeHttpUrl(unent(url));
    if (!safe) return label;
    return `<a href="${esc(safe)}" target="_blank" rel="noopener nofollow">${label}</a>`;
  });
  return t;
}

/** Markdown string -> HTML string. Escaping happens before anything else. */
export function renderMarkdown(src) {
  const lines = esc(String(src ?? "")).replace(/\r\n?/g, "\n").split("\n");
  const out = [];
  let i = 0;
  let para = [];
  const flushPara = () => {
    if (para.length) { out.push(`<p>${inline(para.join(" "))}</p>`); para = []; }
  };

  while (i < lines.length) {
    const line = lines[i];

    // fenced code block
    if (/^```/.test(line)) {
      flushPara();
      const buf = [];
      i++;
      while (i < lines.length && !/^```/.test(lines[i])) { buf.push(lines[i]); i++; }
      i++; // closing fence
      out.push(`<pre><code>${buf.join("\n")}</code></pre>`);
      continue;
    }

    // blank line
    if (/^\s*$/.test(line)) { flushPara(); i++; continue; }

    // heading
    const h = line.match(/^(#{1,6})\s+(.*)$/);
    if (h) { flushPara(); out.push(`<h${h[1].length}>${inline(h[2].trim())}</h${h[1].length}>`); i++; continue; }

    // horizontal rule
    if (/^(-{3,}|\*{3,}|_{3,})\s*$/.test(line)) { flushPara(); out.push("<hr>"); i++; continue; }

    // blockquote (escaped ">" is "&gt;")
    if (/^&gt;\s?/.test(line)) {
      flushPara();
      const buf = [];
      while (i < lines.length && /^&gt;\s?/.test(lines[i])) {
        buf.push(lines[i].replace(/^&gt;\s?/, "")); i++;
      }
      out.push(`<blockquote>${inline(buf.join(" "))}</blockquote>`);
      continue;
    }

    // unordered list
    if (/^\s*[-*+]\s+/.test(line)) {
      flushPara();
      const items = [];
      while (i < lines.length && /^\s*[-*+]\s+/.test(lines[i])) {
        items.push(`<li>${inline(lines[i].replace(/^\s*[-*+]\s+/, ""))}</li>`); i++;
      }
      out.push(`<ul>${items.join("")}</ul>`);
      continue;
    }

    // ordered list
    if (/^\s*\d+\.\s+/.test(line)) {
      flushPara();
      const items = [];
      while (i < lines.length && /^\s*\d+\.\s+/.test(lines[i])) {
        items.push(`<li>${inline(lines[i].replace(/^\s*\d+\.\s+/, ""))}</li>`); i++;
      }
      out.push(`<ol>${items.join("")}</ol>`);
      continue;
    }

    // paragraph text
    para.push(line.trim());
    i++;
  }
  flushPara();
  return out.join("\n");
}
