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


def test_block_drift_within_work_blocks():
    # blocco 10′ a 150 W: FC 140 fissa (in controllo), poi blocco 10′ a 150 W con FC che sale 140->154
    power = [150] * 1200
    hr = [140] * 600 + [140 + (i * 14) // 600 for i in range(600)]
    laps = [{"i": 1, "time_s": 600, "avg_power": 150, "start": START.isoformat()},
            {"i": 2, "time_s": 600, "avg_power": 150, "start": (START + timedelta(seconds=600)).isoformat()},
            {"i": 3, "time_s": 120, "avg_power": 90, "start": (START + timedelta(seconds=1200)).isoformat()}]
    d = F.block_drift(laps, {"t": list(range(1200)), "power": power, "hr": hr}, START, min_power=120)
    assert d[1]["deriva_pct"] == 0
    # esclusi i primi 2′: FC media ~145.6 -> ~151.2 a parità di watt = +3.8%
    assert 3.5 < d[2]["deriva_pct"] < 4.1 and d[2]["fc_fine"] > d[2]["fc_inizio"]
    assert 3 not in d                                    # recupero corto: niente deriva


def test_no_drift_when_power_ramps():
    # 20′ in progressione 150->200 W (come un test FTP mal dosato): non è deriva
    power = [150 + i * 50 // 1200 for i in range(1200)]
    hr = [140 + i * 15 // 1200 for i in range(1200)]
    laps = [{"i": 1, "time_s": 1200, "avg_power": 175, "start": START.isoformat()}]
    assert F.block_drift(laps, {"t": list(range(1200)), "power": power, "hr": hr}, START) == {}
