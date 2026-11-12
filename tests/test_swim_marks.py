from datetime import datetime

from app.main import swim_lap_marks


def test_lengths_fill_the_lap_with_their_strokes():
    start = datetime(2026, 11, 9, 19, 5, 21)
    laps = [{"i": 1, "start": "2026-11-09T19:06:14", "distance_m": 50, "time_s": 45.1, "stroke": "mixed",
             "lengths": [{"time_s": 16.2, "stroke": "freestyle"}, {"time_s": 28.9, "stroke": "breaststroke"}]},
            {"i": 2, "start": "2026-11-09T19:08:00", "distance_m": 25, "time_s": 20.0, "stroke": "freestyle",
             "lengths": []}]
    m = swim_lap_marks(laps, start)
    assert m[0]["a"] == 53 and m[0]["b"] == 98.1 and m[0]["st"] == "mx" and m[0]["dist"] == 50
    assert [l["st"] for l in m[0]["lens"]] == ["SL", "RA"]
    assert m[0]["lens"][0]["b"] == m[0]["lens"][1]["a"] and m[0]["lens"][-1]["b"] == m[0]["b"]
    assert m[1]["lens"] == [] and m[1]["swim"]
