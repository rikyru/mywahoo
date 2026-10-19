"""Season-over-season cycling progress.

The honest way to track progress WITHOUT a power meter is to compare like with
like: the SAME climb/segment ridden again (time, VAM and at what heart rate),
and trends aligned by day-of-season so "where am I now vs a year ago" is a fair,
same-point-in-the-year comparison. This module holds the pure data shaping;
I/O (DB, AI) stays in main.py / anthropic_client.py so it's easy to test.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime


def _as_date(d) -> date:
    if isinstance(d, datetime):
        return d.date()
    if isinstance(d, str):
        return date.fromisoformat(d[:10])
    return d


def season_year(d) -> int:
    """Season = calendar year. Simple, and matches 'inizio stagione' ≈ 1 gennaio.
    (If one day you ride all winter, this can be swapped for a March cutover.)"""
    return _as_date(d).year


def day_of_season(d) -> int:
    """Day-of-year (1..366): the x-axis that lets two seasons line up."""
    return _as_date(d).timetuple().tm_yday


def _pct(now: float | None, ref: float | None) -> float | None:
    """Signed % change of `now` vs `ref` (None if either missing/zero)."""
    if not now or not ref:
        return None
    return round((now - ref) / ref * 100, 1)


def _best_effort(efforts: list[dict]) -> dict | None:
    """Fastest effort (min time_s) of a list, carrying its vam/hr/speed/date."""
    cand = [e for e in efforts if e.get("time_s")]
    if not cand:
        return None
    b = min(cand, key=lambda e: e["time_s"])
    return {"time_s": int(b["time_s"]),
            "vam": round(b["vam"]) if b.get("vam") else None,
            "avg_hr": round(b["avg_hr"]) if b.get("avg_hr") else None,
            "speed_kmh": round(b["speed_kmh"], 1) if b.get("speed_kmh") else None,
            "date": _as_date(b["date"]).isoformat()}


def segment_compare(efforts: list[dict], today: date) -> dict | None:
    """Season-over-season comparison for ONE segment's efforts.

    efforts: [{date, time_s, vam?, avg_hr?, speed_kmh?}]. Returns best THIS
    season, best LAST season (both full and capped at today's day-of-season for
    a fair 'a oggi'), the all-time best, and the % deltas. Lower time is better,
    so a NEGATIVE time delta = faster = improvement; HR context says whether it
    was fitness (faster at equal/lower HR) or just a harder push.
    None if there isn't enough to compare (no efforts at all)."""
    if not efforts:
        return None
    cur = season_year(today)
    doy = day_of_season(today)
    by_season: dict[int, list[dict]] = defaultdict(list)
    for e in efforts:
        by_season[season_year(e["date"])].append(e)

    this_best = _best_effort(by_season.get(cur, []))
    last_all = by_season.get(cur - 1, [])
    last_best = _best_effort(last_all)
    last_best_todate = _best_effort(
        [e for e in last_all if day_of_season(e["date"]) <= doy])
    all_time = _best_effort(efforts)

    seasons = sorted(by_season)
    # time delta vs last season (negative = faster = better)
    time_delta_pct = (_pct(this_best["time_s"], last_best["time_s"])
                      if this_best and last_best else None)
    vam_delta_pct = (_pct(this_best["vam"], last_best["vam"])
                     if this_best and last_best
                     and this_best["vam"] and last_best["vam"] else None)
    hr_delta = (this_best["avg_hr"] - last_best["avg_hr"]
                if this_best and last_best
                and this_best["avg_hr"] and last_best["avg_hr"] else None)

    return {
        "this": this_best,
        "last": last_best,
        "last_todate": last_best_todate,
        "best": all_time,
        "n_efforts": len(efforts),
        "n_seasons": len(seasons),
        "seasons": seasons,
        "is_pr_this_season": bool(this_best and all_time
                                  and this_best["time_s"] == all_time["time_s"]),
        "time_delta_pct": time_delta_pct,     # <0 = più veloce di un anno fa
        "vam_delta_pct": vam_delta_pct,       # >0 = più VAM
        "hr_delta": hr_delta,                 # <0 = stesso/più veloce a FC più bassa
        "verdict": _verdict(time_delta_pct, hr_delta),
    }


def _verdict(time_delta_pct: float | None, hr_delta: int | None) -> str | None:
    """A one-word read of a segment's year-over-year change, HR-aware."""
    if time_delta_pct is None:
        return None
    if time_delta_pct <= -2:                       # meaningfully faster
        if hr_delta is not None and hr_delta <= 1:
            return "migliorato"                    # faster at equal/lower HR
        return "più veloce"                        # faster but pushed harder
    if time_delta_pct >= 2:
        return "più lento"
    return "stabile"


def align_by_season(points: list[dict], today: date, value_key: str = "value",
                    only_recent: int = 2) -> dict[int, list[dict]]:
    """Split a dated per-day series into one line per season, x = day-of-season.

    points: [{date, <value_key>}]. Keeps the latest `only_recent` seasons (this
    one and last, by default) so the overlay stays readable. Returns
    {season_year: [{doy, date, value}]} sorted by doy."""
    by_season: dict[int, list[dict]] = defaultdict(list)
    for p in points:
        v = p.get(value_key)
        if v is None:
            continue
        d = _as_date(p["date"])
        by_season[season_year(d)].append(
            {"doy": day_of_season(d), "date": d.isoformat(), "value": round(v, 2)})
    keep = sorted(by_season)[-only_recent:]
    return {s: sorted(by_season[s], key=lambda x: x["doy"]) for s in keep}


def cumulative_by_season(day_values: list[dict], today: date,
                         value_key: str = "value", only_recent: int = 2
                         ) -> dict[int, list[dict]]:
    """Like align_by_season but the value accumulates within each season — for
    'volume so far this season vs last' (hours, elevation, distance)."""
    by_season: dict[int, list[dict]] = defaultdict(list)
    for p in day_values:
        v = p.get(value_key)
        if v is None:
            continue
        d = _as_date(p["date"])
        by_season[season_year(d)].append((day_of_season(d), v))
    keep = sorted(by_season)[-only_recent:]
    out: dict[int, list[dict]] = {}
    for s in keep:
        run = 0.0
        pts = []
        for doy, v in sorted(by_season[s]):
            run += v
            pts.append({"doy": doy, "value": round(run, 1)})
        out[s] = pts
    return out


def season_volume(workouts: list[dict], today: date) -> dict:
    """Headline 'a oggi' totals: this season vs last season at the SAME point of
    the year. workouts: [{date, moving_s, distance_m, ascent_m}] (bike only).
    Returns per-metric {this, last, delta_pct}."""
    cur = season_year(today)
    doy = day_of_season(today)
    acc = {s: {"hours": 0.0, "km": 0.0, "elev": 0.0, "rides": 0}
           for s in (cur, cur - 1)}
    for w in workouts:
        d = _as_date(w["date"])
        s = season_year(d)
        if s not in acc or day_of_season(d) > doy:     # only up to today's day-of-season
            continue
        acc[s]["hours"] += (w.get("moving_s") or 0) / 3600
        acc[s]["km"] += (w.get("distance_m") or 0) / 1000
        acc[s]["elev"] += w.get("ascent_m") or 0
        acc[s]["rides"] += 1
    out = {}
    for k in ("hours", "km", "elev", "rides"):
        now, ref = acc[cur][k], acc[cur - 1][k]
        out[k] = {"this": round(now, 1), "last": round(ref, 1),
                  "delta_pct": _pct(now, ref)}
    return out
