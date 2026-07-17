"""User profile (height, weight, age, sex, HR anchors).

Stored in the AppSetting key/value table under the "profile." prefix, so no
schema migration is needed. Weight is normally mirrored from Google Health; the
other fields are entered by hand. The HR anchors matter a lot: the TRIMP load
model is built on the rest-HR/max-HR range, so a wrong range skews every CTL/ATL
value.
"""
from datetime import date

from .db import get_setting, set_setting

NUMERIC_FIELDS = ("height_cm", "weight_kg", "birth_year", "rest_hr", "max_hr")
# sex + ai_notes are free text, handled apart from the numeric fields
AI_NOTES_MAX = 1500  # cap so the note can't blow up every prompt

# Banister TRIMP coefficients (the exponential weighting differs by sex)
TRIMP_COEFF = {"F": (0.86, 1.67), "M": (0.64, 1.92)}


def _num(s: str) -> float | None:
    try:
        return float(s) if str(s).strip() != "" else None
    except (TypeError, ValueError):
        return None


def load() -> dict:
    """Profile as entered by the user (missing numeric fields are None)."""
    p = {k: _num(get_setting(f"profile.{k}", "")) for k in NUMERIC_FIELDS}
    p["sex"] = get_setting("profile.sex", "") or None
    p["ai_notes"] = get_setting("profile.ai_notes", "")
    return p


def save(values: dict) -> None:
    for k in NUMERIC_FIELDS:
        if k in values:
            v = values[k]
            set_setting(f"profile.{k}", "" if v is None else str(v).strip())
    if "sex" in values:
        set_setting("profile.sex", (values["sex"] or "").strip())
    if "ai_notes" in values:
        set_setting("profile.ai_notes", (values["ai_notes"] or "").strip()[:AI_NOTES_MAX])


def age(p: dict | None = None) -> int | None:
    p = p if p is not None else load()
    y = p.get("birth_year")
    return int(date.today().year - y) if y and 1900 < y <= date.today().year else None


def bmi(p: dict | None = None) -> float | None:
    p = p if p is not None else load()
    h, w = p.get("height_cm"), p.get("weight_kg")
    return round(w / (h / 100) ** 2, 1) if h and w else None


def hr_anchors(p: dict | None = None, measured_max: float | None = None) -> tuple[float, float, str]:
    """(rest_hr, max_hr, sex) used by the load model, with sane fallbacks.

    max HR: an explicit profile value wins. Otherwise take the HIGHER of the
    highest ever measured and Tanaka (208-0.7*age): the measured peak is only a
    lower bound (you may never have gone all out), while Tanaka is a population
    estimate that athletes routinely exceed. Falls back to 190 with neither.
    """
    p = p if p is not None else load()
    rest = p.get("rest_hr") or 55.0
    mx = p.get("max_hr")
    if not mx:
        a = age(p)
        tanaka = 208 - 0.7 * a if a else None
        mx = max([v for v in (measured_max, tanaka) if v] or [190.0])
    if mx <= rest:            # nonsense input: fall back rather than divide by ~0
        rest, mx = 55.0, 190.0
    sex = p.get("sex") if p.get("sex") in TRIMP_COEFF else "M"
    return float(rest), float(mx), sex


def ai_context(p: dict | None = None) -> dict:
    """Compact profile for the AI prompts (only the fields actually filled in)."""
    p = p if p is not None else load()
    out = {}
    if (a := age(p)):
        out["eta"] = a
    if p.get("sex"):
        out["sesso"] = "donna" if p["sex"] == "F" else "uomo"
    if p.get("height_cm"):
        out["altezza_cm"] = round(p["height_cm"])
    if p.get("weight_kg"):
        out["peso_kg"] = round(p["weight_kg"], 1)
    if (b := bmi(p)):
        out["bmi"] = b
    if p.get("rest_hr"):
        out["fc_riposo"] = round(p["rest_hr"])
    if p.get("max_hr"):
        out["fc_max"] = round(p["max_hr"])
    # Free-text memory: injuries, equipment, availability, goals, preferences.
    # Placed last and labelled so the AI treats it as durable context to respect.
    if p.get("ai_notes"):
        out["note_da_rispettare"] = p["ai_notes"]
    return out
