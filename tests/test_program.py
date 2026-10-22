"""Motore del programma adattivo (puro)."""
from datetime import date

from app import program as P

MON = date(2026, 10, 26)          # lunedì
TPL = [
    {"id": "nuoto", "days": [0, 2], "sport": "Nuoto", "kind": "external", "duration": 75},
    {"id": "core", "days": [1], "sport": "Corpo libero", "kind": "planned", "duration": 30},
    {"id": "gambe", "days": [3], "sport": "Corpo libero", "kind": "planned", "duration": 45},
    {"id": "bici", "days": [5, 6], "pick": "one", "sport": "Ciclismo", "kind": "planned",
     "duration": 120},
    {"id": "off", "days": [4], "sport": "Corpo libero", "kind": "planned", "duration": 30,
     "enabled": False},
]


def _dates(inst, sid):
    return [i["date"] for i in inst if i["slot"]["id"] == sid]


def test_block_position_cycles_and_deload():
    m = [1.0, 1.05, 1.1, 0.7]
    assert P.block_position(MON, MON, 4, m)["week_in_block"] == 1
    p = P.block_position(MON, date(2026, 11, 18), 4, m)        # 4ª settimana
    assert (p["block"], p["week_in_block"], p["deload"]) == (1, 4, True)
    p = P.block_position(MON, date(2026, 11, 23), 4, m)        # blocco 2
    assert (p["block"], p["week_in_block"], p["multiplier"]) == (2, 1, 1.0)


def test_expand_single_and_multi_day_slots():
    inst = P.expand_slots(TPL, [], MON, date(2026, 11, 1), MON)
    assert _dates(inst, "core") == [date(2026, 10, 27)]
    assert _dates(inst, "nuoto") == [date(2026, 10, 26), date(2026, 10, 28)]   # lun E mer
    assert _dates(inst, "bici") == [date(2026, 10, 31)]        # sab O dom: il primo libero
    assert _dates(inst, "off") == []                            # disattivato


def test_multi_day_moves_to_sunday_when_saturday_skipped():
    inst = P.expand_slots(TPL, [], MON, date(2026, 11, 1), MON,
                          skipped={("bici", "2026-10-31")})
    assert _dates(inst, "bici") == [date(2026, 11, 1)]


def test_multi_day_none_if_already_done_that_week():
    inst = P.expand_slots(TPL, [], MON, date(2026, 11, 1), date(2026, 10, 31),
                          done_days={("ciclismo", "2026-10-31")})
    assert _dates(inst, "bici") == []


def test_multi_day_keeps_chosen_day():
    inst = P.expand_slots(TPL, [], MON, date(2026, 11, 1), MON,
                          keep_day={("bici", "2026-10-26"): "2026-11-01"})
    assert _dates(inst, "bici") == [date(2026, 11, 1)]


def test_absence_blocks_unless_sport_allowed():
    ab = [{"start": "2026-10-27", "end": "2026-10-29", "allowed": []}]
    inst = P.expand_slots(TPL, ab, MON, date(2026, 11, 1), MON)
    assert _dates(inst, "core") == []
    ab = [{"start": "2026-10-27", "end": "2026-10-29", "allowed": ["corpo libero"]}]
    inst = P.expand_slots(TPL, ab, MON, date(2026, 11, 1), MON)
    assert _dates(inst, "core") == [date(2026, 10, 27)]


def test_rpe_for_load_inverts_load():
    tgt = P.load_at(6.0, 45)
    assert P.rpe_for_load(tgt, 45) == 6.0
    assert P.rpe_for_load(1e9, 45) == P.RPE_RANGE[1]
    assert P.rpe_for_load(0, 45) == P.RPE_RANGE[0]


def test_allocate_keeps_day_intent_when_squeezed():
    # settimana troppo carica per il target: il livello scende per tutti, ma il
    # giorno "leggero" resta più leggero di quello "intenso" (era tutto RPE 5.5)
    planned = [("mar", "Corpo libero", 25, "leggera"), ("gio", "Corpo libero", 45, "intensa"),
               ("sab", "Ciclismo", 120, "media")]
    out = P.allocate(100, 0, [], planned)
    assert out["mar"]["rpe"] < out["sab"]["rpe"] < out["gio"]["rpe"]


def test_allocate_redistributes_and_clamps():
    planned = [("a", "Corpo libero", 45, "media"), ("b", "Ciclismo", 120, "media")]
    nom = sum(P.load_at(P.nominal_rpe(s, i), d) for _, s, d, i in planned)
    # budget esattamente uguale al nominale -> sedute tipo
    out = P.allocate(nom, 0, [], planned)
    assert all(abs(v["ratio"] - 1.0) < 0.02 for v in out.values())
    # già fatto molto -> sedute alleggerite, ma mai sotto il clamp
    out = P.allocate(nom, nom, [], planned)
    assert all(v["ratio"] == P.ALLOC_CLAMP[0] for v in out.values())
    assert out["a"]["rpe"] < 6.0
    # budget enorme -> clamp alto
    out = P.allocate(nom * 5, 0, [], planned)
    assert all(v["ratio"] == P.ALLOC_CLAMP[1] for v in out.values())


def test_needs_update_thresholds():
    assert P.needs_update(None, None, 6, 45)
    assert not P.needs_update(6.0, 45, 6.5, 48)
    assert P.needs_update(6.0, 45, 7.0, 45)
    assert P.needs_update(6.0, 45, 6.0, 60)


def test_sanitize_template_from_untrusted_ai():
    rid = {"core-pt"}
    raw = [{"id": "Bici!", "days": ["5", 6, 9], "pick": "one", "sport": "Ciclismo",
            "kind": "boh", "duration": "120", "routines": ["core-pt", "inventata"]},
           {"id": "bici", "days": [0], "sport": "Nuoto", "kind": "external"},
           {"sport": "", "days": [1]}, "spazzatura", {"sport": "Yoga", "days": []}]
    out = P.sanitize_template(raw, rid)
    assert [s["id"] for s in out] == ["bici", "bici-2"]          # id unici
    assert out[0]["days"] == [5, 6] and out[0]["pick"] == "one"
    assert out[0]["kind"] == "planned" and out[0]["duration"] == 120
    assert out[0]["routines"] == ["core-pt"]                     # niente routine inventate
    assert out[1]["kind"] == "external" and out[1]["pick"] == "each"


def test_sanitize_template_duration_and_indoor_guards():
    # i difetti visti nella precompilazione reale: nuoto a 0/5', seduta più corta
    # della routine inclusa, "indoor" su sport che non sono bici
    raw = [{"days": [0], "sport": "Nuoto", "kind": "external", "duration": 5, "indoor": True},
           {"days": [3], "sport": "Forza", "duration": 10, "routines": ["core-pt"]},
           {"days": [5], "sport": "Ciclismo", "duration": 120, "indoor": True}]
    out = P.sanitize_template(raw, {"core-pt": 19})
    assert out[0]["duration"] == P.DEFAULT_SLOT_MIN and out[0]["indoor"] is False
    assert out[1]["duration"] == 19                    # mai meno della routine
    assert out[2]["indoor"] is True


def test_sanitize_routines():
    out = P.sanitize_routines([{"name": "Core PT", "duration_min": "19", "content": "x"},
                               {"name": " "}, 3])
    assert out == [{"id": "core-pt", "name": "Core PT", "duration_min": 19,
                    "content": "x", "video_url": ""}]


def test_proposal_days():
    assert P.is_proposal_day(MON, MON)
    assert P.is_proposal_day(date(2026, 10, 27), MON)
    assert not P.is_proposal_day(date(2026, 10, 28), MON)
