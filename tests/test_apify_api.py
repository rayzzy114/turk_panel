from __future__ import annotations

import sys
from pathlib import Path

import httpx
import pytest

sys.path.append(str(Path(__file__).resolve().parents[1]))

from apify_api import ApifyAPI, ApifyAPIError


@pytest.mark.asyncio
async def test_apify_api_returns_normalized_comments() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.startswith("/v2/acts/") and request.url.path.endswith(
            "/runs"
        ):
            return httpx.Response(
                200,
                json={
                    "data": {
                        "id": "run-1",
                        "status": "SUCCEEDED",
                        "defaultDatasetId": "dataset-1",
                    }
                },
            )
        if request.url.path == "/v2/datasets/dataset-1/items":
            return httpx.Response(
                200,
                json=[
                    {
                        "profileName": "Alice",
                        "text": "Hello",
                        "commentUrl": "https://example.com/c/1",
                    },
                    {
                        "profileName": "Bob",
                        "text": "World",
                        "commentUrl": "https://example.com/c/2",
                    },
                ],
            )
        return httpx.Response(404, json={"error": "not found"})

    api = ApifyAPI(api_token="token", transport=httpx.MockTransport(handler))
    try:
        comments, debug = await api.run_facebook_comments_scraper(
            "https://example.com/post"
        )
    finally:
        await api.aclose()

    assert comments == [
        {
            "author": "Alice",
            "author_id": "",
            "text": "Hello",
            "comment_url": "https://example.com/c/1",
            "date": "",
            "likes_count": 0,
            "replies_count": 0,
            "replies": [],
        },
        {
            "author": "Bob",
            "author_id": "",
            "text": "World",
            "comment_url": "https://example.com/c/2",
            "date": "",
            "likes_count": 0,
            "replies_count": 0,
            "replies": [],
        },
    ]

    assert any("run_id=run-1" in line for line in debug)
    assert any("items_count=2" in line for line in debug)


@pytest.mark.asyncio
async def test_apify_api_raises_when_run_not_succeeded() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.startswith("/v2/acts/") and request.url.path.endswith(
            "/runs"
        ):
            return httpx.Response(
                200,
                json={
                    "data": {
                        "id": "run-2",
                        "status": "TIMED-OUT",
                        "defaultDatasetId": "dataset-2",
                    }
                },
            )
        return httpx.Response(404, json={"error": "not found"})

    api = ApifyAPI(api_token="token", transport=httpx.MockTransport(handler))
    try:
        with pytest.raises(ApifyAPIError):
            await api.run_facebook_comments_scraper("https://example.com/post")
    finally:
        await api.aclose()
