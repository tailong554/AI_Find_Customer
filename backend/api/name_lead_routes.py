"""Public-post lead discovery for people explicitly asking for a Chinese name.

This module deliberately stores public post URLs and post excerpts only. It does
not harvest private contact details, infer sensitive traits, or contact users.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from api.security import require_api_access
from config.settings import _resolve_file, get_settings
from tools.google_search import GoogleSearchTool
from tools.llm_client import LLMTool
from tools.web_search import WebSearchTool

logger = logging.getLogger(__name__)
router = APIRouter()

SEARCH_QUERIES = [
    'site:reddit.com "I want a Chinese name"',
    'site:reddit.com "help me choose" "Chinese name"',
    'site:reddit.com "looking for a Chinese name"',
    'site:reddit.com "what Chinese name should I choose"',
    'site:reddit.com "Chinese name suggestions" "for me"',
    'site:quora.com "Chinese name" "for me" suggestions',
    '"need a Chinese name" forum',
    '"help me pick a Chinese name" -dictionary',
]

DIRECT_PHRASES = (
    "i want a chinese name",
    "i need a chinese name",
    "need a chinese name",
    "looking for a chinese name",
    "help me choose a chinese name",
    "help me pick a chinese name",
    "suggest a chinese name for me",
    "recommend a chinese name for me",
    "what chinese name should i",
    "what would be a good chinese name for me",
    "can someone give me a chinese name",
    "choose a chinese name for me",
    "pick a chinese name for me",
    "chinese name for myself",
    "chinese name suggestions for me",
)

RESEARCH_PHRASES = (
    "meaning of the chinese name",
    "popular chinese names",
    "list of chinese names",
    "how are chinese names",
    "history of chinese names",
    "chinese names mean",
)


class NameLeadSearchRequest(BaseModel):
    country_code: str = Field(default="", description="Optional Google country code: us, gb, ca, au, etc.")
    max_results_per_query: int = Field(default=6, ge=1, le=10)
    max_candidates: int = Field(default=40, ge=1, le=80)
    include_reddit: bool = True
    include_other_forums: bool = True
    include_tiktok: bool = True
    include_instagram: bool = True
    include_facebook: bool = True


class NameLeadStatusRequest(BaseModel):
    status: Literal["new", "reviewed", "replied", "converted", "dismissed"]


class NameLead(BaseModel):
    id: str
    title: str
    url: str
    snippet: str
    source_platform: str
    source_domain: str
    search_query: str
    intent_type: Literal["direct_request", "research", "irrelevant"]
    intent_score: int = Field(ge=0, le=100)
    purchase_signal: Literal["explicit", "possible", "unknown"]
    reason: str
    evidence_quote: str
    reply_draft: str
    status: str
    created_at: str
    updated_at: str


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _database_path() -> Path:
    override = os.environ.get("NAME_LEAD_DB_PATH", "").strip()
    path = Path(override).expanduser() if override else Path(_resolve_file("name_leads.sqlite3"))
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(str(_database_path()), timeout=20)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS name_intent_leads (
            id TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            url TEXT NOT NULL UNIQUE,
            snippet TEXT NOT NULL DEFAULT '',
            source_platform TEXT NOT NULL DEFAULT '',
            source_domain TEXT NOT NULL DEFAULT '',
            search_query TEXT NOT NULL DEFAULT '',
            intent_type TEXT NOT NULL DEFAULT 'irrelevant',
            intent_score INTEGER NOT NULL DEFAULT 0,
            purchase_signal TEXT NOT NULL DEFAULT 'unknown',
            reason TEXT NOT NULL DEFAULT '',
            evidence_quote TEXT NOT NULL DEFAULT '',
            reply_draft TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'new',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    conn.commit()
    return conn


def _canonical_url(value: str) -> str:
    parsed = urlparse((value or "").strip())
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return ""
    host = parsed.netloc.lower()
    if host.startswith("www."):
        host = host[4:]
    ignored = {"utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content", "ref", "fbclid"}
    query = [(k, v) for k, v in parse_qsl(parsed.query, keep_blank_values=True) if k.lower() not in ignored]
    path = parsed.path.rstrip("/") or "/"
    return urlunparse((parsed.scheme.lower(), host, path, "", urlencode(query), ""))


def _platform_for(url: str) -> tuple[str, str]:
    host = urlparse(url).netloc.lower().removeprefix("www.")
    if host.endswith("reddit.com"):
        return "Reddit", host
    if host.endswith("tiktok.com"):
        return "TikTok", host
    if host.endswith("instagram.com"):
        return "Instagram", host
    if host.endswith("facebook.com"):
        return "Facebook", host
    if host.endswith("quora.com"):
        return "Quora", host
    if "stackexchange.com" in host or host.endswith("stackoverflow.com"):
        return "Stack Exchange", host
    if "forum" in host or "community" in host:
        return "Forum", host
    return "Web", host


def _candidate_id(url: str) -> str:
    return hashlib.sha256(url.encode("utf-8")).hexdigest()[:20]


def _heuristic_classification(candidate: dict) -> dict:
    text = f"{candidate.get('title', '')} {candidate.get('snippet', '')}".lower()
    direct_hits = [phrase for phrase in DIRECT_PHRASES if phrase in text]
    research_hits = [phrase for phrase in RESEARCH_PHRASES if phrase in text]
    if direct_hits:
        score = min(96, 76 + 5 * len(direct_hits))
        return {
            "intent_type": "direct_request",
            "intent_score": score,
            "purchase_signal": "unknown",
            "reason": "The public post appears to ask for a Chinese name or personalized suggestions.",
            "evidence_quote": direct_hits[0],
            "reply_draft": (
                "A good Chinese name should sound natural and have a meaning that fits you. "
                "If you share the meaning or style you prefer, I can suggest a few options and explain each character."
            ),
        }
    if research_hits:
        return {
            "intent_type": "research",
            "intent_score": 30,
            "purchase_signal": "unknown",
            "reason": "The result appears informational rather than a direct request for a personal name.",
            "evidence_quote": research_hits[0],
            "reply_draft": "",
        }
    return {
        "intent_type": "irrelevant",
        "intent_score": 5,
        "purchase_signal": "unknown",
        "reason": "No clear first-person request for a Chinese name was found in the indexed text.",
        "evidence_quote": "",
        "reply_draft": "",
    }


def _extract_json_object(raw: str) -> dict:
    text = (raw or "").strip()
    try:
        value = json.loads(text)
        if isinstance(value, dict):
            return value
    except json.JSONDecodeError:
        pass
    match = re.search(r"\{.*\}", text, flags=re.DOTALL)
    if not match:
        raise ValueError("LLM response did not contain a JSON object")
    value = json.loads(match.group(0))
    if not isinstance(value, dict):
        raise ValueError("LLM response JSON must be an object")
    return value


async def _classify_batch(candidates: list[dict]) -> list[dict]:
    if not candidates:
        return []
    fallback = {item["id"]: _heuristic_classification(item) for item in candidates}
    try:
        llm = LLMTool(agent="name_intent_hunter")
        payload = [
            {"id": x["id"], "title": x["title"], "snippet": x["snippet"], "url": x["url"]}
            for x in candidates
        ]
        raw = await llm.generate(
            json.dumps(payload, ensure_ascii=False),
            system=(
                "You qualify public web posts for a small service that creates meaningful Chinese names "
                "for non-Chinese-speaking customers. Classify ONLY what the post itself says. "
                "Do not infer ethnicity, age, gender, location, income, identity, or willingness to pay. "
                "A direct_request requires first-person wording or an explicit request for a personal Chinese name. "
                "A general discussion, list, translation question, or cultural question is research or irrelevant. "
                "purchase_signal must be explicit only when the post itself mentions paying, hiring, commissioning, "
                "or buying a naming service; otherwise use unknown or possible only for a clear service-seeking request. "
                "evidence_quote must be an exact short substring of title/snippet; never invent evidence. "
                "Write a short, helpful, non-pushy reply_draft only for direct_request. Do not include a link. "
                "Return JSON only in this shape: "
                '{"leads":[{"id":"...","intent_type":"direct_request|research|irrelevant",'
                '"intent_score":0,"purchase_signal":"explicit|possible|unknown","reason":"...",'
                '"evidence_quote":"...","reply_draft":"..."}]}'
            ),
            temperature=0.1,
            max_tokens=5000,
            response_format={"type": "json_object"},
        )
        parsed = _extract_json_object(raw)
        rows = parsed.get("leads", [])
        by_id = {str(row.get("id", "")): row for row in rows if isinstance(row, dict)}
        classified = []
        for candidate in candidates:
            row = by_id.get(candidate["id"], {})
            base = fallback[candidate["id"]]
            intent_type = str(row.get("intent_type", base["intent_type"]))
            if intent_type not in {"direct_request", "research", "irrelevant"}:
                intent_type = base["intent_type"]
            purchase_signal = str(row.get("purchase_signal", base["purchase_signal"]))
            if purchase_signal not in {"explicit", "possible", "unknown"}:
                purchase_signal = "unknown"
            try:
                score = max(0, min(100, int(row.get("intent_score", base["intent_score"]))))
            except (TypeError, ValueError):
                score = base["intent_score"]
            if intent_type == "irrelevant":
                score = min(score, 25)
            elif intent_type == "research":
                score = min(score, 55)
            evidence = str(row.get("evidence_quote", "") or "").strip()
            searchable = f"{candidate['title']} {candidate['snippet']}".lower()
            if evidence and evidence.lower() not in searchable:
                evidence = ""
            classified.append({
                "intent_type": intent_type,
                "intent_score": score,
                "purchase_signal": purchase_signal,
                "reason": str(row.get("reason", base["reason"]))[:400],
                "evidence_quote": evidence,
                "reply_draft": str(row.get("reply_draft", base["reply_draft"]))[:1200]
                if intent_type == "direct_request" else "",
            })
        return classified
    except Exception as exc:
        logger.warning("[NameLeadHunter] LLM classification unavailable; using evidence rules: %s", exc)
        return [fallback[item["id"]] for item in candidates]


def _build_queries(request: NameLeadSearchRequest) -> list[str]:
    queries: list[str] = []
    if request.include_reddit:
        queries.extend(SEARCH_QUERIES[:5])
    if request.include_other_forums:
        queries.extend(SEARCH_QUERIES[5:])
    if request.include_tiktok:
        queries.extend([
            'site:tiktok.com "I want a Chinese name"',
            'site:tiktok.com "need a Chinese name" OR "Chinese name for me"',
            'site:tiktok.com "help me choose a Chinese name"',
        ])
    if request.include_instagram:
        queries.extend([
            'site:instagram.com/p/ "I want a Chinese name"',
            'site:instagram.com/reel/ "Chinese name for me"',
            'site:instagram.com "help me choose a Chinese name"',
        ])
    if request.include_facebook:
        queries.extend([
            'site:facebook.com/groups/ "I need a Chinese name"',
            'site:facebook.com "looking for a Chinese name"',
            'site:facebook.com "help me choose a Chinese name"',
        ])
    if not queries:
        queries = SEARCH_QUERIES[:5]
    return queries


async def _search_one(tool, query: str, request: NameLeadSearchRequest) -> tuple[str, list[dict]]:
    results = await tool.search(
        query,
        num=request.max_results_per_query,
        gl=request.country_code.strip().lower(),
        hl="en",
    )
    return query, results


async def _search_candidates(request: NameLeadSearchRequest) -> tuple[list[dict], str, list[str]]:
    settings = get_settings()
    if settings.serper_api_key:
        tool = GoogleSearchTool(settings)
        provider = "Google / Serper"
    elif settings.tavily_api_key or settings.youcom_api_key:
        tool = WebSearchTool(settings)
        provider = f"Web search ({tool.backend})"
    else:
        raise HTTPException(
            status_code=400,
            detail="请先在系统设置中配置 SERPER_API_KEY、TAVILY_API_KEY 或 YOUCOM_API_KEY。",
        )

    queries = _build_queries(request)
    semaphore = asyncio.Semaphore(3)

    async def bounded(query: str):
        async with semaphore:
            try:
                return await _search_one(tool, query, request)
            except Exception as exc:
                logger.warning("[NameLeadHunter] Search failed for %r: %s", query, exc)
                return query, []

    try:
        batches = await asyncio.gather(*(bounded(query) for query in queries))
    finally:
        await tool.close()

    deduped: dict[str, dict] = {}
    for query, results in batches:
        for result in results:
            url = _canonical_url(str(result.get("link", result.get("url", "")) or ""))
            if not url or url in deduped:
                continue
            title = str(result.get("title", "") or "").strip()
            snippet = str(result.get("snippet", result.get("content", "")) or "").strip()
            platform, domain = _platform_for(url)
            deduped[url] = {
                "id": _candidate_id(url),
                "title": title[:500],
                "url": url,
                "snippet": snippet[:1800],
                "source_platform": platform,
                "source_domain": domain,
                "search_query": query,
            }
            if len(deduped) >= request.max_candidates:
                break
        if len(deduped) >= request.max_candidates:
            break
    return list(deduped.values()), provider, queries


def _save_leads(leads: list[dict]) -> int:
    if not leads:
        return 0
    now = _now()
    inserted = 0
    with _connect() as conn:
        for lead in leads:
            cursor = conn.execute(
                """
                INSERT INTO name_intent_leads (
                    id, title, url, snippet, source_platform, source_domain, search_query,
                    intent_type, intent_score, purchase_signal, reason, evidence_quote,
                    reply_draft, status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'new', ?, ?)
                ON CONFLICT(url) DO UPDATE SET
                    title=excluded.title,
                    snippet=excluded.snippet,
                    source_platform=excluded.source_platform,
                    source_domain=excluded.source_domain,
                    search_query=excluded.search_query,
                    intent_type=excluded.intent_type,
                    intent_score=excluded.intent_score,
                    purchase_signal=excluded.purchase_signal,
                    reason=excluded.reason,
                    evidence_quote=excluded.evidence_quote,
                    reply_draft=excluded.reply_draft,
                    updated_at=excluded.updated_at
                """,
                (
                    lead["id"], lead["title"], lead["url"], lead["snippet"],
                    lead["source_platform"], lead["source_domain"], lead["search_query"],
                    lead["intent_type"], lead["intent_score"], lead["purchase_signal"],
                    lead["reason"], lead["evidence_quote"], lead["reply_draft"], now, now,
                ),
            )
            inserted += max(cursor.rowcount, 0)
    return inserted


def _row_to_dict(row: sqlite3.Row) -> dict:
    return dict(row)


@router.post("/name-leads/search", dependencies=[Depends(require_api_access)])
async def search_name_leads(request: NameLeadSearchRequest):
    """Search public posts and rank explicit requests for personal Chinese names."""
    candidates, provider, queries = await _search_candidates(request)
    classifications: list[dict] = []
    for start in range(0, len(candidates), 12):
        classifications.extend(await _classify_batch(candidates[start:start + 12]))

    leads = []
    for candidate, classification in zip(candidates, classifications):
        leads.append({**candidate, **classification, "status": "new"})
    _save_leads(leads)
    leads.sort(key=lambda item: item["intent_score"], reverse=True)
    return {
        "provider": provider,
        "queries_run": queries,
        "candidate_count": len(candidates),
        "direct_requests": sum(1 for item in leads if item["intent_type"] == "direct_request"),
        "high_intent_count": sum(1 for item in leads if item["intent_score"] >= 70),
        "leads": leads,
        "notice": "仅检索公开帖子；评分依据可见文本，不代表用户愿意付费。请人工审核后再回复。",
    }


@router.get("/name-leads", dependencies=[Depends(require_api_access)])
async def list_name_leads(
    status: str | None = Query(default=None),
    min_score: int = Query(default=0, ge=0, le=100),
    limit: int = Query(default=100, ge=1, le=200),
):
    """List saved public-post leads."""
    allowed = {"new", "reviewed", "replied", "converted", "dismissed"}
    if status and status not in allowed:
        raise HTTPException(status_code=400, detail="Invalid lead status.")
    sql = "SELECT * FROM name_intent_leads WHERE intent_score >= ?"
    params: list[object] = [min_score]
    if status:
        sql += " AND status = ?"
        params.append(status)
    sql += " ORDER BY intent_score DESC, updated_at DESC LIMIT ?"
    params.append(limit)
    with _connect() as conn:
        rows = conn.execute(sql, params).fetchall()
    return {"leads": [_row_to_dict(row) for row in rows], "count": len(rows)}


@router.patch("/name-leads/{lead_id}/status", dependencies=[Depends(require_api_access)])
async def update_name_lead_status(lead_id: str, request: NameLeadStatusRequest):
    """Update the operator's follow-up status for a lead."""
    with _connect() as conn:
        cursor = conn.execute(
            "UPDATE name_intent_leads SET status = ?, updated_at = ? WHERE id = ?",
            (request.status, _now(), lead_id),
        )
        if cursor.rowcount == 0:
            raise HTTPException(status_code=404, detail="Name lead not found.")
        row = conn.execute("SELECT * FROM name_intent_leads WHERE id = ?", (lead_id,)).fetchone()
    return {"lead": _row_to_dict(row)}
