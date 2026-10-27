"""Tempi nuoto: lettura tempi/passaggi e progressi per gara."""
from app.swim import events, fmt, lap_labels, parse_splits, parse_time


def test_parse_time_formats():
    assert parse_time("1:23.45") == 83.45
    assert parse_time("1'23\"45") == 83.45
    assert parse_time("1.23.45") == 83.45
    assert parse_time("38,2") == 38.2
    assert parse_time("abc") is None and parse_time("") is None


def test_splits_cumulative_and_missing_last():
    # passaggi sui 25 di un 100: 17.5, 37.0, 58.9 -> l'ultimo dal tempo finale 1:19.0
    assert parse_splits("0:17.5 0:37.0 0:58.9", 79.0, 100) == [17.5, 19.5, 21.9, 20.1]
    assert parse_splits("17.5 37 58.9 1:19.0", 79.0, 100) == [17.5, 19.5, 21.9, 20.1]


def test_splits_per_lap():
    assert parse_splits("17.5 19.5 21.9 20.1", 79.0, 100) == [17.5, 19.5, 21.9, 20.1]
    assert parse_splits("17.5; 19.5; 21.9", 79.0, 100) == [17.5, 19.5, 21.9, 20.1]
    assert parse_splits("", 79.0, 100) == []


def test_medley_labels():
    assert lap_labels("misti", 100, 25, 4) == ["25 m · delfino", "50 m · dorso",
                                                "75 m · rana", "100 m · libero"]
    assert lap_labels("misti", 200, 25, 8)[2] == "75 m · dorso"


def test_events_best_and_progress():
    rows = [{"id": 1, "date": "2026-03-01", "distance_m": 50, "style": "libero", "time_s": 42.0},
            {"id": 2, "date": "2026-06-01", "distance_m": 50, "style": "libero", "time_s": 39.5},
            {"id": 3, "date": "2026-10-01", "distance_m": 50, "style": "libero", "time_s": 40.1},
            {"id": 4, "date": "2026-10-01", "distance_m": 100, "style": "misti", "time_s": 101.0}]
    ev = events(rows)
    free = ev[0]
    assert free["label"] == "50 stile libero" and free["n"] == 3
    assert free["best"] == fmt(39.5) == "39.50" and free["best_date"] == "2026-06-01"
    assert free["delta_s"] == -1.9
    assert [a["pb"] for a in free["attempts"]] == [False, True, False]   # recenti prima; la prima non è un PB
    assert ev[1]["delta_s"] is None and fmt(101.0) == "1:41.00"
