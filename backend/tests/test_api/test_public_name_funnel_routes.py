import re
import sqlite3
from types import SimpleNamespace

import pytest

from api import public_name_funnel_routes as public_routes


@pytest.mark.asyncio
async def test_public_double_opt_in_creates_consent_and_email_followup(tmp_path, monkeypatch):
    db_path = tmp_path / "name_leads.sqlite3"
    monkeypatch.setenv("NAME_LEAD_DB_PATH", str(db_path))
    monkeypatch.setattr(public_routes, "_resolve_file", lambda _name: str(db_path))
    monkeypatch.setattr(
        public_routes,
        "get_settings",
        lambda: SimpleNamespace(
            public_app_base_url="https://example.test",
            email_from_address="hello@example.test",
            email_smtp_last_test_at="2026-11-10T00:00:00+00:00",
            name_lead_order_webhook_secret="test-secret",
        ),
    )
    sent = {}

    def fake_send(_settings, *, to_address, subject, body_text):
        sent.update(to=to_address, subject=subject, body=body_text)

    monkeypatch.setattr(public_routes, "send_smtp_email", fake_send)
    response = await public_routes.submit_public_name_request(
        public_routes.PublicNameRequest(
            email="buyer@example.com",
            consent=True,
            source="instagram",
            referral_code="",
            preferred_style="wisdom and nature",
        )
    )
    assert response["status"] == "confirmation_sent"
    token_match = re.search(r"/name-request/verify\?token=([A-Za-z0-9_-]+)", sent["body"])
    assert token_match
    verified = await public_routes.verify_public_name_request(token_match.group(1))
    assert verified["status"] == "verified"

    with sqlite3.connect(db_path) as conn:
        consent = conn.execute(
            "SELECT verified_at, source FROM public_name_consents WHERE email_hash=?",
            (public_routes._hash("buyer@example.com"),),
        ).fetchone()
        lead = conn.execute(
            "SELECT status, source_platform FROM name_intent_leads"
        ).fetchone()
        followup = conn.execute(
            "SELECT email, consent_confirmed, status, step FROM name_lead_email_followups"
        ).fetchone()

    assert consent and consent[0] and consent[1] == "instagram"
    assert lead == ("email_consented", "Instagram")
    assert followup == ("buyer@example.com", 1, "consented", 0)


@pytest.mark.asyncio
async def test_public_name_request_requires_explicit_consent():
    with pytest.raises(Exception):
        await public_routes.submit_public_name_request(
            public_routes.PublicNameRequest(
                email="buyer@example.com",
                consent=False,
                source="website",
            )
        )
