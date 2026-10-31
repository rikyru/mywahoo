from app import google_health as G


def test_indoor_bike_has_its_own_label_but_stays_bike():
    for label in ("SPINNING", "Biking (indoor trainer)", "Cycling virtual_activity", "Ciclismo indoor"):
        assert G.canonical_sport(label) == "Ciclismo indoor"
        assert G._sport_family(G.canonical_sport(label)) == "bike"
    assert G._sport_label("SPINNING") == "Ciclismo indoor"


def test_outdoor_bike_and_other_sports_unchanged():
    assert G.canonical_sport("Cycling") == "Ciclismo"
    assert G.canonical_sport("Biking (road)") == "Ciclismo"
    assert G.canonical_sport("running treadmill") == "Corsa"


def test_indoor_ride_dedups_with_google_twin():
    from datetime import datetime, timedelta
    t = datetime(2026, 10, 31, 16, 55)
    # FIT di MyWhoosh vs "Spinning" del Pixel Watch partito 13' dopo: stessa attività
    assert G._same_activity(t + timedelta(minutes=13), t + timedelta(minutes=80), "Ciclismo indoor",
                            t, t + timedelta(minutes=76), "Ciclismo indoor")
    # e anche se Google lo chiamasse ellittica (sport sconosciuto: si accorpa per orario)
    assert G._same_activity(t + timedelta(minutes=13), t + timedelta(minutes=80), "Elliptical",
                            t, t + timedelta(minutes=76), "Ciclismo indoor")
