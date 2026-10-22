"""Keyword genre tagging. Used for web filters and as the no-LLM ranking fallback."""
from __future__ import annotations

from .models import Event, norm

GENRES: dict[str, tuple[str, ...]] = {
    "samba": ("samba", "roda de samba", "partido alto", "pagode de mesa", "samba de raiz", "sambista",
              "escola de samba", "velha guarda", "cacique de ramos", "portela", "mangueira"),
    "choro": ("choro", "chorinho", "regional de choro", "roda de choro", "pixinguinha", "jacob do bandolim",
              "bandolim", "cavaquinho", "escola portatil"),
    "instrumental": ("instrumental", "quarteto", "quinteto", "trio", "big band", "orquestra de frevo",
                     "hermeto", "egberto"),
    "gafieira": ("gafieira", "baile", "samba rock"),
    "jazz": ("jazz", "bebop", "swing", "standards", "blue note", "bossa nova", "bossa"),
    "afro": ("afro", "afrobeat", "candomble", "jongo", "ijexa", "ancestral", "diaspora", "diasporica",
             "maracatu", "coco", "tambor", "batuque", "capoeira", "samba de roda", "cumbia", "salsa"),
    "sopros": ("flauta", "sax", "saxofone", "sopros", "trompete", "clarinete", "metais"),
    "mpb": ("mpb", "cancao", "cantautor", "musica popular brasileira", "tropicalia"),
    "forro": ("forro", "pe de serra", "sanfona", "xote", "baiao", "xaxado"),
    "frevo": ("frevo", "maracatu", "caboclinho"),
    "classical": ("concerto", "sinfonica", "filarmonica", "camara", "recital", "minimalis", "erudit",
                  "orquestra", "piano solo", "quarteto de cordas", "opera", "candlelight"),
    "rock": ("rock", "punk", "metal", "indie", "grunge", "hardcore"),
    "pop": ("pop", "turne", "tour"),
    "electronic": ("dj", "techno", "house", "eletronica", "rave", "edm", "trance"),
    "funk": ("funk carioca", "baile funk", "funk", "mc "),
    "sertanejo": ("sertanejo", "sertaneja", "modao", "piseiro", "arrocha"),
    "pagode_romantico": ("pagode romantico", "pagodao", "pagode 90"),
    "tribute": ("tributo", "cover", "tribute", "homenagem a", "covers", "a lenda de"),
    "hiphop": ("rap", "hip hop", "trap", "slam"),
    "reggae": ("reggae", "dub", "ska"),
    "blues_soul": ("blues", "soul", "r b", "motown", "gospel"),
}

LOVE = {"samba", "choro", "instrumental", "gafieira", "jazz", "afro", "sopros"}
INTERESTED = {"mpb", "forro", "frevo", "classical"}
DEPRIORITIZE = {"pagode_romantico", "sertanejo", "funk", "electronic", "tribute"}


def tag(ev: Event) -> list[str]:
    t = " " + norm(" ".join([ev.title, " ".join(ev.artists), ev.venue, ev.description[:600]])) + " "
    found = []
    for g, kws in GENRES.items():
        if any(f" {k}" in t for k in kws):
            found.append(g)
    # "pagode" alone: ambiguous; treat roda de pagode / pagode de mesa as samba
    if " pagode " in t and "samba" not in found and "pagode_romantico" not in found:
        found.append("pagode_romantico" if "romantic" in t else "samba")
    return found


# An open roda (musicians sit in around a table, audience stands around) vs a staged show.
# "samba de roda" is a Bahian genre, not a format, so it's stripped before matching.
RODA_KWS = (" roda de samba", " roda de choro", " roda de pagode", " roda de partido", " roda de forro",
            " roda de coco", " roda de jongo", " rodas de samba", " rodas de choro", " roda aberta",
            " na roda", " pra roda", " a roda ", " sua roda", " jam session", " canja ")


def kind_of(ev: Event) -> str:
    t = " " + norm(" ".join([ev.title, ev.description[:300]])) + " "
    t = t.replace(" samba de roda ", " ")
    title = norm(ev.title)
    if title.startswith(("roda ", "rodas ")) and not title.startswith(("roda de conversa", "roda de leitura")):
        return "roda"
    return "roda" if any(k in t for k in RODA_KWS) else "show"


def heuristic_score(ev: Event) -> tuple[int, str]:
    """Rough 0-100 score from genre tags. Used only when Claude is unavailable."""
    g = set(ev.genres or tag(ev))
    score = 45
    loved = g & LOVE
    liked = g & INTERESTED
    bad = g & DEPRIORITIZE
    score += 18 * min(len(loved), 2) + 8 * min(len(liked), 2) - 22 * min(len(bad), 2)
    score = max(0, min(100, score))
    parts = []
    if loved:
        parts.append("love: " + ", ".join(sorted(loved)))
    if liked:
        parts.append("interested: " + ", ".join(sorted(liked)))
    if bad:
        parts.append("deprioritized: " + ", ".join(sorted(bad)))
    return score, ("[keyword] " + "; ".join(parts)) if parts else "[keyword] no genre signal"
