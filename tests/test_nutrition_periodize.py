"""Periodizzazione del target giornaliero (fix: il surplus del giorno allenato
non deve sparire quando la sessione è segnata «fatta»; usa l'attività reale)."""
from app.nutrition import _periodize, _session_kcal


def test_future_day_uses_plan():
    out = _periodize(1755, [("2026-10-25", "Camminata", 45)], [])
    assert out["2026-10-25"]["kcal"] == round(1755 + _session_kcal("Camminata", 45))
    assert out["2026-10-25"]["training_note"] == "Camminata 45′"


def test_done_session_surplus_kept_via_actual():
    # la sessione pianificata è «fatta» (fuori da `planned`); resta l'attività reale
    out = _periodize(1755, [], [("2026-10-21", "Nuoto", 57)])
    assert out["2026-10-21"]["kcal"] == round(1755 + _session_kcal("Nuoto", 57))
    assert out["2026-10-21"]["training_note"] == "Nuoto 57′"


def test_takes_max_of_plan_and_actual():
    # stesso giorno: piano 75′ vs reale 50′ -> vince il piano (max)
    out = _periodize(1000, [("2026-10-22", "Nuoto", 75)], [("2026-10-22", "Nuoto", 50)])
    assert out["2026-10-22"]["kcal"] == round(1000 + _session_kcal("Nuoto", 75))


def test_actual_wins_when_bigger_or_equal():
    out = _periodize(1000, [("2026-10-22", "Nuoto", 40)], [("2026-10-22", "Nuoto", 60)])
    assert out["2026-10-22"]["kcal"] == round(1000 + _session_kcal("Nuoto", 60))
    assert out["2026-10-22"]["training_note"] == "Nuoto 60′"


def test_no_training_day_omitted():
    assert _periodize(1755, [], []) == {}


def test_multiple_sessions_same_day_summed():
    out = _periodize(1000, [("2026-10-23", "Forza", 50), ("2026-10-23", "Corpo libero", 20)], [])
    exp = _session_kcal("Forza", 50) + _session_kcal("Corpo libero", 20)
    assert out["2026-10-23"]["kcal"] == round(1000 + exp)
    assert "Forza 50′" in out["2026-10-23"]["training_note"]
    assert "Corpo libero 20′" in out["2026-10-23"]["training_note"]
