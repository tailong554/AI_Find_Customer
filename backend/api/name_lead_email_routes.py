"""Consent-based email follow-up for Chinese-name service leads.

Only emails recorded with explicit user consent can enter this workflow.
The first email is operator-triggered; later steps are opt-in scheduled follow-ups.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import uuid
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from api.name_lead_routes import _connect as _lead_connect, _now
from api.security import require_api_access
from config.settings import get_settings
from emailing.imap_client import search_recent_replies
from emailing.readiness import ensure_smtp_tested
from emailing.smtp_client import send_smtp_email
from tools.llm_client import LLMTool

logger = logging.getLogger(__name__)
router = APIRouter()


class ConsentRequest(BaseModel):
    email: str = Field(min_length=5, max_length=254)
    consent_confirmed: bool
    consent_source: str = Field(min_length=3, max_length=200)
    consent_evidence: str = Field(min_length=8, max_length=2000)


class FollowupStatusRequest(BaseModel):
    status: Literal["paused", "unsubscribed"]


def _init_tables() -> None:
    with _lead_connect() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS name_lead_email_followups (
                lead_id TEXT PRIMARY KEY,
                email TEXT NOT NULL,
                consent_confirmed INTEGER NOT NULL DEFAULT 0,
                consent_source TEXT NOT NULL DEFAULT '',
                consent_evidence TEXT NOT NULL DEFAULT '',
                consented_at TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'consented',
                step INTEGER NOT NULL DEFAULT 0,
                next_send_at TEXT NOT NULL DEFAULT '',
                last_sent_at TEXT NOT NULL DEFAULT '',
                last_reply_at TEXT NOT NULL DEFAULT '',
                last_reply_subject TEXT NOT NULL DEFAULT '',
                last_error TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS name_lead_email_events (
                id TEXT PRIMARY KEY,
                lead_id TEXT NOT NULL,
                step INTEGER NOT NULL,
                recipient_email TEXT NOT NULL,
                subject TEXT NOT NULL,
                body_text TEXT NOT NULL,
                status TEXT NOT NULL,
                sent_at TEXT NOT NULL DEFAULT '',
                error TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL
            )
            """
        )
        conn.commit()


def _get_followup(lead_id: str) -> dict[str, Any] | None:
    _init_tables()
    with _lead_connect() as conn:
        row = conn.execute(
            "SELECT * FROM name_lead_email_followups WHERE lead_id = ?",
            (lead_id,),
        ).fetchone()
    return dict(row) if row else None


def _get_lead(lead_id: str) -> dict[str, Any]:
    with _lead_connect() as conn:
        row = conn.execute(
            "SELECT * FROM name_intent_leads WHERE id = ?",
            (lead_id,),
        ).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Name lead not found.")
    return dict(row)


def _valid_email(value: str) -> bool:
    pattern = r"[A-Za-z0-9.!#$%&'*+/=?^_{|}~-]+@[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)+"
    return bool(re.fullmatch(pattern, value.strip()))


def _extract_json(raw: str) -> dict[str, Any]:
    text = (raw or "").strip()
    try:
        value = json.loads(text)
        if isinstance(value, dict):
            return value
    except json.JSONDecodeError:
        pass
    match = re.search(r"\{.*\}", text, flags=re.DOTALL)
    if not match:
        raise ValueError("Email model did not return JSON.")
    value = json.loads(match.group(0))
    if not isinstance(value, dict):
        raise ValueError("Email model response must be a JSON object.")
    return value


async def _draft_email(lead: dict[str, Any], email_address: str, step: int) -> tuple[str, str]:
    fallback = {
        1: (
            "Your Chinese name request",
            "Hi there,\n\nThanks for sharing your interest in choosing a Chinese name. "
            "A good name should sound natural and carry a meaning that feels right to you. "
            "If you reply with your preferred meaning, style, or pronunciation, I can prepare a few thoughtful options "
            "and explain the meaning of each character.\n\nIf you no longer want these emails, reply STOP.\n\nBest,\nChinese Name Studio",
        ),
        2: (
            "A quick follow-up on your Chinese name",
            "Hi there,\n\nI’m following up on your request for a Chinese name. If you’re still interested, "
            "just reply with a meaning or style you like (for example: wisdom, kindness, strength, or nature), "
            "and I can help you explore suitable options.\n\nIf you no longer want these emails, reply STOP.\n\n"
            "Best,\nChinese Name Studio",
        ),
        3: (
            "Closing the loop on your Chinese name",
            "Hi there,\n\nThis is my last follow-up about your Chinese name request. If you would still like help, "
            "you can reply whenever convenient. Otherwise, no action is needed and I won’t send more follow-ups.\n\n"
            "If you no longer want these emails, reply STOP.\n\nBest,\nChinese Name Studio",
        ),
    }
    settings = get_settings()
    try:
        llm = LLMTool(agent="name_lead_email")
        prompt = {
            "recipient_email": email_address,
            "source_post_title": lead.get("title", ""),
            "source_post_excerpt": lead.get("snippet", ""),
            "step": step,
            "service": (
                "A personalized Chinese-name service that provides three name candidates, pinyin, "
                "character-by-character meanings, overall meaning, and a short English explanation."
            ),
            "constraints": [
                "Write a natural, concise English email, not a cold sales blast.",
                "The recipient previously asked for help choosing a Chinese name and voluntarily submitted this email address with consent.",
                "Do not claim the person has purchased anything.",
                "Do not invent personal details.",
                "Do not include attachments, URLs, fake urgency, discounts, or exaggerated claims.",
                "Include a clear sentence that they can reply STOP to end follow-up emails.",
                "Return JSON with subject and body_text only.",
            ],
        }
        raw = await llm.generate(
            json.dumps(prompt, ensure_ascii=False),
            system=(
                "You write respectful one-to-one customer service emails for a personalized Chinese-name service. "
                "Do not use manipulative sales language. Return valid JSON only: "
                '{"subject":"...","body_text":"..."}'
            ),
            temperature=0.4,
            max_tokens=700,
            response_format={"type": "json_object"},
        )
        result = _extract_json(raw)
        subject = str(result.get("subject", "")).strip()[:180]
        body = str(result.get("body_text", "")).strip()[:5000]
        if subject and body:
            if "reply stop" not in body.lower():
                body += "\n\nIf you no longer want these emails, reply STOP."
            return subject, body
    except Exception:
        logger.exception("[NameLeadEmail] Could not generate email with configured LLM; using safe template.")
    return fallback[step]


def _record_event(
    lead_id: str,
    step: int,
    email_address: str,
    subject: str,
    body: str,
    status: str,
    *,
    sent_at: str = "",
    error: str = "",
) -> None:
    _init_tables()
    with _lead_connect() as conn:
        conn.execute(
            """
            INSERT INTO name_lead_email_events
            (id, lead_id, step, recipient_email, subject, body_text, status, sent_at, error, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (str(uuid.uuid4()), lead_id, step, email_address, subject, body, status, sent_at, error, _now()),
        )
        conn.commit()


async def _send_step(lead_id: str, *, initial: bool = False) -> dict[str, Any]:
    settings = get_settings()
    try:
        ensure_smtp_tested(settings)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    lead = _get_lead(lead_id)
    _init_tables()
    with _lead_connect() as conn:
        row = conn.execute(
            "SELECT * FROM name_lead_email_followups WHERE lead_id = ?",
            (lead_id,),
        ).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="No consented email is recorded for this lead.")
        followup = dict(row)
        if not followup["consent_confirmed"]:
            raise HTTPException(status_code=403, detail="Explicit email consent is required.")
        if followup["status"] in {"replied", "unsubscribed", "completed", "sending"}:
            raise HTTPException(status_code=409, detail=f"Follow-up is {followup['status']}.")
        if initial and int(followup["step"]) > 0:
            raise HTTPException(status_code=409, detail="The first email has already been sent.")
        if not initial and followup["status"] != "active":
            raise HTTPException(status_code=409, detail="Only active follow-ups can send scheduled messages.")
        previous_status = str(followup["status"])
        cursor = conn.execute(
            "UPDATE name_lead_email_followups SET status = 'sending', updated_at = ? WHERE lead_id = ? AND status = ?",
            (_now(), lead_id, previous_status),
        )
        if cursor.rowcount != 1:
            raise HTTPException(status_code=409, detail="Follow-up state changed; refresh and try again.")
        conn.commit()

    step = int(followup["step"]) + 1
    if step > 3:
        with _lead_connect() as conn:
            conn.execute(
                "UPDATE name_lead_email_followups SET status='completed', next_send_at='', updated_at=? WHERE lead_id=?",
                (_now(), lead_id),
            )
            conn.commit()
        return _get_followup(lead_id) or {}

    subject, body = await _draft_email(lead, str(followup["email"]), step)
    if not settings.email_from_address:
        with _lead_connect() as conn:
            conn.execute(
                "UPDATE name_lead_email_followups SET status=?, updated_at=? WHERE lead_id=?",
                (previous_status, _now(), lead_id),
            )
            conn.commit()
        raise HTTPException(status_code=409, detail="EMAIL_FROM_ADDRESS is not configured.")

    try:
        await asyncio.to_thread(
            send_smtp_email,
            settings,
            to_address=str(followup["email"]),
            subject=subject,
            body_text=body,
        )
        sent_at = _now()
        _record_event(lead_id, step, str(followup["email"]), subject, body, "sent", sent_at=sent_at)
        if step == 1:
            next_days = max(1, min(30, int(settings.name_lead_email_first_followup_days or 3)))
            next_send_at = (datetime.now(timezone.utc) + timedelta(days=next_days)).isoformat()
            new_status = "active"
        elif step == 2:
            next_days = max(1, min(30, int(settings.name_lead_email_second_followup_days or 4)))
            next_send_at = (datetime.now(timezone.utc) + timedelta(days=next_days)).isoformat()
            new_status = "active"
        else:
            next_send_at = ""
            new_status = "completed"
        with _lead_connect() as conn:
            conn.execute(
                """
                UPDATE name_lead_email_followups
                SET status=?, step=?, next_send_at=?, last_sent_at=?, last_error='', updated_at=?
                WHERE lead_id=?
                """,
                (new_status, step, next_send_at, sent_at, sent_at, lead_id),
            )
            conn.commit()
        return _get_followup(lead_id) or {}
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("[NameLeadEmail] SMTP send failed for lead %s", lead_id)
        _record_event(lead_id, step, str(followup["email"]), subject, body, "failed", error=str(exc)[:1000])
        with _lead_connect() as conn:
            conn.execute(
                "UPDATE name_lead_email_followups SET status=?, last_error=?, updated_at=? WHERE lead_id=?",
                (previous_status, str(exc)[:1000], _now(), lead_id),
            )
            conn.commit()
        raise HTTPException(status_code=502, detail=f"SMTP send failed: {exc}") from exc


def _reply_is_after_sent(reply_date: str, sent_at: str) -> bool:
    try:
        received = parsedate_to_datetime(reply_date)
        if received.tzinfo is None:
            received = received.replace(tzinfo=timezone.utc)
        sent = datetime.fromisoformat(sent_at.replace("Z", "+00:00"))
        if sent.tzinfo is None:
            sent = sent.replace(tzinfo=timezone.utc)
        return received.astimezone(timezone.utc) >= sent.astimezone(timezone.utc)
    except Exception:
        return False


async def sync_name_lead_replies() -> dict[str, int]:
    settings = get_settings()
    if not settings.email_imap_last_test_at:
        return {"checked": 0, "replied": 0, "unsubscribed": 0}
    _init_tables()
    with _lead_connect() as conn:
        rows = conn.execute(
            "SELECT * FROM name_lead_email_followups WHERE status='active' AND last_sent_at <> ''"
        ).fetchall()
    checked = replied = unsubscribed = 0
    for raw in rows:
        followup = dict(raw)
        checked += 1
        try:
            messages = await asyncio.to_thread(
                search_recent_replies,
                settings,
                from_address=str(followup["email"]),
                limit=10,
            )
        except Exception:
            logger.exception("[NameLeadEmail] IMAP check failed for %s", followup["email"])
            continue
        matching = [
            msg for msg in messages
            if _reply_is_after_sent(str(msg.get("date", "")), str(followup["last_sent_at"]))
        ]
        if not matching:
            continue
        latest = matching[0]
        subject = str(latest.get("subject", "") or "")
        body = str(latest.get("body_text", "") or "")
        optout_text = f"{subject}\n{body}".strip().lower()
        is_optout = bool(re.match(r"^\s*(stop|unsubscribe|remove|opt out)\b", optout_text))
        new_status = "unsubscribed" if is_optout else "replied"
        with _lead_connect() as conn:
            conn.execute(
                """
                UPDATE name_lead_email_followups
                SET status=?, next_send_at='', last_reply_at=?, last_reply_subject=?, updated_at=?
                WHERE lead_id=? AND status='active'
                """,
                (new_status, _now(), subject[:300], _now(), followup["lead_id"]),
            )
            conn.commit()
        if is_optout:
            unsubscribed += 1
        else:
            replied += 1
    return {"checked": checked, "replied": replied, "unsubscribed": unsubscribed}


async def run_due_name_lead_followups() -> dict[str, int]:
    settings = get_settings()
    if not settings.name_lead_email_auto_followup_enabled:
        return {"sent": 0, "failed": 0, "skipped": 0}
    try:
        ensure_smtp_tested(settings)
    except ValueError:
        return {"sent": 0, "failed": 0, "skipped": 0}
    _init_tables()
    now = _now()
    with _lead_connect() as conn:
        rows = conn.execute(
            """
            SELECT lead_id FROM name_lead_email_followups
            WHERE status='active' AND consent_confirmed=1 AND step IN (1, 2)
              AND next_send_at <> '' AND next_send_at <= ?
            ORDER BY next_send_at ASC LIMIT 5
            """,
            (now,),
        ).fetchall()
    sent = failed = skipped = 0
    for row in rows:
        lead_id = str(row["lead_id"])
        try:
            if settings.name_lead_email_reply_detection_enabled and settings.email_imap_last_test_at:
                await sync_name_lead_replies()
            followup = _get_followup(lead_id)
            if not followup or followup["status"] != "active":
                skipped += 1
                continue
            await _send_step(lead_id, initial=False)
            sent += 1
        except HTTPException:
            skipped += 1
        except Exception:
            logger.exception("[NameLeadEmail] Scheduled follow-up failed for %s", lead_id)
            failed += 1
    return {"sent": sent, "failed": failed, "skipped": skipped}


@router.post("/name-leads/{lead_id}/email-consent", dependencies=[Depends(require_api_access)])
async def record_name_lead_email_consent(lead_id: str, request: ConsentRequest):
    _get_lead(lead_id)
    email_address = request.email.strip().lower()
    if not _valid_email(email_address):
        raise HTTPException(status_code=422, detail="Please provide a valid email address.")
    if not request.consent_confirmed:
        raise HTTPException(status_code=403, detail="The user must explicitly consent to email follow-up.")
    now = _now()
    _init_tables()
    existing = _get_followup(lead_id)
    if existing and int(existing.get("step", 0)) > 0:
        raise HTTPException(status_code=409, detail="Email follow-up has already started; do not replace its recipient.")
    with _lead_connect() as conn:
        conn.execute(
            """
            INSERT INTO name_lead_email_followups
            (lead_id, email, consent_confirmed, consent_source, consent_evidence, consented_at,
             status, step, next_send_at, last_sent_at, last_reply_at, last_reply_subject,
             last_error, created_at, updated_at)
            VALUES (?, ?, 1, ?, ?, ?, 'consented', 0, '', '', '', '', '', ?, ?)
            ON CONFLICT(lead_id) DO UPDATE SET
              email=excluded.email, consent_confirmed=1, consent_source=excluded.consent_source,
              consent_evidence=excluded.consent_evidence, consented_at=excluded.consented_at,
              status='consented', next_send_at='', last_error='', updated_at=excluded.updated_at
            """,
            (lead_id, email_address, request.consent_source.strip(), request.consent_evidence.strip(), now, now, now),
        )
        conn.commit()
    return {"followup": _get_followup(lead_id)}


@router.get("/name-leads/{lead_id}/email-followup", dependencies=[Depends(require_api_access)])
async def get_name_lead_email_followup(lead_id: str):
    _get_lead(lead_id)
    return {"followup": _get_followup(lead_id)}


@router.post("/name-leads/{lead_id}/email-followup/start", dependencies=[Depends(require_api_access)])
async def start_name_lead_email_followup(lead_id: str):
    followup = _get_followup(lead_id)
    if not followup:
        raise HTTPException(status_code=404, detail="Record explicit user email consent first.")
    if int(followup.get("step", 0)) > 0 and followup.get("status") == "paused":
        with _lead_connect() as conn:
            conn.execute(
                "UPDATE name_lead_email_followups SET status='active', updated_at=? WHERE lead_id=? AND status='paused'",
                (_now(), lead_id),
            )
            conn.commit()
        return {"followup": _get_followup(lead_id), "message": "Follow-up resumed; no duplicate welcome email sent."}
    updated = await _send_step(lead_id, initial=True)
    return {"followup": updated, "message": "First consent-based email sent."}


@router.patch("/name-leads/{lead_id}/email-followup/status", dependencies=[Depends(require_api_access)])
async def update_name_lead_email_followup_status(lead_id: str, request: FollowupStatusRequest):
    followup = _get_followup(lead_id)
    if not followup:
        raise HTTPException(status_code=404, detail="Email follow-up record not found.")
    with _lead_connect() as conn:
        conn.execute(
            "UPDATE name_lead_email_followups SET status=?, next_send_at='', updated_at=? WHERE lead_id=?",
            (request.status, _now(), lead_id),
        )
        conn.commit()
    return {"followup": _get_followup(lead_id)}


async def name_lead_email_scheduler_loop() -> None:
    """Poll IMAP replies and send only consented, due follow-ups."""
    last_reply_check = 0.0
    while True:
        settings = get_settings()
        now = asyncio.get_running_loop().time()
        if settings.name_lead_email_reply_detection_enabled and settings.email_imap_last_test_at:
            interval = max(5, min(240, int(settings.name_lead_email_reply_check_interval_minutes or 15))) * 60
            if now - last_reply_check >= interval:
                try:
                    result = await sync_name_lead_replies()
                    logger.info("[NameLeadEmail] IMAP reply sync: %s", result)
                except Exception:
                    logger.exception("[NameLeadEmail] IMAP reply sync failed")
                last_reply_check = now
        if settings.name_lead_email_auto_followup_enabled:
            try:
                result = await run_due_name_lead_followups()
                if result["sent"] or result["failed"]:
                    logger.info("[NameLeadEmail] follow-up run: %s", result)
            except Exception:
                logger.exception("[NameLeadEmail] follow-up scheduler failed")
        await asyncio.sleep(60)
