from __future__ import annotations

import httpx

from whl_image_processor.settings import Settings
from whl_image_processor.store import SupabaseJobStore


def test_reconciliation_uses_bounded_server_side_rpc():
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=17)

    settings = Settings(
        supabase_url="https://project.supabase.co",
        supabase_secret_key="sb_secret_processor_test",
    )
    client = httpx.Client(transport=httpx.MockTransport(handler))
    with SupabaseJobStore(settings, client) as store:
        reconciled = store.reconcile_terminal_captures(limit=5000)

    assert reconciled == 17
    assert len(seen) == 1
    assert seen[0].method == "POST"
    assert seen[0].url.path == "/rest/v1/rpc/reconcile_photo_processing_captures"
    assert seen[0].read() == b'{"p_limit":1000}'
