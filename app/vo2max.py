"""VO2max estimation from data OpenFit already has.

The Pixel Watch's Firstbeat VO2max isn't exposed by the Google Health API, so we
estimate it. Both methods are TREND indicators more than clinical values
(±10-15% on the absolute):

- Uth-Sørensen, from resting and max HR — available every day, any sport. The
  resting HR moves while max HR is fixed, so the day-to-day trend is meaningful
  even when the absolute leans on a formula-based max HR.
- ACSM cycling, from the best ~5-min power and body mass — sharper for the
  cyclist, shown when a recent ride with a power stream yields one.
"""

UTH_K = 15.3


def uth(rest_hr: float | None, max_hr: float | None) -> float | None:
    """Uth-Sørensen-Overgaard-Pedersen: VO2max ≈ 15.3 × (HRmax / HRrest)."""
    if not rest_hr or not max_hr or rest_hr <= 0:
        return None
    return round(UTH_K * (max_hr / rest_hr), 1)


def from_cycling_power(best_5min_w: float | None, weight_kg: float | None) -> float | None:
    """ACSM cycling estimate at the ~5-min max aerobic power:
    VO2 (ml/kg/min) ≈ (10.8 × W / body-kg) + 7. Uses BODY mass (not rider+bike),
    since the result is per kg of body weight."""
    if not best_5min_w or not weight_kg or weight_kg <= 0:
        return None
    return round(10.8 * best_5min_w / weight_kg + 7, 1)


def trend_from_resting(resting_series: list | None, max_hr: float | None) -> list:
    """A VO2max point per day from the resting-HR series (max HR held fixed)."""
    out = []
    for p in resting_series or []:
        v = uth(p.get("value"), max_hr)
        if v:
            out.append({"date": p.get("date"), "value": v})
    return out
