# Rights policy

Every catalog build carries a `rights` field — the curator's explicit
publication-rights decision, set in the desktop Editor (Entry pane, with a
"Suggest" check against the offline copyright-renewal data). It publishes to
`volumes.copyright_status` and decides what of the book may go public.

| Decision | Site shows | What may publish |
| --- | --- | --- |
| `public-domain` | Public domain | Full page text, translations, notes, and (future) search index |
| `cleared` | Cleared | Same as public-domain — rights were cleared some other way |
| `searchable-only` | Search only | No public page text, translations, or notes; snippets only in a future search index (#140) |
| `no-public-text` | Restricted | Record, PDF and About article only; nothing text-indexed |
| `""` (undecided) | — | Cannot publish at all |

The About article is the curator's own writing, not the book's text, so it
publishes under every decided state. Notes carry verbatim quotes, so they
count as the book's text.

## Where each rule is enforced

- **Undecided blocks publishing**: `POST /api/volumes/publish`
  (`tools/whl_explorer/server.py`) returns 400 for a build with no decision.
- **Text gating**: `_rights_artifacts` in the same file strips page text,
  translations, and notes from the effective bundle for non-permitting
  states; `_publish_bundle`'s pruning then also deletes any text rows a
  previous publish sent.
- **Status publication**: `_volume_row` maps `rights` to the display strings
  above; `tools/backfill_rights.py` backfills rows published before this
  existed.
- **Search index**: the future index build (#140) must filter on
  `volumes.copyright_status` — full text only for "Public domain"/"Cleared",
  snippets for "Search only", nothing for "Restricted".
