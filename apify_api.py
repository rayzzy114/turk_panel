from __future__ import annotations

import os
from typing import Any

import httpx
from dotenv import load_dotenv

load_dotenv()


class ApifyAPIError(RuntimeError):
    def __init__(self, message: str, *, debug: list[str] | None = None) -> None:
        super().__init__(message)
        self.debug = list(debug or [])


class ApifyAPI:
    def __init__(
        self,
        *,
        api_token: str | None = None,
        actor_id: str | None = None,
        results_limit: int | None = None,
        include_nested_comments: bool | None = None,
        view_option: str | None = None,
        timeout_seconds: int | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.api_token = api_token or os.getenv("APIFY_API_TOKEN")
        if not self.api_token:
            raise ApifyAPIError("APIFY_API_TOKEN is not set.")

        self.actor_id = actor_id or os.getenv(
            "APIFY_ACTOR_ID", "apify/facebook-comments-scraper"
        )
        self.results_limit = (
            results_limit
            if results_limit is not None
            else int(os.getenv("APIFY_RESULTS_LIMIT", "50"))
        )
        include_replies_raw = os.getenv("APIFY_INCLUDE_REPLIES", "false")
        self.include_nested_comments = (
            include_nested_comments
            if include_nested_comments is not None
            else include_replies_raw.strip().lower() in {"1", "true", "yes", "on"}
        )
        self.view_option = view_option or os.getenv(
            "APIFY_VIEW_OPTION", "RANKED_UNFILTERED"
        )
        self.timeout_seconds = (
            timeout_seconds
            if timeout_seconds is not None
            else int(os.getenv("APIFY_TIMEOUT_SECONDS", "90"))
        )
        request_timeout = max(self.timeout_seconds + 15, 30)
        self._client = httpx.AsyncClient(
            base_url="https://api.apify.com",
            timeout=float(request_timeout),
            transport=transport,
            headers={"Authorization": f"Bearer {self.api_token}"},
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def run_facebook_comments_scraper(
        self, url: str
    ) -> tuple[list[dict[str, Any]], list[str]]:
        debug = [f"actor={self.actor_id}", f"url={url}"]
        run_payload = {
            "startUrls": [{"url": url}],
            "resultsLimit": self.results_limit,
            "includeNestedComments": self.include_nested_comments,
            "viewOption": self.view_option,
        }
        url_actor_id = self.actor_id.replace("/", "~")
        run_response = await self._client.post(
            f"/v2/acts/{url_actor_id}/runs",
            params={"waitForFinish": self.timeout_seconds},
            json=run_payload,
        )
        try:
            if run_response.status_code != 201 and run_response.status_code != 200:
                debug.append(f"http_status={run_response.status_code}")
                debug.append(f"http_body={run_response.text}")
            run_response.raise_for_status()
        except Exception as exc:
            raise ApifyAPIError(
                "Failed to start Apify actor run.", debug=debug
            ) from exc

        run_body = run_response.json()
        run_data = run_body.get("data") if isinstance(run_body, dict) else None
        if not isinstance(run_data, dict):
            raise ApifyAPIError("Apify run response has invalid format.", debug=debug)

        run_id = run_data.get("id")
        run_status = run_data.get("status")
        dataset_id = run_data.get("defaultDatasetId")
        debug.extend(
            [f"run_id={run_id}", f"run_status={run_status}", f"dataset_id={dataset_id}"]
        )

        if run_status != "SUCCEEDED":
            raise ApifyAPIError(
                f"Apify actor run did not succeed: {run_status}", debug=debug
            )
        if not dataset_id:
            raise ApifyAPIError(
                "Apify actor run finished without dataset ID.", debug=debug
            )

        items_response = await self._client.get(
            f"/v2/datasets/{dataset_id}/items",
            params={"clean": "true"},
        )
        try:
            items_response.raise_for_status()
        except Exception as exc:
            raise ApifyAPIError(
                "Failed to fetch Apify dataset items.", debug=debug
            ) from exc

        items = items_response.json()
        if not isinstance(items, list):
            raise ApifyAPIError("Apify dataset response is not a list.", debug=debug)

        comments = [_normalize_item(item) for item in items if isinstance(item, dict)]
        if self.results_limit is not None:
            comments = comments[: self.results_limit]

        debug.append(f"items_count={len(comments)}")
        return comments, debug


def _parse_human_int(value: Any) -> int:
    if not value:
        return 0
    if isinstance(value, int):
        return value

    clean = str(value).strip().upper().replace(",", "")
    if not clean:
        return 0

    multiplier = 1
    if clean.endswith("K"):
        multiplier = 1000
        clean = clean[:-1]
    elif clean.endswith("M"):
        multiplier = 1000000
        clean = clean[:-1]
    elif clean.endswith("B"):
        multiplier = 1000000000
        clean = clean[:-1]

    try:
        return int(float(clean) * multiplier)
    except (ValueError, TypeError):
        return 0


def _normalize_item(item: dict[str, Any]) -> dict[str, str | int | list[Any]]:
    author = (
        item.get("profileName")
        or item.get("authorName")
        or item.get("author")
        or item.get("facebookName")
        or "Unknown"
    )
    author_id = (
        item.get("profileId")
        or item.get("authorId")
        or item.get("facebookId")
        or item.get("userId")
        or item.get("profile_id")
        or item.get("author_id")
        or ""
    )
    text = item.get("text") or item.get("commentText") or item.get("body") or ""
    comment_url = item.get("commentUrl") or item.get("url") or ""

    date_str = item.get("date") or item.get("timestamp") or ""
    likes_count = _parse_human_int(item.get("likesCount"))
    replies_count = _parse_human_int(item.get("commentsCount"))

    replies = []
    raw_replies = item.get("comments") or []
    if isinstance(raw_replies, list):
        replies = [_normalize_item(r) for r in raw_replies if isinstance(r, dict)]

    return {
        "author": str(author).strip(),
        "author_id": str(author_id).strip(),
        "text": str(text).strip(),
        "comment_url": str(comment_url).strip(),
        "date": str(date_str),
        "likes_count": likes_count,
        "replies_count": replies_count,
        "replies": replies,
    }
