"""Read-only integration with planmydinner (separate app) to bring nutrition
context — plan adherence, energy/macros of tracked meals, preferences — into the
health AI analysis and chat. Its API is open on the LAN; we only read.

planmydinner exposes a purpose-built /integration/summary (versioned) with daily
and average kcal + macros and adherence. Calories are for TRACKED meals only
(coverage over planned slots), not necessarily the full daily intake, so they are
labelled as such and never treated as a complete energy balance. planmydinner has
no meal-quality index, so quality is left to the AI to judge from the macro split,
protein-per-kg and the in-plan vs free/mensa ratio.
"""
import logging
from datetime import date

import httpx

from . import profile as profilemod
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

    async with httpx.AsyncClient(timeout=15) as client:
        summary = await _get(client, base, "/integration/summary",
                             {"profile_id": prof, "start_date": start.isoformat(),
                              "end_date": end.isoformat()})
        profile = await _get(client, base, f"/profiles/{prof}", {})

    return _shape(summary, profile, start, end, profilemod.load().get("weight_kg"))


def _shape(summary, profile, start: date, end: date, weight_kg) -> dict | None:
    """Turn the raw /integration/summary (+ profile) into the compact Italian view
    the AI prompts consume, or None when there's no usable data."""
    summary = summary if isinstance(summary, dict) else {}
    adh = summary.get("adherence") or {}
    avg = summary.get("averages") or {}
    days = summary.get("days") or []
    if not (avg.get("days_with_data") or adh.get("planned_slots") or adh.get("free_meals")):
        return None

    out: dict = {"periodo": f"{start.isoformat()} → {end.isoformat()}"}

    if adh:
        score = adh.get("adherence_score") or 0
        out["aderenza_al_piano"] = {
            "punteggio_pct": round(score * 100) if score <= 1 else round(score),
            "pasti_pianificati": adh.get("planned_slots"),
            "pasti_da_piano_consumati": adh.get("in_plan_consumed"),
            "pasti_liberi_usati": adh.get("free_meals"),
            "quota_pasti_liberi": adh.get("free_meal_quota"),
            "pasti_saltati": adh.get("not_eaten_slots"),
        }

    if avg.get("days_with_data"):
        prot = avg.get("protein_g")
        macros = {
            "nota": "valori dei PASTI TRACCIATI (pasti pianificati), non "
                    "necessariamente l'intero introito giornaliero",
            "giorni_con_dati": avg.get("days_with_data"),
            "kcal_medie": _r(avg.get("kcal")),
            "proteine_g_medie": _r(prot),
            "carboidrati_g_medi": _r(avg.get("carbs_g")),
            "grassi_g_medi": _r(avg.get("fat_g")),
        }
        # protein per kg: the single most useful quality signal for an athlete
        if prot and weight_kg:
            macros["proteine_g_per_kg"] = round(prot / weight_kg, 2)
        # per-day energy/macros so the AI can see variability, not just the mean
        macros["per_giorno"] = [
            {"data": d.get("date"), "kcal": _r((d.get("nutrition") or {}).get("kcal")),
             "proteine_g": _r((d.get("nutrition") or {}).get("protein_g")),
             "carboidrati_g": _r((d.get("nutrition") or {}).get("carbs_g")),
             "grassi_g": _r((d.get("nutrition") or {}).get("fat_g")),
             "pasti_liberi": d.get("free_meals")}
            for d in days if (d.get("nutrition") or {}).get("kcal") is not None]
        out["alimentazione_tracciata"] = macros

    if profile:
        prefs = {k: v for k in ("preferences", "allergies", "excluded_foods")
                 if (v := profile.get(k))}
        if prefs:
            out["profilo"] = prefs
    return out


def _r(v, nd: int = 0):
    """Round a possibly-None number, keeping None as None."""
    try:
        return round(float(v), nd) if v is not None else None
    except (TypeError, ValueError):
        return None
