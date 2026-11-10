"""Taste-neutral facts for the public site, one Claude pass per new event: format, whether it's a
real live-music listing (vs a party or tourist package), zone of town, and a one-line blurb in
English and Portuguese. Cached by event content (not taste), so editing taste.md doesn't redo it.
Model: MUSICFINDER_MODEL_DESCRIBE, default Haiku (~$0.15 per 1000 events)."""
from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor

from . import db, llm
from .models import Event

log = logging.getLogger(__name__)

BATCH = 40
WORKERS = 6     # batches in flight; the first run on an undescribed db has ~60
VERSION = "v1"   # bump to redo every event after changing the prompt or schema

FORMATS = ("roda", "show", "baile", "rua", "festival")
LISTINGS = ("music", "party", "tourist", "other")
ZONES = {
    "rio": {
        "centro": "Centro, Lapa, Cinelândia, Saúde, Gamboa, Santo Cristo, Praça Mauá, Estácio, Cidade Nova",
        "sul_centro": "Santa Teresa, Glória, Catete, Flamengo, Laranjeiras, Cosme Velho, Botafogo, Humaitá, Urca",
        "orla": "Leme, Copacabana, Arpoador, Ipanema, Leblon, Vidigal",
        "gavea": "Gávea, Jardim Botânico, Horto, Lagoa, São Conrado, Rocinha",
        "tijuca": "Tijuca, Praça da Bandeira, Maracanã, Vila Isabel, Grajaú, Andaraí, Alto da Boa Vista",
        "norte": "rest of the Zona Norte: São Cristóvão, Méier, Madureira, Oswaldo Cruz, Penha, Olaria, Irajá, Pilares...",
        "ilha": "Ilha do Governador, Paquetá",
        "barra": "Barra da Tijuca, Recreio, Joá, Jacarepaguá, Freguesia, Taquara, Vargem Grande, Cidade de Deus",
        "oeste": "Bangu, Realengo, Padre Miguel, Campo Grande, Santa Cruz, Guaratiba, Sulacap",
        "metro": "outside the city: Niterói, São Gonçalo, Baixada Fluminense (Duque de Caxias, Nova Iguaçu...)",
    },
    "sp": {
        "centro": "Sé, República, Luz, Bom Retiro, Brás, Liberdade, Santa Cecília, Vila Buarque, Barra Funda, Higienópolis, Campos Elíseos",
        "paulista": "Avenida Paulista, Bela Vista/Bixiga, Consolação, Jardins, Cerqueira César, Paraíso",
        "oeste": "Pinheiros, Vila Madalena, Sumaré, Perdizes, Pompeia, Lapa, Vila Leopoldina, Butantã, Água Branca",
        "sul": "Vila Mariana, Ibirapuera, Moema, Itaim Bibi, Vila Olímpia, Brooklin, Santo Amaro, Ipiranga, Interlagos, Campo Limpo",
        "leste": "Mooca, Belenzinho, Tatuapé, Penha, Carrão, Itaquera, São Mateus...",
        "norte": "Santana, Casa Verde, Tucuruvi, Vila Maria, Freguesia do Ó, Brasilândia...",
        "metro": "outside the city: ABC (Santo André, São Bernardo...), Guarulhos, Osasco...",
    },
}


def schema(city: str) -> dict:
    return {
        "type": "object",
        "properties": {"events": {"type": "array", "items": {
            "type": "object",
            "properties": {
                "key": {"type": "string"},
                "format": {"type": "string", "enum": list(FORMATS)},
                "listing": {"type": "string", "enum": list(LISTINGS)},
                "zone": {"type": "string", "enum": [*ZONES.get(city, {}), ""]},
                "neighborhood": {"type": "string"},
                "blurb_en": {"type": "string"},
                "blurb_pt": {"type": "string"},
            },
            "required": ["key", "format", "listing", "zone", "neighborhood", "blurb_en", "blurb_pt"],
            "additionalProperties": False}}},
        "required": ["events"], "additionalProperties": False,
    }


SYSTEM = """You label live-music listings in {city} for a public gig guide. Be neutral: describe,
don't judge taste. For each event:

format:
- roda: open roda / jam (musicians around a table, crowd around them), samba or choro in a bar or quintal
- show: a staged concert (theater, club, bar stage, concert hall, Sesc)
- baile: a dance night with live music (gafieira, forró, charme, samba-rock, baile)
- rua: outdoors on the street or in a square: bloco, cortejo, festa de rua, free open-air gig
- festival: multi-act festival, feira or day-long program

listing:
- music: live music is the point (whatever the genre, including funk, pop or electronic live acts)
- party: a DJ/club night or open-bar party where live music is incidental. A baile with a live
  band (forró, gafieira, samba-rock) is music, even with DJs between sets
- tourist: a packaged tourist product (bar crawl, pub crawl, boat party, "experience" packages,
  Candlelight/Fever-style candle concerts, samba-show dinners sold to tourists)
- other: not a music performance (theatre play, talk, class, workshop, exhibition, sports)

zone, by where the venue is (use what you know about the venue when the listing omits it):
{zones}
- "" if you can't tell.

neighborhood: the venue's bairro, properly capitalised ("Glória", not "GLORIA"), "" if unknown.

blurb_en / blurb_pt: one plain line (<= 110 chars) on who plays and what kind of music, natural
English and natural Brazilian Portuguese. No hype, no score, no date/time/venue (shown elsewhere).
Return one entry per input key."""


def _line(key: str, e: Event) -> str:
    bits = [f"[{key}] {e.title}"]
    if e.artists:
        bits.append("artists: " + ", ".join(e.artists[:6]))
    bits.append(f"venue: {e.venue}" + (f" ({e.neighborhood})" if e.neighborhood else ""))
    bits.append(e.start_datetime.strftime("%a %H:%M"))
    if e.is_free:
        bits.append("free")
    if e.genres:
        bits.append("tags: " + ",".join(e.genres))
    if e.description:
        bits.append("desc: " + e.description[:280].replace("\n", " "))
    return " | ".join(bits)


def cache_key(e: Event) -> str:
    return f"{VERSION}:{e.city}:{e.content_hash()}"


def lookup(e: Event) -> dict | None:
    return db.cache_get("describe", cache_key(e))


def describe(events: list[Event], city_name: str = "") -> None:
    """Fill the describe cache for events that aren't in it. Nothing is set on the events: the
    web output reads the cache, so `publish` (which loads events from the db) sees it too."""
    seen, todo = set(), []
    for e in events:
        k = cache_key(e)
        if k not in seen and db.cache_get("describe", k) is None:
            seen.add(k)
            todo.append(e)
    if not todo or not llm.available():
        return
    city = todo[0].city
    zones = "\n".join(f"- {z}: {desc}" for z, desc in ZONES.get(city, {}).items())
    log.info("describing %d events", len(todo))
    system = SYSTEM.format(city=city_name or city, zones=zones)
    chunks = [{f"e{j}": e for j, e in enumerate(todo[i:i + BATCH])} for i in range(0, len(todo), BATCH)]

    def call(chunk: dict[str, Event]):
        return llm.json_call(system=system, prompt="\n".join(_line(k, e) for k, e in chunk.items()),
                             schema=schema(city), max_tokens=12000, cache=False, task="describe")

    # API calls in threads; cache writes stay on this thread (one sqlite connection)
    with ThreadPoolExecutor(WORKERS) as pool:
        for chunk, out in zip(chunks, pool.map(call, chunks)):
            for item in (out or {}).get("events", []):
                e = chunk.get(item.pop("key", ""))
                if e is not None:
                    db.cache_put("describe", cache_key(e), item)
