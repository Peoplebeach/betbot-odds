#!/usr/bin/env python3
"""
fetch_odds.py — kvotsnål oddshämtning, avsedd att köras av GitHub Actions
=========================================================================

Skriver exakt samma CSV-schema som betbots befintliga bat-fil, så filerna är
utbytbara och kan läsas av samma inläsning:

    hamtad_utc, liga, avspark_utc, hemmalag, bortalag,
    bookmaker, uppdaterad, marknad, utfall, odds

Varför den finns
----------------
Bat-filen kan bara köras när datorn är igång. Matcher spelas på kvällar och
helger då den är avstängd, så priserna nära avspark fångas aldrig — och det är
just de priserna CLV-mätningen behöver. Detta skript körs av GitHub Actions på
schema, utan att någon dator behöver vara på.

Kvotlogik (viktigast i hela filen)
----------------------------------
The Odds API kostar krediter enligt:

    krediter = antal_ligor x antal_regioner x antal_marknader

Med 7 ligor, 2 regioner och 1 marknad blir det 14 krediter per körning. Vid 500
krediter i månaden räcker det till ~35 körningar — långt under vad som behövs
för bra täckning nära avspark.

Lösningen: `/events`-anropet är gratis. Skriptet frågar först vilka matcher som
finns, och hämtar odds ENDAST för de ligor som har en match inom fönstret. Under
uppehåll i kalendern kostar en körning då noll krediter.

Anrop:
    python3 fetch_odds.py --ut odds/                 # normal körning
    python3 fetch_odds.py --ut odds/ --fonster 3     # bara matcher inom 3 h
    python3 fetch_odds.py --ut odds/ --torrkor       # visa kostnad, hämta inget
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

BAS = "https://api.the-odds-api.com/v4"

# Samma sju ligor som bat-filen hämtar. Av dessa ingår soccer_epl, efl_champ,
# spain_la_liga, italy_serie_a och germany_bundesliga i modellens universum.
# Allsvenskan och CL hämtas men modellen är inte tränad på dem.
LIGOR = [
    "soccer_epl",
    "soccer_efl_champ",
    "soccer_spain_la_liga",
    "soccer_italy_serie_a",
    "soccer_germany_bundesliga",
    "soccer_uefa_champs_league",
    "soccer_sweden_allsvenskan",
]

# Varje region kostar en kredit per liga. Håll listan så kort som möjligt.
REGIONER = os.environ.get("ODDS_REGIONER", "eu,uk")
MARKNADER = os.environ.get("ODDS_MARKNADER", "h2h")

KOLUMNER = ["hamtad_utc", "liga", "avspark_utc", "hemmalag", "bortalag",
            "bookmaker", "uppdaterad", "marknad", "utfall", "odds"]


def hamta(url: str) -> tuple[list | dict, dict]:
    """GET med JSON-svar. Returnerar (data, svarsheaders).

    Allt som går fel — HTTP-fel, nätverksfel, trasig JSON — kastas som
    RuntimeError med läsbart meddelande. Anroparen ska aldrig behöva skilja på
    feltyperna, och en stackspårning i en Actions-logg hjälper ingen.
    """
    rensad = url.split("?")[0]
    try:
        with urllib.request.urlopen(url, timeout=60) as r:
            return json.loads(r.read().decode("utf-8")), dict(r.headers)
    except urllib.error.HTTPError as e:
        kropp = e.read().decode("utf-8", "replace")[:300]
        hint = " (kontrollera ODDS_API_KEY)" if e.code in (401, 403) else ""
        raise RuntimeError(f"HTTP {e.code} för {rensad}{hint}: {kropp}") from e
    except urllib.error.URLError as e:
        raise RuntimeError(f"nätverksfel mot {rensad}: {e.reason}") from e
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        raise RuntimeError(f"ogiltigt svar från {rensad}: {e}") from e
    except OSError as e:
        raise RuntimeError(f"anslutningsfel mot {rensad}: {e}") from e


def kommande(liga: str, nyckel: str) -> list[dict]:
    """Lista matcher för en liga. Detta anrop är gratis."""
    url = f"{BAS}/sports/{liga}/events?apiKey={nyckel}"
    data, _ = hamta(url)
    return data if isinstance(data, list) else []


def odds_for(liga: str, nyckel: str) -> tuple[list[dict], str | None]:
    url = (f"{BAS}/sports/{liga}/odds?apiKey={nyckel}"
           f"&regions={REGIONER}&markets={MARKNADER}&oddsFormat=decimal")
    data, h = hamta(url)
    return (data if isinstance(data, list) else []), h.get("x-requests-remaining")


def rader_ur(match: dict, liga: str, nu: str):
    """Platta ut ett matchobjekt till CSV-rader, ett pris per rad."""
    for bok in match.get("bookmakers", []):
        for mark in bok.get("markets", []):
            for utf in mark.get("outcomes", []):
                yield {
                    "hamtad_utc": nu,
                    "liga": liga,
                    "avspark_utc": match.get("commence_time", ""),
                    "hemmalag": match.get("home_team", ""),
                    "bortalag": match.get("away_team", ""),
                    "bookmaker": bok.get("key", ""),
                    "uppdaterad": mark.get("last_update") or bok.get("last_update", ""),
                    "marknad": mark.get("key", ""),
                    "utfall": utf.get("name", ""),
                    "odds": utf.get("price", ""),
                }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--ut", default="odds", help="katalog att skriva CSV till")
    ap.add_argument("--fonster", type=float, default=6.0,
                    help="hämta odds för ligor med match inom så här många timmar")
    ap.add_argument("--torrkor", action="store_true",
                    help="visa vad som skulle hämtas, förbruka inga krediter")
    args = ap.parse_args()

    nyckel = os.environ.get("ODDS_API_KEY", "").strip()
    if not nyckel:
        print("FEL: miljövariabeln ODDS_API_KEY saknas.", file=sys.stderr)
        return 2

    nu_dt = datetime.now(timezone.utc)
    grans = nu_dt + timedelta(hours=args.fonster)
    nu = nu_dt.strftime("%Y-%m-%dT%H:%M:%SZ")

    # --- Steg 1: gratis kartläggning av vilka ligor som är aktuella ---
    aktuella: list[str] = []
    misslyckade: list[str] = []
    for liga in LIGOR:
        try:
            ev = kommande(liga, nyckel)
        except RuntimeError as e:
            print(f"  {liga}: kunde inte listas – {e}", file=sys.stderr)
            misslyckade.append(liga)
            continue
        traffar = 0
        for m in ev:
            ct = m.get("commence_time", "")
            try:
                t = datetime.fromisoformat(ct.replace("Z", "+00:00"))
            except ValueError:
                continue
            if nu_dt <= t <= grans:
                traffar += 1
        if traffar:
            aktuella.append(liga)
        print(f"  {liga:<28} {traffar} match(er) inom {args.fonster:g} h")

    antal_reg = len([r for r in REGIONER.split(",") if r])
    antal_mark = len([m for m in MARKNADER.split(",") if m])
    kostnad = len(aktuella) * antal_reg * antal_mark

    # Kunde ingen liga alls listas är det ett fel — inte ett lugnt besked.
    # Utan detta ser en ogiltig nyckel eller ett nere API exakt likadant ut som
    # "inga matcher just nu": grön bock och tystnad. Det är samma feltyp som
    # gjorde att prognosloggen verkade fungera medan den stod stilla.
    if misslyckade and len(misslyckade) == len(LIGOR):
        print("\nFEL: ingen liga kunde listas. Kontrollera att ODDS_API_KEY är "
              "giltig och att API:et svarar. Detta är INTE samma sak som att "
              "det saknas matcher.", file=sys.stderr)
        return 1
    if misslyckade:
        print(f"\nVARNING: {len(misslyckade)} av {len(LIGOR)} ligor kunde inte "
              f"listas: {', '.join(misslyckade)}", file=sys.stderr)

    if not aktuella:
        print(f"\nInga matcher inom {args.fonster:g} timmar "
              f"({len(LIGOR)} ligor kontrollerade, alla svarade). "
              "Inga krediter förbrukade.")
        return 0

    print(f"\nHämtar odds för {len(aktuella)} liga(or). "
          f"Beräknad kostnad: {kostnad} krediter.")
    if args.torrkor:
        print("Torrkörning – avslutar utan att hämta.")
        return 0

    # --- Steg 2: hämta odds, endast för aktuella ligor ---
    rader: list[dict] = []
    kvar = None
    for liga in aktuella:
        try:
            data, kvar = odds_for(liga, nyckel)
        except RuntimeError as e:
            print(f"  {liga}: hämtning misslyckades – {e}", file=sys.stderr)
            continue
        n = 0
        for match in data:
            ct = match.get("commence_time", "")
            try:
                t = datetime.fromisoformat(ct.replace("Z", "+00:00"))
            except ValueError:
                continue
            if not (nu_dt <= t <= grans):
                continue            # spara bara matcher i fönstret
            for r in rader_ur(match, liga, nu):
                rader.append(r)
                n += 1
        print(f"  {liga:<28} {n} prisrader")

    if not rader:
        print("\nInga prisrader att spara.")
        return 0

    utk = Path(args.ut)
    utk.mkdir(parents=True, exist_ok=True)
    fil = utk / f"odds_{nu_dt:%Y-%m-%d_%H%M}.csv"
    with fil.open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=KOLUMNER, quoting=csv.QUOTE_ALL)
        w.writeheader()
        w.writerows(rader)

    print(f"\nKLART. {len(rader)} prisrader sparade i {fil}")
    if kvar is not None:
        print(f"Anrop kvar denna månad: {kvar}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
