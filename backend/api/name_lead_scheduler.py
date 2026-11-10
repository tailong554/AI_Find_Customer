"""Opt-in periodic search for public Chinese-name request posts."""

from __future__ import annotations

import asyncio
import logging

from api.name_lead_routes import NameLeadSearchRequest, search_name_leads
from config.settings import get_settings

logger = logging.getLogger(__name__)


async def name_lead_scheduler_loop() -> None:
    """Run a bounded public-post search at the configured interval."""
    while True:
        settings = get_settings()
        enabled = bool(settings.name_lead_auto_search_enabled)
        interval_hours = max(1, min(168, int(settings.name_lead_search_interval_hours or 6)))
        if enabled:
            try:
                result = await search_name_leads(
                    NameLeadSearchRequest(
                        country_code=settings.name_lead_country_code,
                        max_results_per_query=max(1, min(10, int(settings.name_lead_max_results_per_query or 6))),
                        max_candidates=max(1, min(80, int(settings.name_lead_max_candidates or 40))),
                        include_reddit=True,
                        include_other_forums=True,
                    )
                )
                logger.info(
                    "[NameLeadScheduler] scanned=%s direct_requests=%s high_intent=%s",
                    result.get("candidate_count", 0),
                    result.get("direct_requests", 0),
                    result.get("high_intent_count", 0),
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("[NameLeadScheduler] scheduled search failed")
        await asyncio.sleep(interval_hours * 3600)
