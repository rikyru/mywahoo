"""Tempi nuoto: lettura tempi/passaggi e progressi per gara."""
from app.swim import as_passages, events, fmt, medley_leg, parse_passages, parse_time, segments


def test_parse_time_formats():
    assert parse_time("1:23.45") == 83.45
    assert parse_time("1'23\"45") == 83.45
    assert parse_time("1.23.45") == 83.45
    assert parse_time("38,2") == 38.2
    assert parse_time("abc") is None and parse_time("") is None


def test_passages_from_form():
    # 100 m: passaggi letti al cronometro ai 25/50/75 (vuoti ignorati)
    pts, err = parse_passages({25: "17.5", 50: "0:37.0", 75: "58,9"}, 79.0, 100)
    assert err is None and pts == [[25, 17.5], [50, 37.0], [75, 58.9]]
    pts, err = parse_passages({25: "", 50: "37", 75: ""}, 79.0, 100)
    assert pts == [[50, 37.0]]


def test_passages_must_increase():
    # errore tipico: tempi delle singole vasche invece del cronometro
    pts, err = parse_passages({25: "17.5", 50: "19.5", 75: "21.9"}, 79.0, 100)
    assert pts == [] and "cronometro" in err
    _, err = parse_passages({25: "17.5", 50: "15"}, 79.0, 100)   # decrescenti
    assert "crescere" in err
    _, err = parse_passages({50: "1:30"}, 79.0, 100)       # oltre il tempo finale
    assert err


def test_segments_and_medley():
    seg = segments([[25, 17.5], [50, 37.0], [75, 58.9]], 79.0, 100, "misti")
    assert [g["at"] for g in seg] == ["25 m · delfino", "50 m · dorso", "75 m · rana", "100 m · libero"]
    assert [g["split"] for g in seg] == ["17.50", "19.50", "21.90", "20.10"]
    assert segments([[50, 37.0]], 79.0, 100, "libero")[1] == {
        "at": "100 m", "cum": "1:19.00", "split": "42.00", "span": 50}
    assert segments([], 79.0, 100, "libero") == []
    assert medley_leg(200, 75) == "dorso"


def test_old_lap_format_is_converted():
    assert as_passages([17.5, 19.5], 25) == [[25, 17.5], [50, 37.0]]


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
