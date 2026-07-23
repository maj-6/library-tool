# Corrections release gate

The final Corrections gate includes a live headless production-transport flow:

```text
python -m pytest -q tests/test_corrections_release_gate.py tests/test_corrections_server_bridge.py
node --test tests/corrections_release_gate_behavior.test.js
```

The representative flow runs the real `EngineClient` over loopback HTTP against
the production Flask composition and filesystem engine. It opens an
attention-marked captured book with two Mistral regions, reads Books and
Artifacts, assigns an image category and the canonical `marginalia`/`figure`
roles (`MAR`/`ILL` in the UI), edits a caption, moves the screen-nearest quad
corner, changes Image Adjust brightness, requests OCR, and submits Space. The
filesystem commit is deliberately held while the first client is discarded and
a new client resolves and reopens the review. After release, the new client
observes the corrected rendition and OCR proposal and verifies the human
category, roles, and caption survived. The same fixture seals and re-reads the
capture's current `.lib/3` association.

The lower-level Python gate separately composes the durable review repository,
transform store, job manager, and worker. It proves OCR failure remains
independent after an image commit and covers pre-commit cancellation,
process-restart interruption, exact terminal replay, a successful new-operation
restart, and immutable original bytes.

The JavaScript gate exercises the actual editor, Image Adjust, Books, and
Artifacts contract modules. It proves that Space retry retains one serialized
command, duplicate Space is inert after acceptance, a reopened window can
consume the persisted result, cancellation does not change the remembered
profile, and OCR failure remains visible after image success.

Performance fixture budgets come from
[`ui-ux-redesign-spec.md`](ui-ux-redesign-spec.md#16-performance-and-feedback-budgets):

- 5,000 capture-thumbnail summaries plus a virtual 10,000-artifact tree must
  project a cached browsing window in less than 200 ms.
- The artifact window mounts at most 18 rows for a 10-row viewport with
  four-row overscan, while keeping the active descendant mounted.
- A burst of 100 screen-nearest pointer updates must begin and complete its
  synchronous local feedback in less than 100 ms.

The release workflow also runs Android unit tests, debug/release lint, and a
debug assembly before packaging. Those tests consume current/stale/malformed
association confirmations and assert the accessible confirmed-marker states;
the signed packaging job then verifies the release APK certificate.

These tests deliberately make no claim about a live Chromium renderer, a real
screen reader, a physical Android device, an external OCR provider, or a signed
installer exercise. Those remain separate manual or packaging validations; the
repository-wide test command continues to own existing Replica, legacy `.lib`,
Android v1, updater, and resource-window compatibility smoke coverage.
