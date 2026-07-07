"""Read-only integration with planmydinner (separate app) to bring nutrition
context — plan adherence, meals eaten/free, profile preferences — into the
health AI analysis and chat. Its API is open on the LAN; we only read.

No macros/calories are available in planmydinner, so the correlation is
qualitative (adherence, free meals, meal composition), not an energy balance.
"""
import logging
from datetime import date

import httpx

from .config import settings

logger = logging.getLogger(__name__)


def is_configured() -> bool:
    return bool(settings.planmydinner_url)


async def _get(client: httpx.AsyncClient, base: str, path: str, params: dict):
    try:
        resp = await client.get(base + path, params=params)
        return resp.json() if resp.status_code == 200 else None
    except (httpx.HTTPError, ValueError):
        return None


async def fetch_nutrition(start: date, end: date) -> dict | None:
    """Compact nutrition view for the window, or None when planmydinner isn't
    configured / has no data for the period (so the AI simply omits it)."""
    if not is_configured():
        return None
    base = settings.planmydinner_url.rstrip("/")
    prof = settings.planmydinner_profile
    days = (end - start).days + 1

    async with httpx.AsyncClient(timeout=15) as client:
        adherence = await _get(client, base, "/planner/adherence",
                               {"profile_id_A": prof, "start_date": start.isoformat(), "days": days})
        consumed = await _get(client, base, "/consumed-entries/",
                              {"profile_id": prof, "start_date": start.isoformat(),
                               "end_date": end.isoformat()})
        profile = await _get(client, base, f"/profiles/{prof}", {})

    a = adherence or {}
    consumed = consumed if isinstance(consumed, list) else []
    has_data = bool(consumed) or bool(a.get("planned_slots") or a.get("free_meals")
                                      or a.get("in_plan_consumed"))
    if not has_data:
        return None

    out: dict = {"periodo": f"{start.isoformat()} → {end.isoformat()}"}
    if a:
        score = a.get("adherence_score") or 0
        out["aderenza_al_piano"] = {
            "punteggio_pct": round(score * 100) if score <= 1 else round(score),
            "pasti_pianificati": a.get("planned_slots"),
            "pasti_da_piano_consumati": a.get("in_plan_consumed"),
            "pasti_liberi_usati": a.get("free_meals"),
            "quota_pasti_liberi": a.get("free_meal_quota"),
            "pasti_saltati": a.get("not_eaten_slots"),
        }
    if consumed:
        out["pasti_registrati"] = len(consumed)
    if profile:
        prefs = {k: v for k in ("preferences", "allergies", "excluded_foods")
                 if (v := profile.get(k))}
        if prefs:
            out["profilo"] = prefs
    return out
