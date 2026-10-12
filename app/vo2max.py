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


# --- Garmin/Firstbeat-style estimate: submaximal workload vs HR, extrapolated ---
# to HRmax. During a run/ride, the oxygen cost of the external load (pace for a
# run, power for a ride) at a steady submaximal HR tells us the fraction of VO2max
# in use; projecting that to HRmax via the HR reserve gives VO2max — no need for a
# maximal effort, exactly how a watch derives it from everyday activities.

# Fitness classification by age & sex (lower bounds, ml/kg/min), approximating the
# Cooper Institute norms Garmin uses: [Discreto, Buono, Eccellente, Superiore].
# Below the first bound = Scarso; at/above the last = Superiore.
_NORMS = {
    "M": [(0, 29, (42, 46, 51, 56)), (30, 39, (41, 45, 50, 55)),
          (40, 49, (38, 43, 48, 53)), (50, 59, (35, 40, 45, 50)),
          (60, 200, (31, 36, 41, 46))],
    "F": [(0, 29, (36, 40, 45, 50)), (30, 39, (34, 38, 43, 48)),
          (40, 49, (31, 35, 40, 45)), (50, 59, (28, 32, 37, 42)),
          (60, 200, (25, 30, 35, 40))],
}
_LABELS = ("Scarso", "Discreto", "Buono", "Eccellente", "Superiore")


def category(vo2: float | None, age: int | None, sex: str | None) -> str | None:
    """Qualitative rating (Scarso…Superiore) for a VO2max, by age & sex — the
    "Excellent"-style indication Garmin shows. Age unknown → assumes ~35."""
    if not vo2:
        return None
    table = _NORMS.get((sex or "M").upper()[:1], _NORMS["M"])
    a = age if age and age > 0 else 35
    bounds = next((b for lo, hi, b in table if lo <= a <= hi), table[-1][2])
    idx = sum(1 for b in bounds if vo2 >= b)   # 0..4
    return _LABELS[idx]


def running_vo2(speed_ms: float | None) -> float | None:
    """ACSM level-running oxygen cost (ml/kg/min) at a steady speed."""
    if not speed_ms or speed_ms <= 1.5:        # slower than ~5.4 km/h: walking/noise
        return None
    return 0.2 * (speed_ms * 60.0) + 3.5       # speed_ms*60 = m/min


def cycling_vo2(power_w: float | None, weight_kg: float | None) -> float | None:
    """ACSM cycling oxygen cost (ml/kg/min) at a steady power."""
    if not power_w or power_w <= 0 or not weight_kg or weight_kg <= 0:
        return None
    return 10.8 * power_w / weight_kg + 7.0


def extrapolate(vo2_sub: float | None, hr: float | None,
                rest_hr: float, max_hr: float) -> float | None:
    """Project a submaximal (VO2, HR) point to HRmax via the HR reserve fraction:
    VO2max ≈ VO2_sub × (HRmax − HRrest) / (HR − HRrest)."""
    if (not vo2_sub or not hr or max_hr <= rest_hr
            or hr <= rest_hr or hr >= max_hr):
        return None
    return vo2_sub * (max_hr - rest_hr) / (hr - rest_hr)


def from_samples(workload_vo2: list, hrs: list, rest_hr: float, max_hr: float,
                 lo: float = 0.5, hi: float = 0.9, min_samples: int = 20) -> float | None:
    """Median VO2max over submaximal steady samples: pairs of (oxygen cost of the
    external load, HR) kept only in the HR-reserve band [lo, hi] and within a sane
    physiological range. None when too few samples to trust."""
    import statistics
    hrr = max_hr - rest_hr
    if hrr <= 0:
        return None
    ests = []
    for vo2, hr in zip(workload_vo2, hrs):
        if not vo2 or not hr:
            continue
        frac = (hr - rest_hr) / hrr
        if frac < lo or frac > hi:
            continue
        e = extrapolate(vo2, hr, rest_hr, max_hr)
        if e and 20.0 <= e <= 90.0:            # physiological sanity bounds
            ests.append(e)
    if len(ests) < min_samples:
        return None
    return round(statistics.median(ests), 1)
