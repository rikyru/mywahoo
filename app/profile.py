"""User profile (height, weight, age, sex, HR anchors).

Stored in the AppSetting key/value table under the "profile." prefix, so no
schema migration is needed. Weight is normally mirrored from Google Health; the
other fields are entered by hand. The HR anchors matter a lot: the TRIMP load
model is built on the rest-HR/max-HR range, so a wrong range skews every CTL/ATL
value.
"""
from datetime import date

from sqlmodel import Session, select

from .db import BodyMeasure, engine, get_setting, set_setting

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


# Nutrition goal config (drives the kcal/macro targets pushed to planmydinner).
# Kept apart from the physical profile: these are preferences, not measurements.
NUTRITION_DEFAULTS = {"goal": "cut", "adjust_pct": 15.0, "activity_factor": 1.45,
                      "protein_per_kg_lean": 2.0, "tdee_basis": "estimate"}
NUTRITION_GOALS = ("cut", "maintain", "bulk")
# How to estimate daily burn: "estimate" = BMR × activity_factor (conservative,
# trackers overcount); "measured" = average Google calories_burned.
TDEE_BASES = ("estimate", "measured")


def nutrition_cfg() -> dict:
    """Goal + knobs for the meal-plan targets, with sensible defaults."""
    goal = get_setting("nutrition.goal", "") or NUTRITION_DEFAULTS["goal"]
    if goal not in NUTRITION_GOALS:
        goal = NUTRITION_DEFAULTS["goal"]
    basis = get_setting("nutrition.tdee_basis", "") or NUTRITION_DEFAULTS["tdee_basis"]
    if basis not in TDEE_BASES:
        basis = NUTRITION_DEFAULTS["tdee_basis"]
    adjust = _num(get_setting("nutrition.adjust_pct", ""))
    return {
        "goal": goal,
        "tdee_basis": basis,
        "adjust_pct": adjust if adjust is not None else NUTRITION_DEFAULTS["adjust_pct"],
        "activity_factor": _num(get_setting("nutrition.activity_factor", ""))
        or NUTRITION_DEFAULTS["activity_factor"],
        "protein_per_kg_lean": _num(get_setting("nutrition.protein_per_kg_lean", ""))
        or NUTRITION_DEFAULTS["protein_per_kg_lean"],
    }


def save_nutrition_cfg(values: dict) -> None:
    if "goal" in values:
        g = (values["goal"] or "").strip()
        set_setting("nutrition.goal", g if g in NUTRITION_GOALS else "")
    if "tdee_basis" in values:
        b = (values["tdee_basis"] or "").strip()
        set_setting("nutrition.tdee_basis", b if b in TDEE_BASES else "")
    for k in ("adjust_pct", "activity_factor", "protein_per_kg_lean"):
        if k in values:
            v = _num(values[k])
            set_setting(f"nutrition.{k}", "" if v is None else str(v))


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


def _body_composition() -> dict:
    """Latest smart-scale reading + trend, for the AI prompt. Empty if no scale
    data. The trend compares the latest reading with the oldest in the last ~90
    days so the AI can see where fat/muscle are heading, not just today's number."""
    with Session(engine) as session:
        rows = session.exec(select(BodyMeasure)
                            .order_by(BodyMeasure.measured_at.desc()).limit(90)).all()
    if not rows:
        return {}
    last = rows[0]
    out: dict = {}
    if last.body_fat is not None:
        out["massa_grassa_pct"] = round(last.body_fat, 1)
    if last.muscle_kg is not None:
        out["massa_muscolare_kg"] = round(last.muscle_kg, 1)
    if last.water_pct is not None:
        out["acqua_pct"] = round(last.water_pct, 1)
    if last.visceral is not None:
        out["grasso_viscerale"] = round(last.visceral, 1)
    if last.bmr is not None:
        out["metabolismo_basale_kcal"] = round(last.bmr)
    if last.metabolic_age is not None:
        out["eta_metabolica"] = round(last.metabolic_age)
    # trend vs the oldest reading we have in the window (needs ≥2 weigh-ins)
    if len(rows) > 1:
        first = rows[-1]
        trend = {}
        if last.weight_kg is not None and first.weight_kg is not None:
            trend["peso_kg"] = round(last.weight_kg - first.weight_kg, 1)
        if last.body_fat is not None and first.body_fat is not None:
            trend["massa_grassa_pct"] = round(last.body_fat - first.body_fat, 1)
        if last.muscle_kg is not None and first.muscle_kg is not None:
            trend["massa_muscolare_kg"] = round(last.muscle_kg - first.muscle_kg, 1)
        if trend:
            days = (last.measured_at - first.measured_at).days
            out["andamento"] = {"giorni": days, "delta": trend}
    return out


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
    if (comp := _body_composition()):
        out["composizione_corporea"] = comp
    # Free-text memory: injuries, equipment, availability, goals, preferences.
    # Placed last and labelled so the AI treats it as durable context to respect.
    if p.get("ai_notes"):
        out["note_da_rispettare"] = p["ai_notes"]
    return out
