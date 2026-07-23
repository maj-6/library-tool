from __future__ import annotations


def test_corrections_workbench_is_an_independent_document(client):
    response = client.get("/corrections")

    assert response.status_code == 200
    document = response.get_data(as_text=True)
    assert "data-corrections-root" in document
    assert "corrections/editor-registry.js" in document
    assert "corrections/layout-controller.js" in document
    assert "corrections/artifact-model.js" in document
    assert "corrections/artifact-editors.js" in document
    assert "corrections/properties.js" in document
    assert "corrections/artifacts.js" in document
    assert "corrections/books.js" in document
    assert "corrections/reviews.js" in document
    assert "corrections/commands.js" in document
    assert "corrections/keymap.js" in document
    assert "corrections/artifact-overlay.js" in document
    assert "corrections/classification-controls.js" in document
    assert "corrections/image-editor-state.js" in document
    assert "corrections/image-adjust-tool.js" in document
    assert "corrections/image-editor.js" in document
    assert "corrections/shell.js" in document
    assert "corrections/books.css" in document
    assert "corrections/artifacts.css" in document
    assert "corrections/image-editor.css" in document
    assert "corrections/classification.css" in document
    assert "corrections/image-adjust-tool.css" in document
    assert document.count("?v=") >= 23
    assert document.index("corrections/books.js") < document.index(
        "corrections/reviews.js"
    )
    assert document.index("corrections/artifact-model.js") < document.index(
        "corrections/artifacts.js"
    )
    assert document.index("corrections/image-editor-state.js") < document.index(
        "corrections/image-adjust-tool.js"
    )
    assert document.index("corrections/image-adjust-tool.js") < document.index(
        "corrections/image-editor.js"
    )
    assert document.index("corrections/commands.js") < document.index(
        "corrections/keymap.js"
    )
    assert document.index("corrections/keymap.js") < document.index(
        "corrections/classification-controls.js"
    )
    assert document.index("corrections/artifact-overlay.js") < document.index(
        "corrections/shell.js"
    )
    assert document.index("corrections/image-editor.js") < document.index(
        "corrections/shell.js"
    )
    policy = response.headers["Content-Security-Policy"]
    assert "script-src 'self'" in policy
    assert "style-src 'self' 'unsafe-inline'" in policy
    assert "connect-src 'self'" in policy
    assert "frame-ancestors 'none'" in policy


def test_manager_loads_reusable_workbench_launcher_before_application(client):
    response = client.get("/")

    assert response.status_code == 200
    document = response.get_data(as_text=True)
    launcher = document.index("workbench-launch.js")
    application = document.index("app.js")
    assert launcher < application
    assert 'data-cmd="corrections"' in document
    assert 'data-cmd="corrections-new"' in document
