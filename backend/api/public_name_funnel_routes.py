"""Public double opt-in and order attribution endpoints."""
from __future__ import annotations

import asyncio
import hashlib
import re
import secrets
import sqlite3
from datetime import datetime, timedelta, timezone
from urllib.parse import quote

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel, Field

from api.name_lead_email_routes import _init_tables as _init_email_tables
from api.name_lead_routes import _now
from config.settings import _resolve_file, get_settings
from emailing.smtp_client import send_smtp_email

router = APIRouter()
CONSENT_TEXT = (
    "I agree to receive emails about my Chinese-name request and related service follow-up. "
    "I can unsubscribe at any time."
)


class PublicNameRequest(BaseModel):
    email: str = Field(min_length=5, max_length=254)
    consent: bool
    source: str = Field(default="website", max_length=80)
    referral_code: str = Field(default="", max_length=80)
    preferred_style: str = Field(default="", max_length=300)


class PublicOrderEvent(BaseModel):
    order_id: str = Field(min_length=2, max_length=160)
    email: str = Field(min_length=5, max_length=254)
    amount: float = Field(ge=0, le=1000000)
    currency: str = Field(default="USD", min_length=3, max_length=3)
    product: str = Field(default="Chinese name service", max_length=160)
    source: str = Field(default="", max_length=80)
    referral_code: str = Field(default="", max_length=80)


def _db():
    conn = sqlite3.connect(_resolve_file("name_leads.sqlite3"), timeout=20)
    conn.row_factory = sqlite3.Row
    conn.execute("""CREATE TABLE IF NOT EXISTS public_name_consents (
        id TEXT PRIMARY KEY, email TEXT NOT NULL, email_hash TEXT NOT NULL,
        consent INTEGER NOT NULL, consent_text TEXT NOT NULL, source TEXT NOT NULL,
        referral_code TEXT NOT NULL DEFAULT '', preferred_style TEXT NOT NULL DEFAULT '',
        verify_token_hash TEXT NOT NULL, verified_at TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
        expires_at TEXT NOT NULL DEFAULT '')""")
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(public_name_consents)")}
    if "expires_at" not in columns:
        conn.execute("ALTER TABLE public_name_consents ADD COLUMN expires_at TEXT NOT NULL DEFAULT ''")
    conn.execute("""CREATE TABLE IF NOT EXISTS name_lead_orders (
        order_id TEXT PRIMARY KEY, lead_id TEXT NOT NULL DEFAULT '', email_hash TEXT NOT NULL,
        source TEXT NOT NULL DEFAULT '', referral_code TEXT NOT NULL DEFAULT '',
        product TEXT NOT NULL, amount REAL NOT NULL, currency TEXT NOT NULL,
        ordered_at TEXT NOT NULL)""")
    conn.commit()
    return conn


def _hash(value: str) -> str:
    return hashlib.sha256(value.strip().lower().encode("utf-8")).hexdigest()


def _valid_email(value: str) -> bool:
    return bool(re.fullmatch(
        r"[A-Za-z0-9.!#$%&'*+/=?^_{|}~-]+@[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)+",
        value.strip(),
    ))


def _normalise_source(value: str) -> str:
    source = re.sub(r"[^a-z0-9_-]", "", (value or "website").strip().lower())[:40]
    return source or "website"


def _rate_limit(email_hash: str) -> None:
    now = datetime.now(timezone.utc)
    with _db() as conn:
        recent = conn.execute(
            "SELECT COUNT(*) AS n FROM public_name_consents WHERE email_hash=? AND created_at>=?",
            (email_hash, (now - timedelta(hours=24)).isoformat()),
        ).fetchone()["n"]
        if int(recent) >= 5:
            raise HTTPException(status_code=429, detail="Too many requests for this email. Try again later.")


@router.post("/public/name-request")
async def submit_public_name_request(request: PublicNameRequest):
    if not request.consent:
        raise HTTPException(status_code=403, detail="Consent is required.")
    email = request.email.strip().lower()
    if not _valid_email(email):
        raise HTTPException(status_code=422, detail="Invalid email.")
    settings = get_settings()
    base_url = settings.public_app_base_url.strip().rstrip("/")
    if not base_url.startswith("https://"):
        raise HTTPException(status_code=503, detail="PUBLIC_APP_BASE_URL must be an HTTPS URL.")
    if not settings.email_from_address or not settings.email_smtp_last_test_at:
        raise HTTPException(status_code=503, detail="Configure and test SMTP first.")
    email_hash = _hash(email)
    _rate_limit(email_hash)
    token = secrets.token_urlsafe(32)
    consent_id = secrets.token_hex(16)
    now = datetime.now(timezone.utc)
    expires = (now + timedelta(hours=24)).isoformat()
    source = _normalise_source(request.source)
    referral = request.referral_code.strip()[:80]
    with _db() as conn:
        conn.execute(
            """INSERT INTO public_name_consents
            (id,email,email_hash,consent,consent_text,source,referral_code,preferred_style,
             verify_token_hash,verified_at,created_at,updated_at,expires_at)
            VALUES (?,?,?,?,?,?,?,?,?,'',?,?,?)""",
            (consent_id, email, email_hash, 1, CONSENT_TEXT, source, referral,
             request.preferred_style.strip(), _hash(token), now.isoformat(), now.isoformat(), expires),
        )
        conn.commit()
    verify_url = base_url + "/name-request/verify?token=" + quote(token)
    try:
        await asyncio.to_thread(
            send_smtp_email,
            settings,
            to_address=email,
            subject="Confirm your Chinese name request",
            body_text=(
                "Please confirm your email and consent by opening this link:\n\n"
                + verify_url
                + "\n\nThis link expires in 24 hours. No follow-up emails will be sent until you confirm. "
                "If you did not submit this request, ignore this email."
            ),
        )
    except Exception as exc:
        with _db() as conn:
            conn.execute("DELETE FROM public_name_consents WHERE id=?", (consent_id,))
            conn.commit()
        raise HTTPException(status_code=502, detail="Could not send confirmation email.") from exc
    return {"status": "confirmation_sent", "message": "Check your inbox to confirm."}


@router.get("/public/name-request/verify")
async def verify_public_name_request(token: str):
    if not token or len(token) > 200:
        raise HTTPException(status_code=400, detail="Invalid token.")
    now = datetime.now(timezone.utc)
    with _db() as conn:
        row = conn.execute(
            """SELECT * FROM public_name_consents
            WHERE verify_token_hash=? AND verified_at='' AND expires_at>?""",
            (_hash(token), now.isoformat()),
        ).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Confirmation link is invalid, expired, or already used.")
        consent = dict(row)
        conn.execute(
            "UPDATE public_name_consents SET verified_at=?,updated_at=? WHERE id=?",
            (now.isoformat(), now.isoformat(), consent["id"]),
        )
        conn.commit()

    # Attach the verified consent to the originating social lead, or create a
    # website-origin lead when the form was reached without a known lead ID.
    from api.name_lead_routes import _connect as _lead_connect

    referral = str(consent["referral_code"] or "")
    lead_id = ""
    with _lead_connect() as conn:
        if referral:
            existing = conn.execute("SELECT id FROM name_intent_leads WHERE id=?", (referral,)).fetchone()
            if existing:
                lead_id = referral
        if not lead_id:
            lead_id = str(consent["id"])
            synthetic_url = f"https://lead.local/public-name-request/{lead_id}"
            conn.execute(
                """INSERT OR IGNORE INTO name_intent_leads
                (id,title,url,snippet,source_platform,source_domain,search_query,intent_type,
                 intent_score,purchase_signal,reason,evidence_quote,reply_draft,status,created_at,updated_at)
                VALUES (?,?,?,?,?,?,?,'direct_request',70,'possible',?,?,?,?,?,?)""",
                (
                    lead_id, "Verified Chinese name request", synthetic_url,
                    str(consent["preferred_style"] or "Customer submitted a Chinese-name request."),
                    str(consent["source"]).title(), "website", "Public website double opt-in",
                    "Email ownership and consent verified by double opt-in.",
                    "", "", "email_consented", now.isoformat(), now.isoformat(),
                ),
            )
        else:
            conn.execute(
                "UPDATE name_intent_leads SET status='email_consented',updated_at=? WHERE id=?",
                (now.isoformat(), lead_id),
            )
        conn.commit()

    _init_email_tables()
    with _lead_connect() as conn:
        conn.execute(
            """INSERT INTO name_lead_email_followups
            (lead_id,email,consent_confirmed,consent_source,consent_evidence,consented_at,
             status,step,next_send_at,last_sent_at,last_reply_at,last_reply_subject,last_error,
             created_at,updated_at)
            VALUES (?,?,1,?,?,?,?,0,'','','','','',?,?)
            ON CONFLICT(lead_id) DO UPDATE SET
              email=excluded.email,consent_confirmed=1,consent_source=excluded.consent_source,
              consent_evidence=excluded.consent_evidence,consented_at=excluded.consented_at,
              status=CASE WHEN name_lead_email_followups.step=0 THEN 'consented' ELSE name_lead_email_followups.status END,
              updated_at=excluded.updated_at""",
            (
                lead_id, consent["email"], consent["source"], CONSENT_TEXT,
                now.isoformat(), "consented", now.isoformat(), now.isoformat(),
            ),
        )
        conn.commit()
    return {"status": "verified", "message": "Email confirmed. Your request is now in the customer follow-up queue."}


@router.post("/public/orders")
async def record_public_order(event: PublicOrderEvent, x_order_webhook_secret: str = Header(default="")):
    settings = get_settings()
    expected = settings.name_lead_order_webhook_secret.strip()
    if not expected or not secrets.compare_digest(x_order_webhook_secret, expected):
        raise HTTPException(status_code=401, detail="Invalid order webhook secret.")
    email = event.email.strip().lower()
    if not _valid_email(email):
        raise HTTPException(status_code=422, detail="Invalid email.")
    with _db() as conn:
        consent = conn.execute(
            "SELECT id,source,referral_code FROM public_name_consents WHERE email_hash=? AND verified_at<>'' ORDER BY verified_at DESC LIMIT 1",
            (_hash(email),),
        ).fetchone()
        source = _normalise_source(event.source or (consent["source"] if consent else "direct"))
        referral = event.referral_code.strip() or (consent["referral_code"] if consent else "")
        lead_id = referral or (consent["id"] if consent else "")
        try:
            conn.execute(
                "INSERT INTO name_lead_orders VALUES (?,?,?,?,?,?,?,?,?)",
                (event.order_id, lead_id, _hash(email), source, referral, event.product,
                 event.amount, event.currency.upper(), _now()),
            )
        except sqlite3.IntegrityError as exc:
            raise HTTPException(status_code=409, detail="Duplicate order ID.") from exc
        conn.commit()
    return {"status": "recorded", "order_id": event.order_id, "source": source, "lead_id": lead_id}
