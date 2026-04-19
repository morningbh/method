import pytest


async def test_health(app_client):
    resp = await app_client.get("/api/health")
    assert resp.status_code == 200
    data = resp.json()
    assert data == {"ok": True, "version": "0.0.1"}
