import pytest

from app import cycling as C


def test_speed_and_power_are_inverse():
    m = C.rider_mass(80)
    for g in (0, 4, 8):
        v = C.speed_at_power(250, g, m)
        assert C.power_at_speed(v, g, m) == pytest.approx(250, abs=0.5)


def test_sustainable_power_curve():
    assert C.sustainable_power(200, 3600) == pytest.approx(200)
    assert 1.07 < C.sustainable_power(200, 1200) / 200 < 1.09      # ~108% sui 20′
    assert C.sustainable_power(200, 30) == C.sustainable_power(200, 120)   # limitata sotto i 2′


def test_prediction_is_consistent_and_moves_the_right_way():
    m = C.rider_mass(82.4)
    t = C.predict_climb_time(3000, 6, m, 159)
    # il tempo previsto è coerente con la potenza tenibile per quella durata
    assert C.climb_power_for_time(3000, 6, m, t) == pytest.approx(C.sustainable_power(159, t), rel=0.01)
    assert C.predict_climb_time(3000, 6, m, 180) < t                 # più FTP -> più veloce
    assert C.predict_climb_time(3000, 6, C.rider_mass(78), 159) < t  # meno peso -> più veloce
    assert 15 * 60 < t < 20 * 60                                     # 3 km al 6% a ~2 W/kg
