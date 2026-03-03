from __future__ import annotations
import sys
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parents[1]))

from apify_api import _normalize_item


def test_normalize_item_various_fields() -> None:
    # Test alternative author fields
    assert _normalize_item({"authorName": "Alice", "text": "Hi"})["author"] == "Alice"
    assert _normalize_item({"author": "Bob", "text": "Hi"})["author"] == "Bob"
    assert (
        _normalize_item({"facebookName": "Charlie", "text": "Hi"})["author"]
        == "Charlie"
    )
    assert (
        _normalize_item({"profileName": "Dave", "authorName": "Alice"})["author"]
        == "Dave"
    )  # profileName is first priority
    assert _normalize_item({"text": "Hi"})["author"] == "Unknown"

    # Test alternative text fields
    assert _normalize_item({"profileName": "A", "commentText": "Msg"})["text"] == "Msg"
    assert _normalize_item({"profileName": "A", "body": "Content"})["text"] == "Content"
    assert (
        _normalize_item({"profileName": "A", "text": "Primary", "body": "Secondary"})[
            "text"
        ]
        == "Primary"
    )

    # Test alternative url fields
    assert (
        _normalize_item({"profileName": "A", "url": "http://link"})["comment_url"]
        == "http://link"
    )
    assert (
        _normalize_item(
            {
                "profileName": "A",
                "commentUrl": "http://primary",
                "url": "http://secondary",
            }
        )["comment_url"]
        == "http://primary"
    )


def test_normalize_item_strips_and_casts_to_str() -> None:
    item = {
        "profileName": "  Spacey Name  ",
        "text": 123,  # non-string
        "commentUrl": None,
    }
    result = _normalize_item(item)
    assert result["author"] == "Spacey Name"
    assert result["text"] == "123"
    assert result["comment_url"] == ""
