import re
import sqlite3
from types import SimpleNamespace

import pytest

from api import public_name_funnel_routes as public_routes
from api import name_lead_routes as lead_routes


@pytest.mark.asyncio
async def test_public_double_opt_in_creates_consent_and_email_followup(tmp_path, monkeypatch):
    db_path = tmp_path / "name_leads.sqlite3"
    monkeypatch.setenv("NAME_LEAD_DB_PATH", str(db_path))
    monkeypatch.setattr(public_routes, "_database_path", lambda: db_path)
    monkeypatch.setattr(lead_routes, "_database_path", lambda: db_path)
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

    # Simulate a trusted payment-provider webhook after a successful checkout.
    order = await public_routes.record_public_order(
        public_routes.PublicOrderEvent(
            order_id="test-order-001",
            email="buyer@example.com",
            amount=19.90,
            currency="USD",
            product="Chinese Name Starter",
        ),
        x_order_webhook_secret="test-secret",
    )
    assert order["status"] == "recorded"
    assert order["source"] == "instagram"

    with sqlite3.connect(db_path) as conn:
        saved_order = conn.execute(
            "SELECT source, amount, currency FROM name_lead_orders WHERE order_id=?",
            ("test-order-001",),
        ).fetchone()
    assert saved_order == ("instagram", 19.9, "USD")


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


@pytest.mark.asyncio
async def test_order_webhook_rejects_invalid_secret(tmp_path, monkeypatch):
    db_path = tmp_path / "name_leads.sqlite3"
    monkeypatch.setattr(public_routes, "_database_path", lambda: db_path)
    monkeypatch.setattr(lead_routes, "_database_path", lambda: db_path)
    monkeypatch.setattr(
        public_routes,
        "get_settings",
        lambda: SimpleNamespace(name_lead_order_webhook_secret="correct-secret"),
    )
    with pytest.raises(Exception):
        await public_routes.record_public_order(
            public_routes.PublicOrderEvent(
                order_id="test-order-002",
                email="buyer@example.com",
                amount=19.90,
                currency="USD",
            ),
            x_order_webhook_secret="wrong-secret",
        )
