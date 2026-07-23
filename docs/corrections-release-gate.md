# Corrections release gate

The final Corrections gate is executable at the engine/browser-contract level:

```text
python -m pytest -q tests/test_corrections_release_gate.py
node --test tests/corrections_release_gate_behavior.test.js
```

The Python gate composes the durable review repository, transform store, job
manager, and worker without Flask. It proves that a host-owned transform keeps
running while the first Corrections window is discarded, a reopened window
resolves and reopens the review, OCR fails independently after the image
commit, and verified roles, captions, and text survive. It also covers
pre-commit cancellation, process-restart interruption, exact terminal replay,
a successful new-operation restart, and immutable original bytes.

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

These tests deliberately make no claim about a live browser, screen reader,
physical Android device, network provider, or signed packaged binary. Those
remain separate release-validation gates; the repository-wide test command
continues to own existing Replica, legacy `.lib`, Android v1, updater, and
resource-window compatibility smoke coverage.
