from __future__ import annotations

import os
from typing import Any

import httpx
from dotenv import load_dotenv

load_dotenv()


class MtpAPI:
    def __init__(
        self,
        *,
        base_url: str | None = None,
        api_key: str | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.base_url = base_url or os.getenv(
            "MORETHAN_API_URL", "https://morethanpanel.com/api/v2"
        )
        self.api_key = api_key or os.getenv(
            "MORETHAN_API_KEY", "894a41bd2bb97c2b1aaab433bc576013"
        )
        if not self.api_key:
            raise RuntimeError("Не задан MORETHAN_API_KEY.")
        self._client = httpx.AsyncClient(
            base_url=self.base_url, timeout=30.0, transport=transport
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _post(self, action: str, **payload: Any) -> Any:
        form_data: dict[str, Any] = {"key": self.api_key, "action": action}
        form_data.update(payload)
        response = await self._client.post("", data=form_data)
        response.raise_for_status()

        try:
            data = response.json()
        except Exception:
            raise RuntimeError(f"MoreThanPanel API вернул не JSON: {response.text}")

        if isinstance(data, dict) and data.get("error"):
            raise RuntimeError(f"MoreThanPanel API error: {data['error']}")
        return data

    async def add_order(
        self, service_id: int, link: str, quantity: int, **kwargs: Any
    ) -> int:
        result = await self._post(
            "add", service=service_id, link=link, quantity=quantity, **kwargs
        )
        if not isinstance(result, dict):
            raise RuntimeError("MoreThanPanel API вернул некорректный формат ответа.")
        order = result.get("order")
        if order is None:
            raise RuntimeError("MoreThanPanel API не вернул ID заказа.")
        return int(order)

    async def get_status(self, order_id: int) -> dict[str, Any]:
        return await self._post("status", order=order_id)

    async def get_balance(self) -> dict[str, Any]:
        return await self._post("balance")
