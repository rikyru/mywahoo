"""Body-composition metrics from weight + bioimpedance.

A faithful Python port of the ESPHome template-sensor lambdas used on the
Xiaomi scale (xiaomi_miscale). The scale itself only sends weight + impedance
over BLE; everything else (fat %, water %, bone, muscle, visceral, BMR,
metabolic age, BMI) is *derived* from those two plus the user profile
(height, age, sex). We compute them here so OpenFit does not depend on
Home Assistant's slow (~60 s) template-sensor polling — HA only needs to push
the two instant sensors and we fill in the rest.

All formulas match the ESPHome config 1:1 (same constants, same clamps).
"""
from __future__ import annotations

from typing import Optional


def _clamp(v: float, lo: float, hi: float) -> float:
    if v < lo:
        return lo
    if v > hi:
        return hi
    return v


def _lbm(weight: float, height: float, age: float, impedance: float) -> float:
    """Lean body mass — the common base for fat/bone."""
    lbm = (height * 9.058 / 100.0) * (height / 100.0)
    lbm += weight * 0.32 + 12.226
    lbm -= impedance * 0.0068
    lbm -= age * 0.0542
    return lbm


def _bmi(weight: float, height: float) -> float:
    h = height / 100.0
    return weight / (h * h)


def _body_fat(weight: float, height: float, age: float, impedance: float,
              male: bool) -> float:
    lbm = _lbm(weight, height, age, impedance)

    if not male and age <= 49:
        c = 9.25
    elif not male and age > 49:
        c = 7.25
    else:
        c = 0.8

    if male and weight < 61.0:
        coefficient = 0.98
    elif not male and weight > 60.0:
        coefficient = 0.96
        if height > 160.0:
            coefficient *= 1.03
    elif not male and weight < 50.0:
        coefficient = 1.02
        if height > 160.0:
            coefficient *= 1.03
    else:
        coefficient = 1.0

    fat = (1.0 - (((lbm - c) * coefficient) / weight)) * 100.0
    if fat > 63.0:
        fat = 75.0
    return _clamp(fat, 5.0, 75.0)


def _water(body_fat: float) -> float:
    water = (100.0 - body_fat) * 0.7
    coefficient = 1.02 if water <= 50.0 else 0.98
    if water * coefficient >= 65.0:
        water = 75.0
    return _clamp(water * coefficient, 35.0, 75.0)


def _bone(weight: float, height: float, age: float, impedance: float,
          male: bool) -> float:
    lbm = _lbm(weight, height, age, impedance)
    base = 0.18016894 if male else 0.245691014
    bone = (base - (lbm * 0.05158)) * -1.0
    if bone > 2.2:
        bone += 0.1
    else:
        bone -= 0.1
    if not male and bone > 5.1:
        bone = 8.0
    if male and bone > 5.2:
        bone = 8.0
    return _clamp(bone, 0.5, 8.0)


def _muscle(weight: float, body_fat: float, bone: float, male: bool) -> float:
    muscle = weight - ((body_fat * 0.01) * weight) - bone
    if not male and muscle >= 84.0:
        muscle = 120.0
    if male and muscle >= 93.5:
        muscle = 120.0
    return _clamp(muscle, 10.0, 120.0)


def _visceral(weight: float, height: float, age: float, male: bool) -> float:
    if not male:
        if weight > (13.0 - (height * 0.5)) * -1.0:
            subsubcalc = ((height * 1.45) + (height * 0.1158) * height) - 120.0
            subcalc = weight * 500.0 / subsubcalc
            vfal = (subcalc - 6.0) + (age * 0.07)
        else:
            subcalc = 0.691 + (height * -0.0024) + (height * -0.0024)
            vfal = (((height * 0.027) - (subcalc * weight)) * -1.0) + (age * 0.07) - age
    else:
        if height < weight * 1.6:
            subcalc = ((height * 0.4) - (height * (height * 0.0826))) * -1.0
            vfal = ((weight * 305.0) / (subcalc + 48.0)) - 2.9 + (age * 0.15)
        else:
            subcalc = 0.765 + height * -0.0015
            vfal = (((height * 0.143) - (weight * subcalc)) * -1.0) + (age * 0.15) - 5.0
    return _clamp(vfal, 1.0, 50.0)


def _bmr(weight: float, height: float, age: float, male: bool) -> float:
    if not male:
        bmr = 864.6 + weight * 10.2036 - height * 0.39336 - age * 6.204
        if bmr > 2996.0:
            bmr = 5000.0
    else:
        bmr = 877.8 + weight * 14.916 - height * 0.726 - age * 8.976
        if bmr > 2322.0:
            bmr = 5000.0
    return _clamp(bmr, 500.0, 10000.0)


def _metabolic_age(weight: float, height: float, age: float, impedance: float,
                   male: bool) -> float:
    if not male:
        ma = (height * -1.1165) + (weight * 1.5784) + (age * 0.4615) \
            + (impedance * 0.0415) + 83.2548
    else:
        ma = (height * -0.7471) + (weight * 0.9161) + (age * 0.4184) \
            + (impedance * 0.0517) + 54.2267
    return _clamp(ma, 15.0, 80.0)


def compute(weight: Optional[float], impedance: Optional[float],
            height_cm: Optional[float], age: Optional[float],
            sex: Optional[str]) -> dict:
    """Return the derived body-composition metrics.

    `weight` is required. Metrics needing bioimpedance (fat, water, bone,
    muscle, metabolic_age) are only produced when `impedance` is present and
    positive. BMI needs only weight+height; visceral/BMR need weight+profile.
    Values the inputs cannot support are omitted (left for the caller to keep
    whatever the scale may have sent).
    """
    out: dict = {}
    if weight is None or weight <= 0:
        return out
    if not height_cm or not age or age <= 0:
        return out  # profile incomplete: cannot derive reliably

    height = float(height_cm)
    age = float(age)
    weight = float(weight)
    male = (sex or "M").strip().upper() != "F"

    out["bmi"] = round(_bmi(weight, height), 1)
    out["visceral"] = round(_visceral(weight, height, age, male), 1)
    out["bmr"] = round(_bmr(weight, height, age, male), 0)

    if impedance and impedance > 0:
        imp = float(impedance)
        fat = _body_fat(weight, height, age, imp, male)
        bone = _bone(weight, height, age, imp, male)
        out["body_fat"] = round(fat, 1)
        out["water_pct"] = round(_water(fat), 1)
        out["bone_kg"] = round(bone, 2)
        out["muscle_kg"] = round(_muscle(weight, fat, bone, male), 2)
        out["metabolic_age"] = round(_metabolic_age(weight, height, age, imp, male), 0)

    return out


# ------------------------------------------------------------ pesate per giorno

# (campo, etichetta, unità, decimali, verso "buono": -1 scendere è bene, +1 salire, 0 neutro)
WEIGHIN_FIELDS = [
    ("weight_kg", "peso", "kg", 1, -1),
    ("body_fat", "massa grassa", "%", 1, -1),
    ("muscle_kg", "muscolo", "kg", 1, +1),
    ("water_pct", "acqua", "%", 1, +1),
    ("visceral", "grasso viscerale", "", 1, -1),
    ("bmi", "BMI", "", 1, -1),
    ("bmr", "metabolismo basale", "kcal", 0, 0),
    ("bone_kg", "massa ossea", "kg", 1, 0),
    ("metabolic_age", "età metabolica", "anni", 0, -1),
]
_COLS = [f for f, *_ in WEIGHIN_FIELDS]
# sotto queste variazioni è rumore della bilancia (acqua, ora, cibo): niente colore
NOISE = {"weight_kg": 0.3, "body_fat": 0.1, "muscle_kg": 0.1, "water_pct": 0.2,
         "visceral": 0.2, "bmi": 0.1, "metabolic_age": 1}


def is_repush(a, b) -> bool:
    """La lettura b è un re-invio di a (Home Assistant che rimanda l'ultimo valore
    al riavvio): stesso peso e stessa impedenza, quindi non è una nuova pesata."""
    if a is None or b is None or a.weight_kg is None or b.weight_kg is None:
        return False
    same_w = abs(a.weight_kg - b.weight_kg) < 0.01
    if a.impedance is not None and b.impedance is not None:
        return same_w and abs(a.impedance - b.impedance) < 0.5
    # senza impedenza (manuali): uguale solo se coincidono tutti i campi
    return same_w and all(getattr(a, c) == getattr(b, c) for c in _COLS)


def daily_weighins(rows: list) -> list[dict]:
    """Letture della bilancia (dalla più vecchia) -> una pesata per giorno:
    i re-invii identici sono scartati, più pesate vere nello stesso giorno sono
    mediate campo per campo. [{date, at, n, <campi>}] dalla più vecchia."""
    kept, prev = [], None
    for r in rows:
        if r.weight_kg is None and all(getattr(r, c) is None for c in _COLS):
            continue
        if is_repush(prev, r):
            continue
        kept.append(r)
        prev = r
    days: dict = {}
    for r in kept:
        days.setdefault(r.measured_at.date(), []).append(r)
    out = []
    for d, rs in sorted(days.items()):
        day = {"date": d, "at": rs[-1].measured_at, "n": len(rs)}
        for c in _COLS:
            vals = [getattr(r, c) for r in rs if getattr(r, c) is not None]
            day[c] = sum(vals) / len(vals) if vals else None
        out.append(day)
    return out


def weighin_changes(days: list[dict]) -> list[dict]:
    """Ultima pesata vs la precedente che ha quel dato: [{field, label, unit, value,
    delta, prev_date, tone}] per i campi presenti nell'ultima."""
    if not days:
        return []
    last = days[-1]
    out = []
    for f, label, unit, nd, good in WEIGHIN_FIELDS:
        v = last.get(f)
        if v is None:
            continue
        prev = next((d for d in reversed(days[:-1]) if d.get(f) is not None), None)
        delta = round(v - prev[f], nd if nd else 0) if prev else None
        noise = NOISE.get(f, 0.5 if nd == 0 else 0.1)
        tone = ""
        if delta is not None and good and abs(delta) >= noise:
            tone = "gd-good" if delta * good > 0 else "gd-warn"
        out.append({"field": f, "label": label, "unit": unit, "nd": nd,
                    "value": round(v, nd) if nd else round(v),
                    "delta": delta, "prev_date": prev["date"] if prev else None, "tone": tone})
    return out
