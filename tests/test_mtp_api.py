from __future__ import annotations

import sys
from pathlib import Path
from urllib.parse import parse_qs

import httpx
import pytest

sys.path.append(str(Path(__file__).resolve().parents[1]))

from mtp_api import MtpAPI


@pytest.mark.asyncio
async def test_add_order_returns_order_id(monkeypatch: pytest.MonkeyPatch) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        body = {
            key: values[0] for key, values in parse_qs(request.content.decode()).items()
        }
        assert body["action"] == "add"
        assert body["service"] == "11"
        assert body["link"] == "https://example.com/post"
        assert body["quantity"] == "50"
        return httpx.Response(200, json={"order": 98765})

    transport = httpx.MockTransport(handler)
    monkeypatch.setenv("MORETHAN_API_KEY", "secret")
    client = MtpAPI(base_url="https://mtp.test/api/v2", transport=transport)
    try:
        order_id = await client.add_order(
            service_id=11, link="https://example.com/post", quantity=50
        )
        assert order_id == 98765
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_get_status_and_balance(monkeypatch: pytest.MonkeyPatch) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        body = {
            key: values[0] for key, values in parse_qs(request.content.decode()).items()
        }
        if body["action"] == "status":
            return httpx.Response(200, json={"status": "Completed", "remains": "0"})
        if body["action"] == "balance":
            return httpx.Response(200, json={"balance": "10.50", "currency": "USD"})
        return httpx.Response(400, json={"error": "bad action"})

    transport = httpx.MockTransport(handler)
    monkeypatch.setenv("MORETHAN_API_KEY", "secret")
    client = MtpAPI(base_url="https://mtp.test/api/v2", transport=transport)
    try:
        status = await client.get_status(order_id=555)
        balance = await client.get_balance()
    finally:
        await client.aclose()

    assert status["status"] == "Completed"
    assert balance["balance"] == "10.50"
