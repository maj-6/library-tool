from __future__ import annotations


def test_corrections_workbench_is_an_independent_document(client):
    response = client.get("/corrections")

    assert response.status_code == 200
    document = response.get_data(as_text=True)
    assert "data-corrections-root" in document
    assert "corrections/editor-registry.js" in document
    assert "corrections/layout-controller.js" in document
    assert "corrections/shell.js" in document
    assert document.count("?v=") >= 5
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
