"""Public double opt-in and order attribution endpoints."""
from __future__ import annotations
import asyncio
import hashlib
import re
import secrets
import sqlite3
from urllib.parse import quote
from fastapi import APIRouter, HTTPException, Header
from pydantic import BaseModel, Field
from config.settings import _resolve_file, get_settings
from emailing.smtp_client import send_smtp_email
from api.name_lead_routes import _now

router = APIRouter()

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
        created_at TEXT NOT NULL, updated_at TEXT NOT NULL)""")
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
    return bool(re.fullmatch(r"[A-Za-z0-9.!#$%&'*+/=?^_{|}~-]+@[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)+", value.strip()))

@router.post("/public/name-request")
async def submit_public_name_request(request: PublicNameRequest):
    if not request.consent:
        raise HTTPException(status_code=403, detail="Consent is required.")
    email = request.email.strip().lower()
    if not _valid_email(email):
        raise HTTPException(status_code=422, detail="Invalid email.")
    settings = get_settings()
    base_url = settings.public_app_base_url.strip().rstrip("/")
    if not base_url or not settings.email_from_address or not settings.email_smtp_last_test_at:
        raise HTTPException(status_code=503, detail="Configure PUBLIC_APP_BASE_URL and test SMTP first.")
    token = secrets.token_urlsafe(32)
    consent_id = secrets.token_hex(16)
    now = _now()
    consent_text = "I agree to receive emails about my Chinese-name request and related service follow-up. I can unsubscribe at any time."
    with _db() as conn:
        conn.execute(
            "INSERT INTO public_name_consents VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (consent_id, email, _hash(email), 1, consent_text, request.source, request.referral_code,
             request.preferred_style, _hash(token), "", now, now),
        )
        conn.commit()
    verify_url = base_url + "/name-request/verify?token=" + quote(token)
    try:
        await asyncio.to_thread(
            send_smtp_email, settings, to_address=email,
            subject="Confirm your Chinese name request",
            body_text=("Please confirm your email and consent by opening this link:\n\n" + verify_url +
                       "\n\nNo follow-up emails will be sent until you confirm. If you did not request this, ignore this email."),
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
    now = _now()
    with _db() as conn:
        row = conn.execute("SELECT id FROM public_name_consents WHERE verify_token_hash=? AND verified_at=''", (_hash(token),)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Link invalid or already used.")
        conn.execute("UPDATE public_name_consents SET verified_at=?,updated_at=? WHERE id=?", (now, now, row["id"]))
        conn.commit()
    return {"status": "verified", "message": "Email confirmed. Thank you."}

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
        consent = conn.execute("SELECT id,source,referral_code FROM public_name_consents WHERE email_hash=? AND verified_at<>'' ORDER BY verified_at DESC LIMIT 1", (_hash(email),)).fetchone()
        source = event.source.strip() or (consent["source"] if consent else "direct")
        referral = event.referral_code.strip() or (consent["referral_code"] if consent else "")
        lead_id = referral or (consent["id"] if consent else "")
        try:
            conn.execute("INSERT INTO name_lead_orders VALUES (?,?,?,?,?,?,?,?,?)",
                (event.order_id, lead_id, _hash(email), source, referral, event.product, event.amount, event.currency.upper(), _now()))
        except sqlite3.IntegrityError as exc:
            raise HTTPException(status_code=409, detail="Duplicate order ID.") from exc
        conn.commit()
    return {"status": "recorded", "order_id": event.order_id, "source": source, "lead_id": lead_id}
