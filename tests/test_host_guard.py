"""The loopback Flask app must reject DNS-rebinding Host headers globally."""

import pytest


@pytest.mark.parametrize("host", ["localhost", "localhost.", "127.0.0.1"])
def test_loopback_hosts_are_allowed(client, host):
    response = client.get("/api/client_state", headers={"Host": host})
    assert response.status_code == 200


@pytest.mark.parametrize("host", ["attacker.example", "192.168.1.20", ""])
def test_untrusted_hosts_are_rejected(client, host):
    response = client.get("/api/client_state", headers={"Host": host})
    assert response.status_code == 403
