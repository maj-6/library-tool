from __future__ import annotations

import re
from pathlib import Path


APP = (Path(__file__).parents[1] / "tools" / "whl_explorer" / "static" /
       "app.js").read_text(encoding="utf-8")
TEMPLATE = (Path(__file__).parents[1] / "tools" / "whl_explorer" / "templates" /
            "index.html").read_text(encoding="utf-8")
SERVER = (Path(__file__).parents[1] / "tools" / "whl_explorer" /
          "server.py").read_text(encoding="utf-8")


def test_catalog_source_markers_cover_capture_manual_master_and_edits():
    source_fn = APP.split("function rowSourceMark(row)", 1)[1].split(
        "// tiny image marker", 1)[0]
    assert "ICONS.camera" in source_fn
    assert "ICONS.manual" in source_fn
    assert "ICONS.listfile" in source_fn
    assert "src-edited" in source_fn


def test_analyze_list_filters_out_unavailable_builds():
    render_fn = APP.split("function renderAnList()", 1)[1].split(
        "function anSelect", 1)[0]
    assert ".filter(anAnalyzable)" in render_fn
    assert "an-locked" not in render_fn


def _function(name: str, next_name: str) -> str:
    return APP.split(f"function {name}", 1)[1].split(f"function {next_name}", 1)[0]


def test_run_scans_batch_is_search_only():
    body = _function("runScansBatch", "scanStatusLine")
    assert "queueScan(row.id, false)" in body


def test_verified_toggle_saves_the_whole_editor_form():
    # the toggle routes through setVerified, which is the save action —
    # verification must never be a bare status patch
    handler = APP.split('el("b-ready").addEventListener', 1)[1].split(
        'el("build-new")', 1)[0]
    assert "setVerified(" in handler
    assert "patchBuildRaw(b.id, { status:" not in handler
    body = APP.split("async function setVerified", 1)[1].split(
        "function renderLockNote", 1)[0]
    assert "await saveBuildFields(null, {" in body
    assert "forceMetadata: true" in body
    assert "patchBuildVerificationCompatibility(" in body
    assert "patchBuildRaw(" not in body


def test_locked_phases_offer_the_verify_unlock_inline():
    # the draft gate renders its own "Mark verified" so Text/Knowledge don't
    # require the Publish detour
    body = APP.split("function renderLockNote", 1)[1].split(
        "function applyWorkbenchGates", 1)[0]
    assert "wb-verify-here" in body
    gates = APP.split("function applyWorkbenchGates", 1)[1].split(
        "// Per-phase readiness", 1)[0]
    assert 'renderLockNote("wb-text-locked"' in gates
    assert 'renderLockNote("wb-knowledge-locked"' in gates


def test_publish_flushes_unsaved_edits_first():
    # Picking Rights then publishing must not read stale state or switch books
    # while the save is in flight. Behavioral races live in the Node suite.
    body = APP.split("async function uploadBuild", 1)[1].split(
        "let _publishTimer", 1)[0]
    assert "const buildId = state.buildSel" in body
    assert "saved = await saveBuildFields()" in body
    assert "state.buildSel !== buildId" in body
    assert "JSON.stringify({ build_id: buildId })" in body


def test_workbench_selection_is_unified_and_refreshes_resources():
    # ONE selection entry point sets all three legacy aliases together
    body = _function("selectWorkbenchBook", "setJobsDrawer")
    assert "state.buildSel = bid" in body
    assert "selectOcrBook(bid)" in body
    assert 'activeBuildTab() === "btab-resources"' in body
    assert "refreshResourcesTab()" in body

    alias = _function("selectBuild", "createBuild")
    assert "selectWorkbenchBook(id)" in alias

    sync = _function("selectOcrBook", "ocrVisibleDocs")
    assert "ocrState.book = bid" in sync
    assert "state.anSel = anAnalyzable" in sync
    assert "state.buildSel = bid" in sync


def test_editor_groups_volumes_by_metadata_not_title():
    body = _function("editorBuildItems", "appendBuildListItem")
    assert 'String(b.group_id || "").trim()' in body
    assert "setKeyOf(b)" not in body
    row = _function("appendBuildListItem", "uploadBuild")
    assert 'b.id === state.buildSel' in row

    catalog = APP.split("function groupSets", 1)[1].split(
        '// --- "needs attention" marks', 1)[0]
    assert "groupIdOf(r.book)" in catalog
    assert "if (!key) continue;" in catalog
    assert "setKeyOf(r.book)" not in catalog


def test_legacy_group_migration_persists_association_on_each_book():
    body = _function("backfillSets", "migrateParsedEntries")
    assert 'JSON.stringify({ group_id: key, _preserve: true })' in body
    assert 'entry.book, { group_id: key }' in body


def test_publish_tab_has_tree_grouping_and_catalog_preview():
    # renamed per #125: the tab browses what is already published
    assert 'data-tab="publish">Published Library</button>' in TEMPLATE
    for mode in ("sets", "author", "category", "date"):
        assert f'<option value="{mode}">' in TEMPLATE
    assert 'id="publish-tree" aria-label="Published catalogue"' in TEMPLATE
    assert 'role="tree"' not in TEMPLATE
    assert 'id="publish-side-toggle"' in TEMPLATE
    assert 'id="publish-record"' in TEMPLATE


def test_publish_tree_uses_published_metadata_not_duplicate_titles():
    entities = _function("publishEntities", "publishBookLabel")
    assert 'String(v.group_id || "").trim()' in entities
    assert '"set:" + gid' in entities
    assert "setKeyOf" not in entities
    assert '"book:" + publishSlug(v)' in entities

    grouping = APP.split("function publishEntryPaths", 1)[1].split(
        "function publishNodeOpen", 1)[0]
    assert "category_paths" in grouping
    assert "JSON.stringify(names)" in grouping
    assert 'String(v.authors || "").trim()' in grouping
    assert 'year < 1000 || year > 2999' in grouping


def test_published_volume_context_menu_uses_slug_identity_and_guarded_navigation():
    resolver = _function("publishWorkbenchBuildId", "openPublishedWorkbenchEntry")
    assert "build.published_slug" in resolver
    assert "entry.local_build_id" in resolver
    assert "build.title" not in resolver
    assert "matches.length === 1" in resolver

    opener = _function("openPublishedWorkbenchEntry", "publishContextMenuItems")
    assert "await loadBuilds()" in opener
    assert 'openRoutedItem({ kind: "build", ref: bid })' in opener

    menu = _function("publishContextMenuItems", "onPublishContextMenu")
    assert 'entity.kind !== "book"' in menu
    assert 'label: "Open in Workbench"' in menu

    handler = _function("onPublishContextMenu", "renderPublishTree")
    assert 'closest(".publish-tree-row")' in handler
    assert "ev.preventDefault()" in handler
    assert "openProcMenu(ev.clientX, ev.clientY, items)" in handler
    assert 'el("publish-tree").addEventListener("contextmenu", onPublishContextMenu)' in APP


def test_publish_preview_matches_online_record_shape_and_supports_sets():
    preview = APP.split("function publishMetadata", 1)[1].split(
        "async function renderPublishPreview", 1)[0]
    assert 'class="ppub-record-page"' in preview
    assert 'class="ppub-book-main"' in preview
    assert 'class="ppub-book-side"' in preview
    assert 'class="ppub-meta"' in preview
    assert "publishCatalogRow" in preview
    assert 'data-publish-book=' in preview


def test_workbench_book_list_is_one_unfiltered_tree():
    body = _function("renderOcrBooks", "selectOcrBook")
    # Draft, verified, and published entries always share the same volume tree.
    assert "allBuildsSorted()" in body
    assert "editorBuildItems" in body
    assert "verifiedOnly" not in body
    assert "buildsTab" not in body
    assert 'data-bstab=' not in TEMPLATE
    assert 'id="ocr-filter-verified"' not in TEMPLATE


def test_volume_titles_use_one_non_mutating_tag_formatter_across_surfaces():
    formatter = _function("bookTitleHtml", "applyTheme")
    assert 'class="volume-title-tag">Vol.' in formatter
    assert "esc(volume)" in formatter
    assert "esc(title)" in formatter
    assert "book.title =" not in formatter

    for function_name, next_name in (
        ("renderHome", "initHome"),
        ("renderAnList", "anSelect"),
        ("appendBuildListItem", "uploadBuild"),
        ("renderWorkbench", "selectWorkbenchBook"),
        ("renderReplicaBooks", "rwConfirmDiscard"),
    ):
        assert "bookTitleHtml" in _function(function_name, next_name)

    root = Path(__file__).parents[1] / "website" / "assets"
    for filename in ("records.js", "book.js", "read.js", "browse.js"):
        assert "bookTitleHtml" in (root / filename).read_text(encoding="utf-8")


def test_description_generation_defaults_explicitly_to_deepseek():
    body = _function("generateAiSummary", "loadDescriptionFile")
    assert '"https://api.deepseek.com"' in body
    assert '"deepseek-chat"' in body
    assert "Add your DeepSeek API key (Settings > Credentials)" in body
    assert "Configure the model + API key" not in body
    assert "descriptionProviderLabel(s)" in body
    assert "Generating with ${provider}" in body
    assert "DeepSeek by default, or your configured AI provider" in TEMPLATE


def test_catalog_defaults_to_open_search_workspace_without_overwriting_saved_view():
    defaults = APP.split("settings: {", 1)[1].split("sort:", 1)[0]
    assert "showCatalog: true" in defaults
    assert "checkedCols" not in defaults
    assert 'whlMode: "search", checkedMode: "search"' in defaults

    load = _function("loadSettings", "normalizeSettings")
    assert "Object.assign(state.settings, s, v)" in load
    for key in ("showCatalog", "whlMode", "checkedMode"):
        assert f'"{key}"' in APP.split("const VIEW_STATE_KEYS", 1)[1].split("]);", 1)[0]


def test_facsimile_paper_color_is_an_opt_in_durable_ocr_setting():
    defaults = APP.split("settings: {", 1)[1].split("sort:", 1)[0]
    assert "ocrPaperColor: false" in defaults
    assert "ocrPaperLighten: 20" in defaults
    assert 'id="set-ocr-paper-color"' in TEMPLATE
    assert 'id="set-ocr-paper-lighten"' in TEMPLATE
    assert "/api/pdf/paper-color" in APP
    assert "refreshOcrPaperColors()" in APP


def test_table_chrome_reflows_after_scale_and_catalog_side_panel_changes():
    scale = _function("applyUiScale", "setUiScale")
    assert "scheduleTableChromeRefresh()" in scale

    refresh = _function("refreshVisibleTableChrome", "scheduleTableChromeRefresh")
    assert 'applyTableChrome(state.settings.topTable === "whl" ? "whl" : "checked")' in refresh
    assert 'applyTableChrome("b-" + activeBottomTable())' in refresh

    splitter = APP.split("// resizable left panel", 1)[1].split(
        "// resizable approved-sources pane", 1)[0]
    assert splitter.count("scheduleTableChromeRefresh()") >= 2


def test_tooltip_geometry_converts_visual_pixels_back_through_root_zoom():
    body = _function("showTip", "hideTip")
    assert 'tip.style.left = "0px"' in body
    assert 'tip.style.top = "0px"' in body
    assert "Number(state.settings.uiScale) || 1" in body
    assert 'tip.style.left = (left / scale) + "px"' in body
    assert 'tip.style.top = (top / scale) + "px"' in body


def test_display_settings_have_one_theme_font_owner_and_no_fake_engine_choice():
    display = TEMPLATE.split('id="sec-appearance"', 1)[1].split(
        'id="sec-theme"', 1)[0]
    assert 'id="ui-scale-select"' in display
    for stale_id in ("theme-select", "font-ui-select", "font-select",
                     "font-mono2-select"):
        assert f'id="{stale_id}"' not in TEMPLATE

    theme_editor = APP.split('const THEME_TOKENS = [', 1)[1].split(
        'const THEME_TOKEN_VARS', 1)[0]
    for css_var in ("--ui", "--mono", "--mono2"):
        assert css_var in theme_editor
    assert "Default (Appearance)" not in APP
    assert "Theme default" in APP

    assert 'id="analysis-service"' not in TEMPLATE
    assert 'id="analysis-engine-note"' not in TEMPLATE
    for unavailable in ("Azure Document Intelligence", "OpenAI vision"):
        assert unavailable not in TEMPLATE


def test_client_and_server_agree_on_local_only_secret_keys():
    client = APP.split("const SECRET_IDS = Object.freeze({", 1)[1].split(
        "});", 1)[0]
    server = SERVER.split("_SECRET_IDS = {", 1)[1].split(
        "}\n_SECRET_KEYS", 1)[0]
    key_pattern = r'^\s*"?([A-Za-z0-9_]+)"?\s*:'
    client_keys = set(re.findall(key_pattern, client, re.MULTILINE))
    server_keys = set(re.findall(key_pattern, server, re.MULTILINE))
    assert client_keys == server_keys
    assert {"embedKey", "imgGenKey", "ocrAzureKey"} <= client_keys


def test_page_deletion_surfaces_reference_remap_warnings():
    save_flow = _function(
        "saveOcrDocumentsBeforePageDelete", "deleteSelectedPages")
    assert "!res.ok || !data || !data.ok" in save_flow
    assert "ocrSyncEditor()" in save_flow

    delete_flow = _function("deleteSelectedPages", "titlePageSet")
    assert "data.warnings" in delete_flow
    assert "REVIEW WARNING" in delete_flow
    # the .bak.pdf the message used to name is retired; recovery is the trash
    assert "data.backup" not in delete_flow
    assert "Info > Trash" in delete_flow
    assert "page_revision" in delete_flow
    assert '"unversioned"' in delete_flow
    assert "COMMITTED — REFRESH FAILED" in delete_flow
    assert "Review the affected references/artifacts" in delete_flow
    assert "Review those links" not in delete_flow

    folder_sync = SERVER.split("def api_build_folder_sync", 1)[1].split(
        "@app.route", 1)[0]
    assert 'deletion.get("warnings")' in folder_sync
    assert "page deletion warning" in folder_sync


def test_shared_popup_geometry_uses_visual_pixels_at_non_default_scale():
    helper = _function("positionFixedPopup", "showTip")
    assert "innerWidth - rect.width - edge" in helper
    assert "innerHeight - rect.height - edge" in helper
    assert 'node.style.left = (left / scale) + "px"' in helper
    assert 'node.style.top = (top / scale) + "px"' in helper

    cursor = _function("openProcMenu", "openExternal")
    assert "positionFixedPopup(pop, x, y)" in cursor
    anchored = _function("openPopup", "openColumnMenu")
    assert "fixedPopupMetrics(pop)" in anchored
    assert "positionFixedPopup(pop" in anchored


def test_copyright_tag_uses_independent_semantic_halves_and_accessible_text():
    colors = _function("copyrightColors", "renderCrTag")
    assert 'left: "reg-found"' in colors
    assert 'rc = "public-domain"' in colors
    assert 'rc = "in-copyright"' in colors
    assert 'rc = "inconclusive"' in colors
    assert 'left: copyrightSources().length ? "reg-none" : "unknown"' in colors

    render = _function("renderCrTag", "crStatusFor")
    assert 'role="img"' in render
    assert 'aria-label=' in render
    assert 'aria-hidden="true"' in render
    assert "c.left === c.right" not in render
    assert "cr-mono" not in render

    assert "registration evidence (upper-left) / copyright status (lower-right)" in TEMPLATE


def test_knowledge_gains_test_and_ask_tabs_with_their_contracts():
    # the two new Knowledge views (#142/#143) sit between Passages and
    # Relevance in the an-tabs strip
    tabs = TEMPLATE.split('id="an-tabs"', 1)[1].split("</div>", 1)[0]
    order = [t.split('"')[0] for t in tabs.split('data-antab="')[1:]]
    assert order == ["an-overview", "an-cats", "an-trans", "an-notes",
                     "an-passages", "an-test", "an-ask", "an-rel"]
    # every evaluation-set kind is offered (D9's coverage list)
    for kind in ("exact-phrase", "archaic-modern", "factual", "thematic",
                 "tables", "cross-page", "multilingual", "unanswerable"):
        assert f'value="{kind}"' in TEMPLATE
    # the permanent provenance note under a drafted answer
    assert "not medical advice" in TEMPLATE
    assert 'id="an-ask-note"' in TEMPLATE


def test_ask_answer_escapes_before_linkifying_citations():
    body = _function("renderAskAnswer", "onAskAnswerClick")
    # escape FIRST, then linkify [pN] — model text never lands as HTML
    assert body.index("esc(text)") < body.index("replace(/\\[p(\\d+)\\]/g")
    assert 'class="ask-cite"' in body
    # abstention renders as a note, not an error
    assert 'classList.toggle("ask-abstain", abstained)' in body


def test_test_and_ask_share_one_evidence_row_idiom():
    row = _function("evRowHtml", "renderAnEvalResults")
    assert "data-ev-rel=" in row          # judgment toggles (Test)
    assert "data-ev-row=" in row          # row identity (citation jumps)
    snip = _function("evSnippetHtml", "evPageLabel")
    assert snip.index("esc(s)") < snip.index("ev-mark")   # escape, then mark
    ask = _function("renderAskEvidence", "onAskEvidenceClick")
    assert "evRowHtml(r, null)" in ask    # same rows, no toggles


def test_registration_client_cache_is_versioned_year_aware_and_retries_errors():
    assert 'const REG_KEY = "whl_reg_cache_v2"' in APP
    key = _function("regKey", "queueReg")
    assert "book && book.year" in key
    pump = _function("pumpRegQueue", "crStatusCache")
    assert "if (!r.ok) throw new Error" in pump
    assert "_cachedAt: Date.now()" in pump


def test_remarks_sidebar_is_one_shared_accessible_shell_region():
    assert TEMPLATE.count('id="remarks-sidebar"') == 1
    main_end = TEMPLATE.index("</main>")
    remarks_start = TEMPLATE.index('<aside id="remarks-sidebar"')
    shell_end = TEMPLATE.index('</div><!-- /#shell -->')
    assert main_end < remarks_start < shell_end

    remarks = TEMPLATE[remarks_start:TEMPLATE.index("</aside>", remarks_start)]
    for id_ in (
        "remarks-open", "remarks-expanded", "remarks-heading", "remarks-total",
        "remarks-close", "remarks-filter", "remarks-list",
    ):
        assert remarks.count(f'id="{id_}"') == 1
    assert 'aria-label="Remarks"' in remarks
    assert 'aria-controls="remarks-expanded" aria-expanded="false"' in remarks
    assert 'aria-label="Open Remarks sidebar"' in remarks
    assert 'id="remarks-expanded" hidden' in remarks
    assert 'role="status"' in remarks
    assert 'aria-live="polite"' in remarks
    assert '<label class="tool-label" for="remarks-filter">Show</label>' in remarks
    assert 'aria-label="Collapse Remarks sidebar"' in remarks
    assert 'class="remarks-rail-icon" data-icon="remarks"' in remarks
    assert 'id="remarks-heading" class="remarks-heading-icon" role="heading"' in remarks
    assert 'remarks-rail-label' not in remarks
    assert '<h2 id="remarks-heading">Remarks</h2>' not in remarks


def test_remarks_sidebar_replaces_the_home_awaiting_review_section():
    assert 'class="home-card home-reviews-card"' not in TEMPLATE
    assert 'id="home-review-resolved"' not in TEMPLATE
    assert 'id="home-reviews"' not in TEMPLATE


def test_remarks_filter_options_match_target_classes():
    remarks = TEMPLATE.split('id="remarks-filter"', 1)[1].split("</select>", 1)[0]
    for value, label in (
        ("all", "All"),
        ("catalogs", "Catalogs"),
        ("sources", "Sources"),
        ("entries", "Entries"),
        ("pages", "Pages"),
        ("publications", "Publications"),
    ):
        assert f'<option value="{value}">{label}</option>' in remarks


def test_remarks_view_state_and_tab_switch_wiring_are_explicit():
    view_keys = APP.split("const VIEW_STATE_KEYS", 1)[1].split("]);", 1)[0]
    assert '"remarksSidebarCollapsed"' in view_keys
    assert '"remarksFilters"' in view_keys

    defaults = APP.split("settings: {", 1)[1].split("sort:", 1)[0]
    assert "remarksSidebarCollapsed: true" in defaults
    assert "remarksFilters: {}" in defaults
    assert "remarksMeta: {}" in defaults

    tab_defaults = APP.split("const REMARK_TAB_DEFAULTS", 1)[1].split("};", 1)[0]
    for tab, category in (
        ("home", "all"),
        ("checked", "catalogs"),
        ("workbench", "entries"),
        ("replica", "pages"),
        ("publish", "publications"),
        ("infotab", "all"),
    ):
        assert f'{tab}: "{category}"' in tab_defaults

    switch = _function("initTabs", "showTip")
    assert "syncRemarksForTab()" in switch
    init = _function("initRemarksSidebar", "attnTargetAtHover")
    assert "setRemarksFilterForTab(activeRemarksTab()" in init
    assert "setRemarksCollapsed(state.settings.remarksSidebarCollapsed" in init


def test_remarks_actions_are_accessible_and_failure_safe():
    item = _function("remarkItemHtml", "remarksGroupHtml")
    assert '<li class="remarks-item"' in item
    assert '<details class="remarks-disclosure"' in item
    assert '<summary class="remarks-item-summary">' in item
    assert 'class="remarks-comment-count"' in item
    assert '>(${commentCount})</span>' in item
    assert 'type="button" data-remark-open=' in item
    assert 'aria-label="Open ' in item
    assert 'disabled aria-disabled=\\"true\\"' in item
    assert 'class="cad-btn tiny icon-btn remarks-edit" type="button"' in item
    assert 'data-remark-edit=' in item
    assert 'data-remark-reply=' in item
    assert 'aria-label="Reply to ' in item
    assert 'data-remark-resolve=' in item
    assert 'aria-label="Resolve remark for ' in item
    assert '${ICONS.reply}' in item
    assert '${ICONS.check}' in item

    apply = APP.split("async function applyRemarkValue", 1)[1].split(
        "async function saveRemarkEditor", 1)[0]
    assert "ok = await updateBuildPortableMetadata" in apply
    assert "ok = await setRowAttention" in apply
    assert "if (ok)" in apply
    assert "The mark was kept" in apply

    init = _function("initRemarksSidebar", "attnTargetAtHover")
    escape = init.split('if (ev.key === "Escape")', 1)[1].split(
        '} else if (ev.key === "Enter"', 1)[0]
    assert "ev.preventDefault()" in escape
    assert "ev.stopPropagation()" in init

    collapse = _function("setRemarksCollapsed", "syncRemarksForTab")
    assert "aside.contains(document.activeElement)" in collapse
    assert 'collapsed ? el("remarks-open") : el("remarks-filter")' in collapse


def test_remarks_reply_and_resolution_use_the_shared_review_api():
    reply = _function("submitRemarkReply", "resolveRemarkItem")
    assert "await ensureRemarkReview(item)" in reply
    assert '/comment`' in reply
    assert 'body: JSON.stringify({ text })' in reply
    assert 'remarksState.replyDraft = ""' in reply

    resolve = _function("resolveRemarkItem", "activateTopTab")
    assert '/resolve`' in resolve
    assert 'body: JSON.stringify({ resolved: true })' in resolve
    assert 'await applyRemarkValue(item, "")' in resolve
    assert "item.hasAttention === false" in resolve
    assert "ATTENTION MARK NOT CLEARED" in resolve

    ensure = _function("ensureRemarkReview", "submitRemarkReply")
    assert "page_revision" in ensure
    assert "replicaPageBuildRevision(item.ref)" in ensure


def test_remarks_keep_open_reviews_reachable_without_inflating_attention_counts():
    items = _function("remarksItems", "remarkRoute")
    assert "Object.values(reviewsState.items || {})" in items
    assert 'review.status !== "open"' in items
    assert "reviewOnlyRemarkDescriptor(review" in items
    assert "ids.has(id)" in items

    review_only = _function("reviewOnlyRemarkDescriptor", "remarksItems")
    assert "item.hasAttention = false" in review_only
    assert "item.reviewOnly = true" in review_only

    progress = _function("progressSummary", "homeAttentionDestination")
    assert ".filter((item) => item.hasAttention !== false)" in progress
    assert 'item.category === "pages"' in progress
    assert 'item.category === "publications"' in progress


def test_remarks_navigation_guards_dirty_workbench_edits_before_repainting():
    tabs = _function("initTabs", "showTip")
    assert "preserveWorkbenchEditOnActivate" in tabs

    open_item = _function("openRoutedItem", "openRemarkItem")
    build = open_item.split("if (route.buildId)", 1)[1].split(
        "if (route.sourceKey)", 1)[0]
    assert build.index("selectWorkbenchBook") < build.index("activateTopTab")
    assert build.index("setSetExpanded") < build.index("activateTopTab")
    assert "renderOcrBooks()" in build
    assert "preserveWorkbenchEdit: preserveEdit" in build

    source = open_item.split("if (route.sourceKey)", 1)[1].split(
        "activateTopTab(route.tab);", 1)[0]
    assert "buildIsDirty()" in source
    assert "await confirmDialog" in source

    replica = open_item.split("if (route.replicaBookId)", 1)[1].split(
        "if (route.publicationSelection)", 1)[0]
    assert replica.index("replicaPageSourceAvailable") < replica.index(
        "await selectReplicaBook")
    assert replica.index("await selectReplicaBook") < replica.index("activateTopTab")
    assert "await selectReplicaSource(source)" in replica
    assert "if (rwState.page !== page)" in replica
    assert "await selectReplicaPage(page)" in replica

    publication = open_item.split("if (route.publicationSelection)", 1)[1]
    assert "await loadPublishCatalog()" in publication
    assert "publishFindEntity(route.publicationSelection)" in publication
    assert "renderPublishTree(true)" in publication


def test_pages_and_publications_are_real_attention_targets():
    target = _function("attnTargetAtHover", "onAttentionKey")
    for selector in (
        '#rw-pages .rw-pagebtn:hover',
        '#ocr-pages .ocr-pgrow:hover',
        '#an-facsimile .an-fac-page:hover',
        '#publish-tree .publish-tree-row:hover',
    ):
        assert selector in target
    assert "replicaPageRemarkKey" in target
    assert "replicaPageSourceAvailable(b, source)" in target
    assert "target.pageRevision" in target
    assert "publicationRemarkKey" in target
    assert "grouping folders are not domain items" in target

    refresh = _function("refreshKeyedRemarkTarget", "refreshRemarkTarget")
    assert "renderReplicaAttentionMarks()" in refresh
    assert "decorateOcrPages()" in refresh
    assert "decorateAnFacsimile()" in refresh
    assert "renderPublishTree()" in refresh


def test_keyed_remark_sync_retries_and_survives_a_failed_sidecar_write():
    flush = _function("flushClientState", "richerEntry")
    assert "if (!res.ok) throw" in flush
    assert "_csPending[k] = true" in flush
    assert "setTimeout(flushClientState, _csRetryMs)" in flush
    assert "localStorage.removeItem(ATTN_DIRTY_KEY)" in flush

    keyed = _function("setAttnKey", "activeRemarksTab")
    assert 'localStorage.setItem(ATTN_DIRTY_KEY, "1")' in keyed
    assert "state.attn[k] = String" in keyed
    assert "state.settings.remarksMeta[k]" in keyed


def test_review_labels_navigate_to_the_original_item_and_open_details():
    item = _function("reviewItemHtml", "renderReviewsInto")
    assert 'class="ri-label" type="button" data-rv-open' in item
    assert 'aria-label="Open ' in item
    assert "remarkRoute(r)" in item

    click = _function("onReviewClick", "initReviewWin")
    assert 'closest("[data-rv-open]")' in click
    assert "closeReviewWin()" in click
    assert "await openRoutedItem(review)" in click

    routed = _function("openRoutedItem", "openRemarkItem")
    assert 'switchTopTable("checked")' in routed
    assert 'switchPaneTab("pane-info")' in routed
    assert "flashStreamedTarget" in routed
    assert 'scrollIntoView({ block: "center"' in _function(
        "flashRemarkTarget", "flashStreamedTarget")
