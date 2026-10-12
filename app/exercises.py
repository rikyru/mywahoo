"""Bodyweight-exercise glossary + a linkifier.

The AI writes session descriptions in free text ("3x12 squat, 3x10 push up…").
We can't host video, so each known exercise links to a demonstration search and
carries short form cues. `linkify()` escapes a description and turns recognised
exercise names into clickable links, so "come si svolgono" is one tap away.
"""
import re
from markupsafe import Markup, escape

# canonical name -> (aliases, form cues). The demo link is a localized video
# search (robust: no API key, no dead video ids, works in Italian).
_EXERCISES: dict[str, tuple[list[str], str]] = {
    "Squat": (["squat", "squats"],
              "Schiena neutra, ginocchia in linea coi piedi, scendi col bacino indietro."),
    "Flessioni": (["flessioni", "piegamenti", "push up", "push-up", "pushup"],
                  "Corpo in linea testa-talloni, gomiti ~45°, addome contratto."),
    "Affondi": (["affondi", "affondo", "lunge", "lunges"],
                "Busto eretto, ginocchio anteriore sopra la caviglia, scendi controllato."),
    "Plank": (["plank"],
              "Avambracci a terra, corpo in linea, addome e glutei contratti, non cedere coi lombari."),
    "Plank laterale": (["plank laterale", "side plank"],
                       "Su un avambraccio, fianchi alti, corpo in linea."),
    "Burpee": (["burpee", "burpees"],
               "Squat → plank → (flessione) → salto; ritmo controllato."),
    "Mountain climber": (["mountain climber", "mountain climbers"],
                         "In posizione di plank, porta le ginocchia al petto alternando, bacino basso."),
    "Crunch": (["crunch", "crunches"],
               "Solleva le scapole arrotolando il busto, lombari a terra, niente slancio col collo."),
    "Ponte per i glutei": (["ponte glutei", "ponte per i glutei", "hip thrust", "glute bridge"],
                           "Spingi coi talloni, estendi le anche, stringi i glutei in alto."),
    "Dips": (["dips", "dip"],
             "Gomiti indietro, spalle basse, scendi fino a ~90° e risali."),
    "Jumping jack": (["jumping jack", "jumping jacks"],
                     "Salto aprendo gambe e braccia, atterra morbido."),
    "Russian twist": (["russian twist", "russian twists"],
                      "Seduto, busto inclinato, ruota il tronco lato a lato con l'addome."),
    "Superman": (["superman"],
                 "Prono, solleva insieme braccia e gambe, pausa breve in alto."),
    "Bird dog": (["bird dog"],
                 "Quadrupedia, estendi braccio e gamba opposti tenendo il bacino stabile."),
    "Wall sit": (["wall sit", "sedia al muro"],
                 "Schiena al muro, cosce parallele al pavimento, tieni la posizione."),
    "Calf raise": (["calf raise", "calf raises", "sollevamenti polpacci"],
                   "Sollevati sulle punte lentamente, pausa in alto, scendi controllato."),
    "Trazioni": (["trazioni", "pull up", "pull-up", "pullup"],
                 "Presa prona, scapole attive, mento sopra la sbarra senza slanci."),
    "Chin up": (["chin up", "chin-up", "chin ups"],
                "Presa supina, porta il petto alla sbarra."),
    "Pike push up": (["pike push up", "pike push-up"],
                     "A V rovesciata, piega i gomiti portando la testa verso terra (spalle)."),
    "High knees": (["high knees", "ginocchia alte", "skip alto"],
                   "Corsa sul posto portando le ginocchia all'altezza dell'anca."),
}

# alias (lowercase) -> canonical
_ALIAS = {a.lower(): name for name, (aliases, _) in _EXERCISES.items() for a in aliases}
# match longest aliases first so "plank laterale" wins over "plank"
_PATTERN = re.compile(
    r"\b(" + "|".join(re.escape(a) for a in sorted(_ALIAS, key=len, reverse=True)) + r")\b",
    re.IGNORECASE)


def canonical_names() -> list[str]:
    """The canonical exercise names, for nudging the AI to use them (so the
    how-to linkifier recognises what it writes)."""
    return list(_EXERCISES.keys())


# One line to append to the planning prompts so the AI writes recognisable names.
NUDGE = ("\n\nPer gli esercizi a corpo libero preferisci questi nomi standard "
         "quando l'esercizio corrisponde, così l'app può mostrarne la dimostrazione: "
         + ", ".join(_EXERCISES.keys()) + ".")


def demo_url(name: str) -> str:
    q = re.sub(r"\s+", "+", f"{name} esecuzione corretta")
    return f"https://www.youtube.com/results?search_query={q}"


def catalog() -> list[dict]:
    """The glossary for a reference page."""
    return [{"nome": name, "cues": cues, "url": demo_url(name)}
            for name, (_, cues) in sorted(_EXERCISES.items())]


def linkify(text: str) -> Markup:
    """Escape `text` and turn known exercise names into demo links with cue
    tooltips. Safe to render directly (returns Markup)."""
    if not text:
        return Markup("")
    safe = str(escape(text))   # escape first; we only inject our own anchors

    def repl(m: re.Match) -> str:
        shown = m.group(0)
        name = _ALIAS.get(shown.lower())
        if not name:
            return shown
        cues = _EXERCISES[name][1]
        return (f'<a href="{demo_url(name)}" target="_blank" rel="noopener" '
                f'class="ex-link" title="{escape(cues)}">{shown} ▶</a>')

    return Markup(_PATTERN.sub(repl, safe))
