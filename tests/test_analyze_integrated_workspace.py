from __future__ import annotations

from pathlib import Path

import server


ROOT = Path(__file__).parents[1]
APP = (ROOT / "tools" / "whl_explorer" / "static" / "app.js").read_text(
    encoding="utf-8")
STYLE = (ROOT / "tools" / "whl_explorer" / "static" / "style.css").read_text(
    encoding="utf-8")
TEMPLATE = (ROOT / "tools" / "whl_explorer" / "templates" / "index.html").read_text(
    encoding="utf-8")


def _between(text: str, start: str, end: str) -> str:
    return text.split(start, 1)[1].split(end, 1)[0]


def _ready_build(client, title: str) -> dict:
    response = client.post("/api/builds", json={"build": {
        "title": title,
        "status": "ready",
    }})
    assert response.status_code == 200
    return response.get_json()["build"]


def test_workbench_is_one_top_tab_with_a_five_phase_rail():
    tabs = _between(TEMPLATE, '<div id="tabs">', "</div>")
    assert tabs.count('data-tab="workbench"') == 1
    assert 'data-tab="workbench">Workbench</button>' in tabs
    # the Editor/Analyze split is gone; "Publish" now names the browser of
    # already-published records (#125)
    assert 'data-tab="upload"' not in tabs
    assert 'data-tab="ocr"' not in tabs
    assert 'data-tab="publish">Published Library</button>' in tabs

    rail = _between(TEMPLATE, '<nav id="wb-rail"', "</nav>")
    for phase in ("record", "source", "text", "knowledge", "publish"):
        assert f'data-phase="{phase}"' in rail
    for pane in ("phase-record", "phase-source", "phase-text",
                 "phase-knowledge", "phase-publish"):
        assert f'id="{pane}"' in TEMPLATE

    # the moved panes keep their element ids (DOM-transplant discipline)
    assert 'id="ocr-analysis-workspace"' in TEMPLATE
    assert 'id="ocr-document-workspace"' in TEMPLATE
    assert 'id="an-integrated-host"' in TEMPLATE
    assert 'id="btab-entry"' in TEMPLATE
    assert 'id="btab-source"' in TEMPLATE
    assert 'id="btab-resources"' in TEMPLATE
    assert 'id="b-title"' in TEMPLATE
    assert 'id="b-rights"' in TEMPLATE          # now in the Publish phase
    assert 'id="b-ready"' in TEMPLATE
    assert 'id="build-upload"' in TEMPLATE
    assert 'id="an-bundle"' in TEMPLATE
    assert '<section id="analyze" hidden>' in TEMPLATE
    assert 'id="ocr-workspace-tabs"' not in TEMPLATE

    # the workspace toggle survives as a phase adapter for old callers
    adapter = _between(APP, "function setAnalyzeWorkspace", "function wbActivePhase")
    assert 'which === "document" ? "text" : "knowledge"' in adapter


def test_workbench_phase_gating_locks_analyze_derived_phases_for_drafts():
    for marker in ("wb-text-locked", "wb-knowledge-locked", "wb-publish-locked"):
        assert f'id="{marker}"' in TEMPLATE
    lock = _between(APP, "function wbLockNote", "function applyWorkbenchGates")
    assert "anAnalyzable(b)" in lock
    gates = _between(APP, "function applyWorkbenchGates", "function wbReadiness")
    assert 'el("ocr-document-workspace").hidden = !!textNote' in gates
    assert 'el("ocr-analysis-workspace").hidden = !!knowNote' in gates
    # the Publish action bar stays reachable for drafts: verify lives there
    assert 'el("wb-publish-actions").hidden = !b' in gates
    # [hidden] must beat the id-based .active display rules, or the locked
    # workspace (and its own placeholder) renders under the lock note
    assert ".ocr-workpane[hidden] { display: none !important; }" in STYLE


def test_source_viewer_agrees_with_the_chip_and_workbench_hides_dup_chrome():
    # the Source chip and the Text phase fall back to the entry folder's own
    # PDF via ocrBookPdf — the Source viewer must use the same fallback
    source = _between(APP, "async function refreshSourceTab()",
                      "// create/refresh the entry folder")
    assert "localPath || ocrBookPdf(b.id)" in source
    assert "pagesPdf: viewPath" in source
    # the transplanted an-head must not repeat title · author · year under the
    # Workbench header (the provider/status side of the bar stays)
    assert "#an-integrated-host #an-title" in STYLE
    assert "#an-integrated-host #an-sub { display: none; }" in STYLE
    # the Text toolbar's find/replace give ground before the save button wraps
    assert ("#ocr-find, #ocr-replace "
            "{ flex: 1 1 60px; min-width: 60px; max-width: 130px; }") in STYLE


def test_workbench_readiness_lives_on_the_single_phase_rail():
    ready = _between(APP, "function wbReadiness", "function renderWorkbenchRail")
    assert "ocrBookPdf(b.id)" in ready                       # source
    assert "f.stale === true" in ready                       # text (#135 markers)
    assert "b.ocr_verified" in ready
    assert "summary.exists" in ready and "about.exists" in ready
    assert "outdated" in ready
    assert "b.published_slug" in ready                       # publish
    rail = _between(APP, "function renderWorkbenchRail", "function renderWorkbench()")
    assert '#wb-rail .wb-phase-btn' in rail
    assert 'setAttribute("aria-current", "step")' in rail
    assert "wb-phase-badge" in rail
    assert "wb-chip" not in rail
    assert 'id="wb-chips"' not in TEMPLATE
    assert ".wb-chip" not in STYLE
    # Entry creation remains available in the activity bar and the empty
    # state, without a third copy that overflows the fixed-width sidebar.
    assert 'id="build-new"' in TEMPLATE
    assert 'id="build-new-empty"' in TEMPLATE
    assert 'id="build-new-side"' not in TEMPLATE


def test_workbench_sidebar_controls_are_named_and_keyboard_operable():
    toolbar = _between(TEMPLATE, '<div class="pane-bar" id="builds-tabs">', "</div>")
    assert 'aria-label="Show verified books only"' in toolbar
    assert 'aria-pressed="false"' in toolbar
    assert 'aria-label="Collapse the entries and artifacts sidebar"' in toolbar
    assert 'aria-controls="ocr-side"' in toolbar

    render = _between(APP, "function renderOcrBooks", "async function selectOcrBook")
    assert 'verifiedFilter.setAttribute("aria-pressed"' in render
    assert 'li.setAttribute("role", "button")' in render
    assert 'li.setAttribute("aria-expanded"' in render
    row = _between(APP, "function appendBuildListItem", "// Publish a verified entry")
    assert 'li.tabIndex = 0' in row
    assert 'li.setAttribute("aria-current", "true")' in row
    keyboard = _between(APP, 'el("ocr-books").addEventListener("keydown"',
                        'el("ocr-docs").addEventListener("click"')
    assert 'ev.key !== "Enter" && ev.key !== " "' in keyboard
    assert "row.click()" in keyboard
    assert ".build-item:focus-visible" in STYLE


def test_analyze_facsimile_and_artifact_tree_contracts():
    for element_id in (
        "an-facsimile-pane",
        "an-facsimile",
        "an-facsimile-splitter",
        "an-stage-pages",
        "an-integrated-host",
    ):
        assert f'id="{element_id}"' in TEMPLATE
    assert "#an-facsimile-splitter" in STYLE
    assert '<span class="tool-label">Artifacts</span>' in TEMPLATE
    assert 'id="ocr-docs-empty" class="empty">No artifacts</p>' in TEMPLATE

    artifacts = _between(APP, "function renderOcrDocs()", "async function openTextArtifact")
    for category in (
        'header("PDF documents"',
        'header("OCR data"',
        'header("Full text"',
        'header("Translations"',
        'header("Analysis data"',
        'header("Images"',
    ):
        assert category in artifacts
    assert "Processed PDF - not generated" in artifacts


def test_captured_provenance_flows_into_builds_and_artifact_images():
    manual = _between(APP, "function manualToBook", "function migrateVerify")
    assert "book.extra = e.extra" in manual
    assert "book.images = e.images" in manual
    assert "book.capture_id = e.capture_id" in manual

    sources = _between(APP, "function capturedSourceMeta", "function addedRankByRowId")
    assert "extra:" in sources
    assert "images:" in sources
    assert "capture_id:" in sources
    assert "capturedSourceMeta(row)" in sources

    seed = _between(APP, "function buildSeedFromSource", "async function patchBuildRaw")
    assert "extra: s.extra || {}" in seed
    assert "images: s.images || []" in seed
    assert 'capture_id: s.capture_id || ""' in seed

    artifacts = _between(APP, "function renderOcrDocs", "async function openTextArtifact")
    assert "folder.captured_images || []" in artifacts
    assert '"/api/capture/image?path="' in artifacts
    assert "row.dataset.capturePath" in artifacts
    assert "Captured photo -" in artifacts
    assert "f.artifact || f.name" in artifacts


def test_jobs_drawer_and_default_engine_modal_cover_ocr_and_text_analysis():
    # the queue + Default Engine bar live in a collapsible drawer at the
    # Workbench bottom, visible from every phase, collapsed by default
    assert 'id="wb-jobs"' in TEMPLATE
    assert 'id="wb-jobs-toggle"' in TEMPLATE
    assert 'id="wb-jobs-summary"' in TEMPLATE
    assert 'id="wb-jobs-body" hidden' in TEMPLATE
    # the glyph-only toggle carries an accessible name
    toggle = _between(TEMPLATE, 'id="wb-jobs-toggle"', "</button>")
    assert 'aria-label="Jobs"' in toggle
    assert 'aria-expanded="false"' in toggle
    # the open drawer occludes phase content: opaque, stacked, phases clipped
    drawer_css = _between(STYLE, "#wb-jobs {", "}")
    assert "background: var(--face);" in drawer_css
    assert "z-index: 2;" in drawer_css
    assert "overflow: hidden;" in _between(STYLE, ".wb-phase.active {", "}")
    assert '<span class="tool-label">Jobs</span>' in TEMPLATE
    assert '>Default Engine:</button>' in TEMPLATE
    assert "<th>Type</th>" in TEMPLATE
    assert "<th>Artifact</th>" in TEMPLATE
    assert "<th>Engine</th>" in TEMPLATE
    assert 'id="ocr-queue-empty" class="empty">No jobs</p>' in TEMPLATE

    drawer = _between(APP, "function setJobsDrawer", "function initWorkbench")
    assert "body.hidden = !open" in drawer
    assert "jobsDrawerOpen" in drawer
    summary = _between(APP, "function renderJobsFooter", "function initJobs")
    assert "wb-jobs-summary" in summary
    assert 'tab.dataset.tab !== "workbench"' in summary

    for element_id in (
        "engine-overlay",
        "engine-window",
        "engine-close",
        "engine-settings",
        "engine-done",
        "ocr-service",
        "analysis-service",
    ):
        assert f'id="{element_id}"' in TEMPLATE
    assert '<label class="tool-label" for="ocr-service">OCR</label>' in TEMPLATE
    assert '<label class="tool-label" for="analysis-service">Text Analysis</label>' in TEMPLATE
    for engine in ("mistral", "claude", "textract", "configured"):
        assert f'<option value="{engine}">' in TEMPLATE

    readiness = _between(
        APP, "function refreshDefaultEngineOptions()", "async function openDefaultEngines")
    assert "option.disabled = !ready" in readiness
    assert "analysis.options[0].disabled = !ready" in readiness
    assert "API key required" in readiness


def test_page_analysis_staging_and_unified_job_rows_are_wired():
    staging = _between(
        APP, "async function stageSelectedAnalysisPages()", "async function submitAnalysisStaged")
    assert "ocrState.pageSel" in staging
    assert "ocrState.analysisTags.set" in staging
    assert "analysisServiceReady" in staging

    submit = _between(
        APP, "async function submitAnalysisStaged", "async function ocrQueuePages")
    assert 'anStartJob("/api/analyze/pages"' in submit
    assert "pages: group.pages" in submit
    assert "ocrState.analysisTags.delete" in submit

    jobs = _between(APP, "function renderOcrQueue()", "async function cancelOcrJob")
    assert "ocrState.jobs" in jobs
    assert "anJobs.entries()" in jobs
    assert "<td>OCR</td>" in jobs
    assert "<td>Text analysis</td>" in jobs
    # the drawer count singularizes like the summary line ("1 job", not "1 jobs")
    assert '${total} job${total === 1 ? "" : "s"}' in jobs


def test_manual_about_save_populates_editor_description(client):
    build = _ready_build(client, "About propagation")

    response = client.put(
        f"/api/builds/{build['id']}/about",
        json={"text": "  A polished public description.  \n"},
    )

    assert response.status_code == 200
    assert response.get_json()["ok"] is True
    assert server._read_entry_text(build["id"], "about.md") == (
        "A polished public description.\n")
    builds = server.lib.load_json(server.BUILDS_PATH, {})
    assert builds[build["id"]]["description"] == "A polished public description."


def test_selected_page_analysis_creates_manifested_retrievable_artifact(
        client, monkeypatch):
    build = _ready_build(client, "Selected page analysis")
    ocr_dir = server._entry_dir(build["id"]) / "ocr"
    ocr_dir.mkdir(parents=True, exist_ok=True)
    (ocr_dir / "compiled.txt").write_text(
        "--- page 1 ---\nAlpha medicinal plant.\n\n"
        "--- page 2 ---\nMiddle page should not be analyzed.\n\n"
        "--- page 3 ---\nOmega botanical index.\n",
        encoding="utf-8",
    )

    prompts: list[str] = []

    def fake_ai_chat(_cfg, messages, **_kwargs):
        prompts.append("\n".join(str(message.get("content") or "") for message in messages))
        return "The selected pages connect Alpha and Omega."

    def run_inline(bid, kind, total, target, decorate=None):
        job = server._an_job_new(bid, kind, total)
        if decorate is not None:
            decorate(job)
        target(job)
        return job

    monkeypatch.setattr(server, "_ai_cfg", lambda: {
        "base": "https://example.test/v1",
        "key": "test-key",
        "model": "test-model",
    })
    monkeypatch.setattr(server, "_ai_chat", fake_ai_chat)
    monkeypatch.setattr(server, "_an_job_start", run_inline)

    response = client.post("/api/analyze/pages", json={
        "build_id": build["id"],
        "pages": [3, 1, 3],
        "doc": "compiled.txt",
        "engine": "configured",
    })

    assert response.status_code == 200
    data = response.get_json()
    assert data["ok"] is True
    assert data["pages"] == [1, 3]
    assert data["doc"] == "compiled.txt"
    assert data["engine"] == "configured"
    assert data["artifact"].startswith("page-analysis-1_3-")
    assert data["artifact"].endswith(".md")

    job = client.get(f"/api/analyze/job/{data['job']}").get_json()
    assert job["kind"] == "page-analysis"
    assert job["status"] == "done"
    assert job["pages"] == [1, 3]
    assert job["artifact"] == data["artifact"]
    assert job["engine"] == "configured"
    assert prompts
    assert "Alpha medicinal plant" in prompts[0]
    assert "Omega botanical index" in prompts[0]
    assert "Middle page should not be analyzed" not in prompts[0]

    manifest = client.get(f"/api/builds/{build['id']}/folder").get_json()
    assert data["artifact"] in [item["name"] for item in manifest["analysis"]]
    assert "full_text" in manifest
    assert "translations" in manifest
    assert "images" in manifest

    artifact = client.get(
        f"/api/builds/{build['id']}/artifact/analysis/{data['artifact']}")
    assert artifact.status_code == 200
    artifact_data = artifact.get_json()
    assert artifact_data["kind"] == "analysis"
    assert artifact_data["name"] == data["artifact"]
    assert "# Page analysis: Selected page analysis" in artifact_data["text"]
    assert "## Pages 1-3" in artifact_data["text"]
    assert "The selected pages connect Alpha and Omega." in artifact_data["text"]

    server._write_entry_text(build["id"], "about.md", "must stay private\n")
    traversal = client.get(
        f"/api/builds/{build['id']}/artifact/analysis/../about.md")
    assert traversal.status_code == 404
    assert client.get(
        f"/api/builds/{build['id']}/artifact/unsupported/{data['artifact']}"
    ).status_code == 404
