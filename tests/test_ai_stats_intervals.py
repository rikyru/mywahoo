from datetime import datetime, timedelta

from app import fit as F

START = datetime(2026, 11, 7, 16, 47)


def _streams(power, hr):
    n = len(power)
    return {"t": list(range(n)), "power": power, "hr": hr, "cadence": [90] * n,
            "speed": [None] * n, "alt": [None] * n, "latlng": None}


def test_drift_only_for_steady_sessions():
    steady = _streams([150] * 3600, [130] * 1800 + [137] * 1800)
    s = F.ai_stats(steady)
    assert s["drift_cardiaco_pct"] == 5.4 and s["indice_variabilita"] <= F.STEADY_VI
    # blocchi 10′ a 230 W / 5′ a 90 W: VI alto -> niente percentuale di deriva
    blocks = ([230] * 600 + [90] * 300) * 4
    s = F.ai_stats(_streams(blocks, [140] * len(blocks)))
    assert "drift_cardiaco_pct" not in s and "non applicabile" in s["drift_cardiaco"]


def test_lap_peak_ignores_the_lap_change():
    # blocco a 150 W poi recupero: i primi secondi del recupero sono ancora a 145
    power = [150] * 600 + [145] * 3 + [100] * 177
    laps = [{"i": 1, "time_s": 600, "start": START.isoformat()},
            {"i": 2, "time_s": 180, "start": (START + timedelta(seconds=600)).isoformat()}]
    peaks = F.lap_peak_power(laps, {"t": list(range(len(power))), "power": power}, START)
    assert peaks == {1: 150, 2: 100}
