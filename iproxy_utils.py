from __future__ import annotations

import logging
from typing import Any

import httpx

LOGGER = logging.getLogger("iproxy_utils")


async def rotate_mobile_ip(rotation_url: str) -> bool:
    """
    Rotates a mobile proxy IP by calling provider HTTP endpoint.

    Returns True when rotation succeeds or when URL is not provided.
    """
    if not rotation_url:
        return True

    try:
        timeout = httpx.Timeout(10.0)
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.get(rotation_url)
            response.raise_for_status()
            payload: Any = None
            try:
                payload = response.json()
            except Exception:
                payload = response.text.strip()

        if isinstance(payload, dict):
            old_ip = payload.get("old_ip")
            new_ip = payload.get("new_ip")
            if old_ip or new_ip:
                LOGGER.info(
                    "Mobile proxy rotated via provider endpoint: %s -> %s",
                    old_ip or "unknown",
                    new_ip or "unknown",
                )
        return True
    except Exception as exc:
        LOGGER.warning("Failed to rotate mobile proxy via %s: %s", rotation_url, exc)
        return False


async def get_current_ip(proxy: str) -> str | None:
    """
    Resolves external IP address using ipify through the given proxy URL.
    """
    try:
        timeout = httpx.Timeout(10.0)
        async with httpx.AsyncClient(proxy=proxy, timeout=timeout) as client:
            response = await client.get("https://api.ipify.org")
            response.raise_for_status()
            ip = response.text.strip()
            return ip or None
    except Exception as exc:
        LOGGER.warning("Failed to resolve external IP via proxy %s: %s", proxy, exc)
        return None
