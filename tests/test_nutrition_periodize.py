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


def test_free_meal_quota_is_prorated_to_the_period():
    # caso reale: Salute su ~un mese mostrava "8/2 pasti liberi" (quota 2 è SETTIMANALE)
    from datetime import date
    from app.nutrition import free_meal_quota_period
    q = free_meal_quota_period(2, date(2026, 9, 24), date(2026, 10, 23))      # 30 giorni
    assert q == {"quota_pasti_liberi": 9, "quota_pasti_liberi_settimana": 2}
    assert free_meal_quota_period(2, date(2026, 10, 19), date(2026, 10, 25))["quota_pasti_liberi"] == 2
    assert free_meal_quota_period(2, date(2026, 10, 23), date(2026, 10, 23))["quota_pasti_liberi"] == 1
