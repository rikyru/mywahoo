"""Pure season-over-season progress shaping (no DB / no AI)."""
from datetime import date, datetime

from app import progress as P


TODAY = date(2026, 7, 1)   # day-of-season 182


def _eff(d, time_s, vam=None, hr=None):
    return {"date": d, "time_s": time_s, "vam": vam, "avg_hr": hr}


def test_season_and_doy():
    assert P.season_year(date(2026, 3, 10)) == 2026
    assert P.season_year(datetime(2025, 12, 31, 23, 0)) == 2025
    assert P.day_of_season(date(2026, 1, 1)) == 1
    assert P.day_of_season("2026-12-31") == 365


def test_segment_compare_faster_at_lower_hr_is_improved():
    effs = [
        _eff(date(2025, 5, 1), 600, vam=900, hr=160),   # last season
        _eff(date(2026, 5, 1), 570, vam=950, hr=158),   # this season: faster, lower HR
    ]
    c = P.segment_compare(effs, TODAY)
    assert c["this"]["time_s"] == 570 and c["last"]["time_s"] == 600
    assert c["time_delta_pct"] == -5.0          # 5% faster
    assert c["vam_delta_pct"] and c["vam_delta_pct"] > 0
    assert c["hr_delta"] == -2
    assert c["verdict"] == "migliorato"
    assert c["is_pr_this_season"] is True
    assert c["n_seasons"] == 2


def test_segment_compare_faster_but_higher_hr_is_just_harder():
    effs = [
        _eff(date(2025, 5, 1), 600, hr=150),
        _eff(date(2026, 5, 1), 560, hr=165),   # faster but pushed much harder
    ]
    c = P.segment_compare(effs, TODAY)
    assert c["verdict"] == "più veloce"
    assert c["hr_delta"] == 15


def test_segment_compare_slower():
    effs = [_eff(date(2025, 5, 1), 500), _eff(date(2026, 5, 1), 560)]
    c = P.segment_compare(effs, TODAY)
    assert c["verdict"] == "più lento"
    assert c["time_delta_pct"] == 12.0


def test_segment_compare_only_this_season_has_no_delta():
    effs = [_eff(date(2026, 4, 1), 600), _eff(date(2026, 5, 1), 580)]
    c = P.segment_compare(effs, TODAY)
    assert c["last"] is None
    assert c["time_delta_pct"] is None and c["verdict"] is None
    assert c["this"]["time_s"] == 580


def test_align_by_season_keeps_recent_two_and_splits():
    pts = [{"date": date(2024, 2, 1), "value": 1},
           {"date": date(2025, 2, 1), "value": 2},
           {"date": date(2026, 2, 1), "value": 3}]
    out = P.align_by_season(pts, TODAY)
    assert set(out) == {2025, 2026}          # only the latest two seasons
    assert out[2026][0]["value"] == 3
    assert out[2026][0]["doy"] == P.day_of_season(date(2026, 2, 1))


def test_cumulative_by_season_accumulates():
    pts = [{"date": date(2026, 1, 1), "value": 2},
           {"date": date(2026, 1, 3), "value": 3},
           {"date": date(2026, 1, 2), "value": 1}]
    out = P.cumulative_by_season(pts, TODAY)[2026]
    assert [p["value"] for p in out] == [2, 3, 6]   # sorted by doy, running sum


def test_season_volume_caps_at_today_day_of_season():
    rides = [
        {"date": date(2025, 3, 1), "moving_s": 3600, "distance_m": 30000, "ascent_m": 400},
        {"date": date(2025, 10, 1), "moving_s": 7200, "distance_m": 60000, "ascent_m": 800},  # past today's doy -> excluded
        {"date": date(2026, 3, 1), "moving_s": 5400, "distance_m": 45000, "ascent_m": 600},
    ]
    v = P.season_volume(rides, TODAY)
    assert v["rides"]["this"] == 1 and v["rides"]["last"] == 1   # Oct ride not counted
    assert v["hours"]["this"] == 1.5 and v["hours"]["last"] == 1.0
    assert v["hours"]["delta_pct"] == 50.0
