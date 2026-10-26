"""Bilancio assunte vs bruciate (Salute): saldo per giorno, medie sui giorni chiusi,
macro vs obiettivi e copia compatta per il coach."""
from app.main import _energy_balance


def _data(**burned):
    return {"metrics": {"calories_burned": {"series": [
        {"date": d, "value": v} for d, v in burned.items()]}}}


def _nutri():
    return {
        "obiettivi": {"kcal": 1800, "proteine_g": 140, "carboidrati_g": 180, "grassi_g": 60},
        "alimentazione_tracciata": {"per_giorno": [
            {"data": "2026-10-24", "kcal": 1900, "proteine_g": 120, "carboidrati_g": 200, "grassi_g": 70},
            {"data": "2026-10-25", "kcal": 1700, "proteine_g": 140, "carboidrati_g": 160, "grassi_g": 50},
            {"data": "2026-10-26", "kcal": 600, "proteine_g": 30, "carboidrati_g": 60, "grassi_g": 20,
             "in_corso": True},
        ]},
    }


def test_balance_uses_closed_days_only():
    nutri = _nutri()
    b = _energy_balance(_data(**{"2026-10-24": 2400, "2026-10-25": 2300, "2026-10-26": 1200}), nutri)
    s = b["sintesi"]
    assert s["giorni"] == 2 and s["saldo_medio"] == -550          # (-500 + -600) / 2
    assert s["kg_settimana"] == round(-550 * 7 / 7700, 2)
    assert s["giorni_deficit"] == 2
    assert b["giorni"][0]["data"] == "2026-10-26" and b["giorni"][0]["in_corso"]
    prot = b["macro"][0]
    assert prot["media_g"] == 130 and prot["obiettivo_g"] == 140 and prot["pct"] == 93
    assert "sintesi" in nutri["bilancio_energetico"]
    assert len(nutri["bilancio_energetico"]["per_giorno"]) == 2


def test_no_burned_data_still_shows_macros():
    b = _energy_balance({}, _nutri())
    assert "sintesi" not in b and b["macro"]
    assert _energy_balance({}, {}) is None
