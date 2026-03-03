from __future__ import annotations
import pytest
import pytest
import asyncio
import logging
import json
import sys
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parent))

from apify_api import ApifyAPI


@pytest.mark.asyncio
async def test_zuck_post_with_replies():
    logging.getLogger("httpx").setLevel(logging.WARNING)

    # Включаем сбор вложенных ответов
    api = ApifyAPI(results_limit=5, include_nested_comments=True)

    test_url = "https://www.facebook.com/zuck/posts/10102577175875681/"

    print(f"--- Запускаем парсинг с ОТВЕТАМИ: {test_url} ---")

    try:
        comments, debug = await api.run_facebook_comments_scraper(test_url)
        print("\n--- Результаты ---")
        for i, comment in enumerate(comments, 1):
            print(
                f"{i}. [{comment['author']}] (Лайков: {comment['likes_count']}, Дата: {comment['date']})"
            )
            print(f"   Текст: {comment['text'][:70]}...")

            if comment["replies"]:
                print(
                    f"   --- Ответы ({len(comment['replies'])} из {comment['replies_count']}):"
                )
                for j, reply in enumerate(comment["replies"], 1):
                    print(
                        f"       {i}.{j} [{reply['author']}]: {reply['text'][:50]}..."
                    )
            elif comment["replies_count"] > 0:
                print(
                    f"   --- Ответы есть ({comment['replies_count']}), но в этот раз не спарсились."
                )

        print("\n--- Debug Info ---")
        print(json.dumps(debug, indent=2))

    except Exception as e:
        print(f"\nОшибка при парсинге: {e}")
        if hasattr(e, "debug"):
            print(f"Debug details: {e.debug}")
    finally:
        await api.aclose()


if __name__ == "__main__":
    asyncio.run(test_zuck_post_with_replies())
