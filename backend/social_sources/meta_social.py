"""Official Meta social listening adapters.

Instagram: searches public hashtag media for a connected professional account.
Facebook: searches public Pages only when Page Public Content Access is approved.
Neither adapter reads arbitrary private profiles or private groups. TikTok commercial
public-post discovery is intentionally not wired to the Research API because TikTok
limits that API to approved non-profit/academic research.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlparse

import httpx

from config.settings import _resolve_file, Settings

logger = logging.getLogger(__name__)

_META_TIMEOUT = httpx.Timeout(20.0, connect=8.0)
_IG_HASHTAGS = (
    "chinesename",
    "chinesenames",
    "mandarin",
    "learnchinese",
    "chinesecharacters",
    "chineseculture",
)


def _usage_db() -> sqlite3.Connection:
    path = _resolve_file("social_api_usage.sqlite3")
    conn = sqlite3.connect(path, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS instagram_hashtag_usage (
            hashtag TEXT NOT NULL,
            searched_at TEXT NOT NULL,
            PRIMARY KEY (hashtag, searched_at)
        )
        """
    )
    conn.commit()
    return conn


def _claim_instagram_hashtag(hashtag: str) -> bool:
    """Conservatively stay below Meta's 30 unique hashtag searches per 7 days."""
    now = datetime.now(timezone.utc)
    cutoff = (now - timedelta(days=7)).isoformat()
    with _usage_db() as conn:
        row = conn.execute(
            "SELECT 1 FROM instagram_hashtag_usage WHERE hashtag=? AND searched_at>=? LIMIT 1",
            (hashtag, cutoff),
        ).fetchone()
        if row:
            return False
        count = conn.execute(
            "SELECT COUNT(DISTINCT hashtag) AS n FROM instagram_hashtag_usage WHERE searched_at>=?",
            (cutoff,),
        ).fetchone()["n"]
        if int(count) >= 25:
            return False
        conn.execute(
            "INSERT INTO instagram_hashtag_usage(hashtag, searched_at) VALUES (?, ?)",
            (hashtag, now.isoformat()),
        )
        conn.commit()
    return True


def _api_error(payload: dict[str, Any]) -> str:
    error = payload.get("error")
    if isinstance(error, dict):
        return str(error.get("message") or error.get("type") or "Meta API error")[:500]
    return ""


async def search_instagram_hashtag_media(
    settings: Settings,
    *,
    max_per_hashtag: int = 10,
) -> tuple[list[dict[str, str]], dict[str, Any]]:
    token = settings.instagram_access_token.strip()
    ig_user_id = settings.instagram_business_account_id.strip()
    if not token or not ig_user_id:
        return [], {
            "platform": "Instagram",
            "mode": "not_configured",
            "message": "需要连接 Instagram Professional 账号并配置 Access Token 与 IG User ID。",
            "results": 0,
        }

    version = settings.meta_graph_api_version.strip() or "v26.0"
    base = f"https://graph.facebook.com/{version}"
    candidates: list[dict[str, str]] = []
    errors: list[str] = []
    searched = 0

    async with httpx.AsyncClient(timeout=_META_TIMEOUT) as client:
        for hashtag in _IG_HASHTAGS:
            if not _claim_instagram_hashtag(hashtag):
                continue
            searched += 1
            try:
                response = await client.get(
                    f"{base}/{ig_user_id}/ig_hashtag_search",
                    params={"user_id": ig_user_id, "q": hashtag, "access_token": token},
                )
                response.raise_for_status()
                payload = response.json()
                error = _api_error(payload)
                if error:
                    errors.append(f"#{hashtag}: {error}")
                    continue
                rows = payload.get("data") or []
                if not rows:
                    continue
                hashtag_id = str(rows[0].get("id") or "")
                if not hashtag_id:
                    continue
                media_response = await client.get(
                    f"{base}/{hashtag_id}/recent_media",
                    params={
                        "user_id": ig_user_id,
                        "fields": "id,caption,media_type,permalink,timestamp",
                        "limit": max(1, min(25, max_per_hashtag)),
                        "access_token": token,
                    },
                )
                media_response.raise_for_status()
                media_payload = media_response.json()
                media_error = _api_error(media_payload)
                if media_error:
                    errors.append(f"#{hashtag} media: {media_error}")
                    continue
                for item in media_payload.get("data") or []:
                    url = str(item.get("permalink") or "")
                    if not url:
                        continue
                    caption = str(item.get("caption") or "").strip()
                    title = caption.splitlines()[0][:300] if caption else f"Instagram post tagged #{hashtag}"
                    candidates.append({
                        "title": title,
                        "url": url,
                        "snippet": caption[:1800],
                        "source_platform": "Instagram",
                        "source_domain": urlparse(url).netloc or "www.instagram.com",
                        "search_query": f"Instagram official hashtag #{hashtag}",
                        "source_method": "official_meta_hashtag_api",
                    })
            except (httpx.HTTPError, ValueError) as exc:
                errors.append(f"#{hashtag}: {str(exc)[:300]}")
            await asyncio.sleep(0.15)

    return candidates, {
        "platform": "Instagram",
        "mode": "official_hashtag_api",
        "hashtags_searched": searched,
        "results": len(candidates),
        "comments_access": "Only comments on media owned by the connected account are available; third-party hashtag media comments are not fetched.",
        "errors": errors[:8],
    }


async def search_facebook_public_pages(
    settings: Settings,
    *,
    max_pages: int = 5,
    max_posts_per_page: int = 8,
) -> tuple[list[dict[str, str]], dict[str, Any]]:
    token = settings.meta_access_token.strip()
    approved = bool(settings.meta_page_public_content_access_approved)
    if not token or not approved:
        return [], {
            "platform": "Facebook",
            "mode": "approval_required" if not approved else "not_configured",
            "message": (
                "需要 Meta App Review 批准 Page Public Content Access，并配置 Meta Access Token。"
                if not approved
                else "需要配置 Meta Access Token。"
            ),
            "results": 0,
        }

    version = settings.meta_graph_api_version.strip() or "v26.0"
    base = f"https://graph.facebook.com/{version}"
    candidates: list[dict[str, str]] = []
    errors: list[str] = []
    queries = ("Chinese name", "Chinese names", "learn Chinese")

    async with httpx.AsyncClient(timeout=_META_TIMEOUT) as client:
        seen_pages: set[str] = set()
        for query in queries:
            try:
                response = await client.get(
                    f"{base}/search",
                    params={
                        "type": "page",
                        "q": query,
                        "fields": "id,name,link",
                        "limit": max_pages,
                        "access_token": token,
                    },
                )
                response.raise_for_status()
                payload = response.json()
                error = _api_error(payload)
                if error:
                    errors.append(f"Page search: {error}")
                    continue
                for page in payload.get("data") or []:
                    page_id = str(page.get("id") or "")
                    page_name = str(page.get("name") or "Facebook Page")
                    if not page_id or page_id in seen_pages:
                        continue
                    seen_pages.add(page_id)
                    try:
                        posts_response = await client.get(
                            f"{base}/{page_id}/posts",
                            params={
                                "fields": "id,message,permalink_url,created_time",
                                "limit": max(1, min(25, max_posts_per_page)),
                                "access_token": token,
                            },
                        )
                        posts_response.raise_for_status()
                        posts_payload = posts_response.json()
                        posts_error = _api_error(posts_payload)
                        if posts_error:
                            errors.append(f"{page_name}: {posts_error}")
                            continue
                        for post in posts_payload.get("data") or []:
                            url = str(post.get("permalink_url") or "")
                            message = str(post.get("message") or "").strip()
                            if not url or not message:
                                continue
                            candidates.append({
                                "title": message.splitlines()[0][:300],
                                "url": url,
                                "snippet": message[:1800],
                                "source_platform": "Facebook",
                                "source_domain": "www.facebook.com",
                                "search_query": f"Facebook public Page search: {query}",
                                "source_method": "official_meta_page_public_content",
                            })
                    except (httpx.HTTPError, ValueError) as exc:
                        errors.append(f"{page_name}: {str(exc)[:300]}")
            except (httpx.HTTPError, ValueError) as exc:
                errors.append(f"Page search {query}: {str(exc)[:300]}")
            await asyncio.sleep(0.15)

    return candidates, {
        "platform": "Facebook",
        "mode": "official_page_public_content_api",
        "pages_checked": len(seen_pages),
        "results": len(candidates),
        "group_access": "Facebook Groups API is not available; this adapter searches public Pages only.",
        "errors": errors[:8],
    }


async def search_official_meta_sources(
    settings: Settings,
    *,
    include_instagram: bool,
    include_facebook: bool,
    max_results: int,
) -> tuple[list[dict[str, str]], dict[str, Any]]:
    tasks = []
    if include_instagram:
        tasks.append(search_instagram_hashtag_media(settings, max_per_hashtag=max(1, min(25, max_results))))
    if include_facebook:
        tasks.append(search_facebook_public_pages(settings, max_posts_per_page=max(1, min(25, max_results))))
    if not tasks:
        return [], {}
    results = await asyncio.gather(*tasks, return_exceptions=True)
    candidates: list[dict[str, str]] = []
    status: dict[str, Any] = {}
    for result in results:
        if isinstance(result, Exception):
            logger.exception("[SocialListening] Meta source failed", exc_info=result)
            continue
        rows, meta = result
        candidates.extend(rows)
        status[str(meta.get("platform", "Meta"))] = meta
    return candidates, status


def social_source_capabilities(settings: Settings) -> dict[str, dict[str, Any]]:
    return {
        "TikTok": {
            "mode": "public_search_index",
            "official_commercial_discovery": False,
            "message": "TikTok Research API is restricted to approved non-profit/academic research; commercial lead discovery uses public indexed pages only.",
        },
        "Instagram": {
            "mode": "official_hashtag_api" if settings.instagram_access_token and settings.instagram_business_account_id else "public_search_index",
            "configured": bool(settings.instagram_access_token and settings.instagram_business_account_id),
            "capability": "Public hashtag media captions; not arbitrary comments on third-party posts.",
            "requires": ["Instagram Professional account", "Meta app review and required permissions", "Access token", "IG User ID"],
        },
        "Facebook": {
            "mode": "official_page_public_content_api" if settings.meta_access_token and settings.meta_page_public_content_access_approved else "public_search_index",
            "configured": bool(settings.meta_access_token and settings.meta_page_public_content_access_approved),
            "capability": "Public Facebook Pages and their public posts; no Groups API.",
            "requires": ["Meta app review", "Page Public Content Access approval", "Access token"],
        },
    }
