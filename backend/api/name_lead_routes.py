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
from social_sources.meta_social import search_official_meta_sources, social_source_capabilities

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
    status: Literal["new", "reviewed", "ready_to_contact", "contacted", "email_consented", "email_sent", "replied", "converted", "dismissed"]


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
            need_summary TEXT NOT NULL DEFAULT '',
            buying_signal_summary TEXT NOT NULL DEFAULT '',
            need_clarity_score INTEGER NOT NULL DEFAULT 0,
            purchase_stage_score INTEGER NOT NULL DEFAULT 0,
            urgency_score INTEGER NOT NULL DEFAULT 0,
            service_fit_score INTEGER NOT NULL DEFAULT 0,
            contactability_score INTEGER NOT NULL DEFAULT 0,
            score_explanation TEXT NOT NULL DEFAULT '',
            first_seen_at TEXT NOT NULL DEFAULT '',
            last_seen_at TEXT NOT NULL DEFAULT '',
            search_count INTEGER NOT NULL DEFAULT 1,
            status TEXT NOT NULL DEFAULT 'new',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    # Add columns safely for existing installations created by earlier versions.
    existing = {str(row["name"]) for row in conn.execute("PRAGMA table_info(name_intent_leads)").fetchall()}
    migrations = {
        "need_summary": "TEXT NOT NULL DEFAULT ''",
        "buying_signal_summary": "TEXT NOT NULL DEFAULT ''",
        "need_clarity_score": "INTEGER NOT NULL DEFAULT 0",
        "purchase_stage_score": "INTEGER NOT NULL DEFAULT 0",
        "urgency_score": "INTEGER NOT NULL DEFAULT 0",
        "service_fit_score": "INTEGER NOT NULL DEFAULT 0",
        "contactability_score": "INTEGER NOT NULL DEFAULT 0",
        "score_explanation": "TEXT NOT NULL DEFAULT ''",
        "first_seen_at": "TEXT NOT NULL DEFAULT ''",
        "last_seen_at": "TEXT NOT NULL DEFAULT ''",
        "search_count": "INTEGER NOT NULL DEFAULT 1",
    }
    for column, definition in migrations.items():
        if column not in existing:
            conn.execute(f"ALTER TABLE name_intent_leads ADD COLUMN {column} {definition}")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS name_lead_search_runs (
            id TEXT PRIMARY KEY,
            status TEXT NOT NULL DEFAULT 'running',
            provider TEXT NOT NULL DEFAULT '',
            queries_total INTEGER NOT NULL DEFAULT 0,
            queries_completed INTEGER NOT NULL DEFAULT 0,
            raw_result_count INTEGER NOT NULL DEFAULT 0,
            candidate_count INTEGER NOT NULL DEFAULT 0,
            direct_requests INTEGER NOT NULL DEFAULT 0,
            high_intent_count INTEGER NOT NULL DEFAULT 0,
            error_count INTEGER NOT NULL DEFAULT 0,
            platform_counts_json TEXT NOT NULL DEFAULT '{}',
            started_at TEXT NOT NULL,
            finished_at TEXT NOT NULL DEFAULT '',
            error_summary TEXT NOT NULL DEFAULT ''
        )
        """
    )
    conn.commit()
    return conn


def _create_search_run(run_id: str, query_count: int) -> None:
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO name_lead_search_runs
            (id, status, queries_total, started_at)
            VALUES (?, 'running', ?, ?)
            """,
            (run_id, query_count, _now()),
        )
        conn.commit()


def _update_search_progress(
    run_id: str,
    *,
    provider: str | None = None,
    queries_completed: int = 0,
    raw_result_count: int = 0,
    error_count: int = 0,
) -> None:
    with _connect() as conn:
        conn.execute(
            """
            UPDATE name_lead_search_runs
            SET provider=CASE WHEN ? IS NULL OR ?='' THEN provider ELSE ? END,
                queries_completed=queries_completed+?,
                raw_result_count=raw_result_count+?,
                error_count=error_count+?
            WHERE id=?
            """,
            (provider, provider, provider, queries_completed, raw_result_count, error_count, run_id),
        )
        conn.commit()


def _finish_search_run(
    run_id: str,
    *,
    status: str,
    candidate_count: int = 0,
    direct_requests: int = 0,
    high_intent_count: int = 0,
    platform_counts: dict[str, int] | None = None,
    error_summary: str = "",
) -> None:
    with _connect() as conn:
        conn.execute(
            """
            UPDATE name_lead_search_runs
            SET status=?, candidate_count=?, direct_requests=?, high_intent_count=?,
                platform_counts_json=?, error_summary=?, finished_at=?
            WHERE id=?
            """,
            (
                status, candidate_count, direct_requests, high_intent_count,
                json.dumps(platform_counts or {}, ensure_ascii=False),
                error_summary[:1000], _now() if status != "running" else "", run_id,
            ),
        )
        conn.commit()


def _score_dimensions(row: dict, fallback: dict | None = None) -> dict:
    fallback = fallback or {}
    limits = {
        "need_clarity_score": 3,
        "purchase_stage_score": 3,
        "urgency_score": 3,
        "service_fit_score": 3,
        "contactability_score": 2,
    }
    scores: dict[str, int] = {}
    for key, maximum in limits.items():
        try:
            value = int(row.get(key, fallback.get(key, 0)))
        except (TypeError, ValueError):
            value = int(fallback.get(key, 0))
        scores[key] = max(0, min(maximum, value))
    total = (
        scores["need_clarity_score"] / 3 * 25
        + scores["purchase_stage_score"] / 3 * 25
        + scores["urgency_score"] / 3 * 15
        + scores["service_fit_score"] / 3 * 25
        + scores["contactability_score"] / 2 * 10
    )
    scores["intent_score"] = max(0, min(100, round(total)))
    return scores


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
    platform = str(candidate.get("source_platform", "Web"))
    if direct_hits:
        dimensions = _score_dimensions({
            "need_clarity_score": 3,
            "purchase_stage_score": 2,
            "urgency_score": 0,
            "service_fit_score": 3,
            "contactability_score": 2,
        })
        reply = (
            "That sounds like a fun idea! A Chinese name is best when the characters, meaning, and pronunciation all fit. "
            "If you share the meaning or style you like, I can suggest a few natural options."
        )
        if platform == "Reddit":
            reply = (
                "A good Chinese name should sound natural and have a meaning that fits you, rather than being a literal translation. "
                "If you share the meaning, pronunciation, or style you prefer, I can suggest a few options and explain each character."
            )
        elif platform in {"TikTok", "Instagram"}:
            reply = (
                "Love this idea! A Chinese name should have a beautiful meaning and sound natural. "
                "Share the vibe or meaning you want and I can suggest a few options ✨"
            )
        elif platform == "Facebook":
            reply = (
                "Happy to help you explore a Chinese name. The best choice depends on the meaning and style you prefer. "
                "If you share a little about what you want the name to express, I can suggest some options."
            )
        return {
            "intent_type": "direct_request",
            **dimensions,
            "purchase_signal": "possible",
            "reason": "The public post appears to ask for a personal Chinese name or personalized suggestions.",
            "evidence_quote": direct_hits[0],
            "need_summary": "The author is looking for a Chinese name for personal use.",
            "buying_signal_summary": "They are actively seeking suggestions; willingness to pay is not established.",
            "score_explanation": "Clear personal request and strong service fit; urgency and payment intent are not stated.",
            "reply_draft": reply,
        }
    if research_hits:
        dimensions = _score_dimensions({
            "need_clarity_score": 1,
            "purchase_stage_score": 0,
            "urgency_score": 0,
            "service_fit_score": 1,
            "contactability_score": 0,
        })
        return {
            "intent_type": "research",
            **dimensions,
            "purchase_signal": "unknown",
            "reason": "The result appears informational rather than a direct request for a personal name.",
            "evidence_quote": research_hits[0],
            "need_summary": "The author appears to be researching Chinese names generally.",
            "buying_signal_summary": "No clear request for a paid or personalized naming service.",
            "score_explanation": "Informational content with no clear personal purchase request.",
            "reply_draft": "",
        }
    dimensions = _score_dimensions({})
    return {
        "intent_type": "irrelevant",
        **dimensions,
        "purchase_signal": "unknown",
        "reason": "No clear first-person request for a Chinese name was found in the indexed text.",
        "evidence_quote": "",
        "need_summary": "",
        "buying_signal_summary": "No buying signal found.",
        "score_explanation": "No explicit personal need was identified in the indexed text.",
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
            {
                "id": x["id"],
                "title": x["title"],
                "snippet": x["snippet"],
                "url": x["url"],
                "source_platform": x.get("source_platform", "Web"),
            }
            for x in candidates
        ]
        raw = await llm.generate(
            json.dumps(payload, ensure_ascii=False),
            system=(
                "You qualify public web posts for a personalized Chinese-name service. Classify ONLY what the public post says. "
                "Do not infer ethnicity, age, gender, location, income, identity, or willingness to pay. "
                "A direct_request requires a first-person request for a personal Chinese name. "
                "A general list, cultural discussion, translation question, or name-history question is research or irrelevant. "
                "Rate five evidence-based dimensions: need_clarity_score 0-3, purchase_stage_score 0-3, "
                "urgency_score 0-3, service_fit_score 0-3, contactability_score 0-2. "
                "Purchase stage: 0 research, 1 considering, 2 actively asking for help, 3 explicitly seeking to hire/buy. "
                "Urgency: 0 unstated, 1 soon, 2 time-bound, 3 urgent. Contactability is ONLY whether the public post invites "
                "a public response; never infer or extract private contact details. "
                "purchase_signal is explicit only if the post itself mentions paying/buying/hiring; possible for a clear request "
                "for personalized help; otherwise unknown. "
                "need_summary and buying_signal_summary must be short and grounded in the post. "
                "evidence_quote must be an exact substring of title/snippet. "
                "Write a short non-pushy public reply_draft adapted to the source platform: Reddit conversational and helpful; "
                "TikTok/Instagram concise and natural; Facebook warm and clear. Never include links or claim affiliation. "
                "Return JSON only: "
                '{"leads":[{"id":"...","intent_type":"direct_request|research|irrelevant",'
                '"purchase_signal":"explicit|possible|unknown","need_clarity_score":0,"purchase_stage_score":0,'
                '"urgency_score":0,"service_fit_score":0,"contactability_score":0,"reason":"...",'
                '"evidence_quote":"...","need_summary":"...","buying_signal_summary":"...",'
                '"score_explanation":"...","reply_draft":"..."}]}'
            ),
            temperature=0.1,
            max_tokens=6500,
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
            evidence = str(row.get("evidence_quote", "") or "").strip()
            searchable = f"{candidate['title']} {candidate['snippet']}".lower()
            if evidence and evidence.lower() not in searchable:
                evidence = ""
            dimension_values = _score_dimensions(row, base)
            if intent_type == "irrelevant":
                dimension_values = _score_dimensions({})
            elif intent_type == "research":
                dimension_values["intent_score"] = min(dimension_values["intent_score"], 55)
            classified.append({
                "intent_type": intent_type,
                **dimension_values,
                "purchase_signal": purchase_signal,
                "reason": str(row.get("reason", base["reason"]))[:400],
                "evidence_quote": evidence,
                "need_summary": str(row.get("need_summary", base["need_summary"]))[:500],
                "buying_signal_summary": str(row.get("buying_signal_summary", base["buying_signal_summary"]))[:500],
                "score_explanation": str(row.get("score_explanation", base["score_explanation"]))[:500],
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


async def _search_candidates(
    request: NameLeadSearchRequest,
    *,
    run_id: str = "",
) -> tuple[list[dict], str, list[str]]:
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
    if run_id:
        _update_search_progress(run_id, provider=provider)
    semaphore = asyncio.Semaphore(3)

    async def bounded(query: str):
        async with semaphore:
            try:
                result_query, results = await _search_one(tool, query, request)
                if run_id:
                    _update_search_progress(run_id, queries_completed=1, raw_result_count=len(results))
                return result_query, results
            except Exception as exc:
                logger.warning("[NameLeadHunter] Search failed for %r: %s", query, exc)
                if run_id:
                    _update_search_progress(run_id, queries_completed=1, error_count=1)
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
    official_candidates, official_status = await search_official_meta_sources(
        settings,
        include_instagram=request.include_instagram,
        include_facebook=request.include_facebook,
        max_results=request.max_results_per_query,
    )
    for candidate in official_candidates:
        url = _canonical_url(candidate.get("url", ""))
        if not url or url in deduped:
            continue
        candidate["url"] = url
        candidate["id"] = _candidate_id(url)
        deduped[url] = candidate
        if len(deduped) >= request.max_candidates:
            break
    # Record per-source adapter status in the returned provider label for the run.
    provider = provider + " + official Meta APIs: " + ", ".join(
        f"{name}={meta.get('mode')}" for name, meta in official_status.items()
    )
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
                    reply_draft, need_summary, buying_signal_summary, need_clarity_score,
                    purchase_stage_score, urgency_score, service_fit_score, contactability_score,
                    score_explanation, first_seen_at, last_seen_at, search_count, status,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, 'new', ?, ?)
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
                    need_summary=excluded.need_summary,
                    buying_signal_summary=excluded.buying_signal_summary,
                    need_clarity_score=excluded.need_clarity_score,
                    purchase_stage_score=excluded.purchase_stage_score,
                    urgency_score=excluded.urgency_score,
                    service_fit_score=excluded.service_fit_score,
                    contactability_score=excluded.contactability_score,
                    score_explanation=excluded.score_explanation,
                    last_seen_at=excluded.last_seen_at,
                    search_count=name_intent_leads.search_count+1,
                    updated_at=excluded.updated_at
                """,
                (
                    lead["id"], lead["title"], lead["url"], lead["snippet"],
                    lead["source_platform"], lead["source_domain"], lead["search_query"],
                    lead["intent_type"], lead["intent_score"], lead["purchase_signal"],
                    lead["reason"], lead["evidence_quote"], lead["reply_draft"],
                    lead.get("need_summary", ""), lead.get("buying_signal_summary", ""),
                    lead.get("need_clarity_score", 0), lead.get("purchase_stage_score", 0),
                    lead.get("urgency_score", 0), lead.get("service_fit_score", 0),
                    lead.get("contactability_score", 0), lead.get("score_explanation", ""),
                    now, now, now, now,
                ),
            )
            inserted += max(cursor.rowcount, 0)
    return inserted


def _row_to_dict(row: sqlite3.Row) -> dict:
    return dict(row)


@router.post("/name-leads/search", dependencies=[Depends(require_api_access)])
async def search_name_leads(request: NameLeadSearchRequest):
    """Search public posts, track progress, and rank leads with explainable dimensions."""
    queries = _build_queries(request)
    run_id = uuid.uuid4().hex[:16]
    _create_search_run(run_id, len(queries))
    try:
        candidates, provider, queries = await _search_candidates(request, run_id=run_id)
        classifications: list[dict] = []
        for start in range(0, len(candidates), 12):
            classifications.extend(await _classify_batch(candidates[start:start + 12]))

        leads = [{**candidate, **classification, "status": "new"} for candidate, classification in zip(candidates, classifications)]
        _save_leads(leads)
        leads.sort(key=lambda item: item["intent_score"], reverse=True)
        direct_requests = sum(1 for item in leads if item["intent_type"] == "direct_request")
        high_intent = sum(1 for item in leads if item["intent_score"] >= 70)
        platform_counts: dict[str, int] = {}
        for item in leads:
            platform = str(item.get("source_platform", "Web"))
            platform_counts[platform] = platform_counts.get(platform, 0) + 1
        _finish_search_run(
            run_id,
            status="completed",
            candidate_count=len(candidates),
            direct_requests=direct_requests,
            high_intent_count=high_intent,
            platform_counts=platform_counts,
        )
        return {
            "run_id": run_id,
            "provider": provider,
            "queries_run": queries,
            "candidate_count": len(candidates),
            "direct_requests": direct_requests,
            "high_intent_count": high_intent,
            "platform_counts": platform_counts,
            "leads": leads,
            "notice": "只分析公开帖子。意向评分是文本证据匹配度，不代表真实付费意愿；回复须人工审核。",
        }
    except Exception as exc:
        _finish_search_run(run_id, status="failed", error_summary=str(exc))
        raise


@router.get("/name-leads/dashboard", dependencies=[Depends(require_api_access)])
async def get_name_lead_dashboard():
    """Return funnel metrics, platform distribution, and active search progress."""
    today = datetime.now(timezone.utc).date().isoformat()
    with _connect() as conn:
        runs = conn.execute(
            "SELECT * FROM name_lead_search_runs ORDER BY started_at DESC LIMIT 8"
        ).fetchall()
        lead_rows = conn.execute(
            "SELECT * FROM name_intent_leads WHERE created_at >= ?",
            (today,),
        ).fetchall()
        all_rows = conn.execute("SELECT * FROM name_intent_leads").fetchall()
        active = conn.execute(
            "SELECT * FROM name_lead_search_runs WHERE status='running' ORDER BY started_at DESC LIMIT 1"
        ).fetchone()
        email_stats = conn.execute(
            """
            SELECT status, COUNT(*) AS total
            FROM name_lead_email_followups
            GROUP BY status
            """
        ).fetchall() if conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='name_lead_email_followups'"
        ).fetchone() else []
    todays = [dict(row) for row in lead_rows]
    all_leads = [dict(row) for row in all_rows]
    today_runs = [dict(row) for row in runs if str(row["started_at"]).startswith(today)]
    analyzed_today = sum(int(row.get("candidate_count", 0)) for row in today_runs)
    related = sum(1 for row in all_leads if row.get("intent_type") == "direct_request")
    high_intent = sum(1 for row in all_leads if int(row.get("intent_score", 0)) >= 70)
    contacted = sum(1 for row in all_leads if row.get("status") in {"replied", "contacted", "email_sent", "converted"})
    platform_counts: dict[str, int] = {}
    for row in all_leads:
        platform = str(row.get("source_platform", "Web"))
        platform_counts[platform] = platform_counts.get(platform, 0) + 1
    funnel = {
        "analyzed": analyzed_today,
        "related": related,
        "purchase_intent": sum(1 for row in all_leads if row.get("purchase_signal") in {"possible", "explicit"}),
        "worth_contacting": high_intent,
        "contacted": contacted,
    }
    scores = [int(row.get("intent_score", 0)) for row in todays]
    return {
        "today_new_leads": len(todays),
        "today_analyzed": analyzed_today,
        "today_direct_requests": sum(1 for row in todays if row.get("intent_type") == "direct_request"),
        "today_worth_contacting": sum(1 for row in todays if int(row.get("intent_score", 0)) >= 70),
        "average_intent_score": round(sum(scores) / len(scores)) if scores else 0,
        "total_leads": len(all_leads),
        "funnel": funnel,
        "platform_counts": platform_counts,
        "email_status_counts": {str(row["status"]): int(row["total"]) for row in email_stats},
        "active_run": dict(active) if active else None,
        "recent_runs": [dict(row) for row in runs],
    }


@router.get("/name-leads/search-runs", dependencies=[Depends(require_api_access)])
async def list_name_lead_search_runs(limit: int = Query(default=20, ge=1, le=100)):
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM name_lead_search_runs ORDER BY started_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return {"runs": [dict(row) for row in rows], "count": len(rows)}


@router.get("/name-leads", dependencies=[Depends(require_api_access)])
async def list_name_leads(
    status: str | None = Query(default=None),
    min_score: int = Query(default=0, ge=0, le=100),
    platform: str | None = Query(default=None),
    intent_type: str | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=200),
):
    """List saved public-post leads with platform and intent filters."""
    allowed = {"new", "reviewed", "ready_to_contact", "contacted", "email_consented", "email_sent", "replied", "converted", "dismissed"}
    if status and status not in allowed:
        raise HTTPException(status_code=400, detail="Invalid lead status.")
    if intent_type and intent_type not in {"direct_request", "research", "irrelevant"}:
        raise HTTPException(status_code=400, detail="Invalid intent_type.")
    sql = "SELECT * FROM name_intent_leads WHERE intent_score >= ?"
    params: list[object] = [min_score]
    if status:
        sql += " AND status = ?"
        params.append(status)
    if platform:
        sql += " AND source_platform = ?"
        params.append(platform)
    if intent_type:
        sql += " AND intent_type = ?"
        params.append(intent_type)
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


@router.get("/name-leads/revenue", dependencies=[Depends(require_api_access)])
async def get_name_lead_revenue():
    """Revenue and funnel metrics attributed to the source platform."""
    db_path = _database_path()
    with sqlite3.connect(str(db_path), timeout=20) as conn:
        conn.row_factory = sqlite3.Row
        has_orders = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='name_lead_orders'"
        ).fetchone()
        if not has_orders:
            return {"platforms": [], "total_orders": 0, "total_revenue": 0.0}
        rows = conn.execute(
            """
            SELECT source, currency, COUNT(*) AS orders, SUM(amount) AS revenue,
                   COUNT(DISTINCT email_hash) AS customers
            FROM name_lead_orders
            GROUP BY source, currency
            ORDER BY revenue DESC
            """
        ).fetchall()
        total = conn.execute(
            "SELECT COUNT(*) AS orders, COALESCE(SUM(amount),0) AS revenue FROM name_lead_orders"
        ).fetchone()
    platforms = [dict(row) for row in rows]
    return {
        "platforms": platforms,
        "total_orders": int(total["orders"]),
        "total_revenue": float(total["revenue"]),
        "note": "Revenue is grouped by currency; do not sum different currencies without conversion.",
    }
