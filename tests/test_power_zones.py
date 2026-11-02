from app import fit as F


def test_power_zones_split_time_by_ftp():
    # 10' a 100 W, 10' a 160 W, 1' a 300 W con FTP 160 (1 Hz)
    power = [100] * 600 + [160] * 600 + [300] * 60
    streams = {"t": list(range(len(power))), "power": power}
    z = {x["label"].split(" ·")[0]: x for x in F.power_zone_distribution(streams, 160)}
    assert z["Z2"]["seconds"] == 600          # 100/160 = 62% -> fondo
    assert z["Z4"]["seconds"] == 600          # 100% -> soglia
    assert z["Z7"]["seconds"] == 60           # 188% -> neuromuscolare (l'ultimo campione vale il passo tipico)
    assert z["Z7"]["lo_w"] == round(1.51 * 160) and z["Z7"]["hi_w"] is None
    assert sum(x["pct"] for x in z.values()) in (99, 100, 101)


def test_coasting_counts_as_z1_and_no_power_no_zones():
    streams = {"t": [0, 1, 2, 3], "power": [0, None, 0, 0]}
    z = F.power_zone_distribution(streams, 200)
    assert z == []                            # nessun watt vero: niente zone
    streams = {"t": [0, 1, 2, 3, 4], "power": [0, 0, 250, 0, 0]}
    z = F.power_zone_distribution(streams, 200)
    assert z[0]["seconds"] >= 3
