#!/usr/bin/env python3
"""
notis.py — pushar en notis när en nyhämtad fil innehåller en kandidat.

Körs av GitHub Actions direkt efter fetch_odds.py. Skickar till ntfy.sh på
det ämne som ligger i hemligheten NTFY_AMNE. Ämnet skrivs aldrig ut.

TRÖSKLARNA MÅSTE VARA IDENTISKA med claude/dagslista.py i Claude-projektet.
Ändras den ena måste den andra ändras, annars säger notisen och listan
olika saker om samma match.

Dubblettskydd: nycklar för redan skickade kandidater sparas i
odds/_notiser.txt, som committas med övriga filer. Samma match pushas
därför bara en gång, även om jobbet kör var annan timme.
"""
import csv, glob, math, os, sys, urllib.request
from datetime import datetime, timezone, timedelta

MIN_EV       = 0.02      # samma som dagslista.py
MISSTANKT_EV = 0.08      # samma som dagslista.py
MAX_ODDS     = 8.0       # samma som dagslista.py
MARGINAL_MIN = 20        # minuter som minst måste återstå till avspark
FARSK_MIN    = 90        # strunta i filer äldre än så

LOGG = "odds/_notiser.txt"

SVENSKA = {'atg_se','betinia_se','betmgm_se','betsson','campobet_se','coolbet',
           'expekt_se','hajper_se','leovegas_se','mrgreen_se','nordicbet',
           'sport888_se','svenskaspel_se','unibet_se'}

def shin(odds):
    pi = [1/o for o in odds]; P = sum(pi)
    f = lambda z: [(math.sqrt(z*z + 4*(1-z)*p*p/P) - z)/(2*(1-z)) for p in pi]
    lo, hi = 0.0, 0.5
    for _ in range(200):
        m = (lo+hi)/2
        lo, hi = (m, hi) if sum(f(m)) > 1 else (lo, m)
    s = f((lo+hi)/2); t = sum(s)
    return [x/t for x in s]

def multiplikativ(odds):
    pi = [1/o for o in odds]; P = sum(pi)
    return [p/P for p in pi]

def power(odds):
    pi = [1/o for o in odds]
    lo, hi = 0.5, 2.0
    for _ in range(200):
        k = (lo+hi)/2
        lo, hi = (k, hi) if sum(p**k for p in pi) > 1 else (lo, k)
    k = (lo+hi)/2; s = [p**k for p in pi]; t = sum(s)
    return [x/t for x in s]

def las_logg():
    try:
        with open(LOGG, encoding="utf-8") as f:
            return {rad.strip() for rad in f if rad.strip()}
    except FileNotFoundError:
        return set()

def hitta_kandidater(fil, nu):
    rows = list(csv.DictReader(open(fil, encoding="utf-8-sig")))
    matcher = {}
    for r in rows:
        matcher.setdefault((r['liga'], r['avspark_utc'], r['hemmalag'], r['bortalag']), []).append(r)
    grans = nu + timedelta(minutes=MARGINAL_MIN)
    ut = []
    for (liga, avspark_txt, hem, borta), rs in matcher.items():
        try:
            avspark = datetime.fromisoformat(avspark_txt.replace("Z", "+00:00"))
        except ValueError:
            continue
        if avspark < grans:
            continue
        utfall = sorted({r['utfall'] for r in rs})
        pin = {r['utfall']: float(r['odds']) for r in rs if r['bookmaker'] == 'pinnacle'}
        if len(pin) != len(utfall):
            continue
        o = [pin[u] for u in utfall]
        p3 = (shin(o), multiplikativ(o), power(o))
        for i, u in enumerate(utfall):
            svenska = [(float(r['odds']), r['bookmaker']) for r in rs
                       if r['utfall'] == u and r['bookmaker'] in SVENSKA]
            if not svenska:
                continue
            bast, bok = max(svenska)
            if bast > MAX_ODDS:
                continue
            evs = [p[i]*bast - 1 for p in p3]
            if min(evs) < MIN_EV:
                continue
            ex = next((float(r['odds']) for r in rs
                       if r['utfall'] == u and r['bookmaker'] == 'expekt_se'), None)
            p = p3[0][i]
            kelly = (p*bast - 1)/(bast - 1)
            ut.append({
                "nyckel": f"{avspark_txt}|{hem}|{borta}|{u}",
                "avspark": avspark, "hem": hem, "borta": borta, "spel": u,
                "odds": bast, "bolag": bok, "ev": min(evs),
                "insats": min(0.25*kelly, 0.02)*100,
                "expekt_ok": bool(ex) and (p*ex - 1) >= MIN_EV,
                "misstankt": min(evs) >= MISSTANKT_EV})
    return ut

def main():
    amne = os.environ.get("NTFY_AMNE", "").strip()
    if not amne:
        print("NTFY_AMNE saknas – hoppar över notis.", file=sys.stderr)
        return 0

    filer = sorted(glob.glob("odds/odds_*.csv"))
    if not filer:
        print("Inga oddsfiler – inget att pusha."); return 0
    fil = filer[-1]
    nu = datetime.now(timezone.utc)
    try:
        t = datetime.strptime(os.path.basename(fil)[5:-4], "%Y-%m-%d_%H%M").replace(tzinfo=timezone.utc)
    except ValueError:
        print(f"Kan inte tolka tidsstämpel i {fil} – hoppar över."); return 0
    alder = (nu - t).total_seconds()/60
    if not (0 <= alder <= FARSK_MIN):
        print(f"Senaste filen är {alder:.0f} min gammal – ingen ny hämtning att pusha."); return 0

    skickade = las_logg()
    kandidater = [k for k in hitta_kandidater(fil, nu) if k["nyckel"] not in skickade]
    if not kandidater:
        print("Inga nya kandidater att pusha."); return 0

    kandidater.sort(key=lambda k: k["avspark"])
    rader = []
    for k in kandidater:
        märke = " [EXPEKT]" if k["expekt_ok"] else ""
        varning = "  ?? MISSTANKT STOR KANT" if k["misstankt"] else ""
        rader.append(f"{k['avspark']:%H:%M} UTC {k['hem']}-{k['borta']}\n"
                     f"{k['spel']} {k['odds']:.2f} @ {k['bolag']}{märke}\n"
                     f"EV {k['ev']*100:+.1f} %  insats {k['insats']:.2f} %{varning}")
    kropp = "\n\n".join(rader)
    titel = f"Betbot: {len(kandidater)} kandidat{'er' if len(kandidater) > 1 else ''}"

    req = urllib.request.Request(
        f"https://ntfy.sh/{amne}", data=kropp.encode("utf-8"), method="POST",
        headers={"Title": titel, "Priority": "default", "Tags": "soccer"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            print(f"Notis skickad ({r.status}) – {len(kandidater)} kandidat(er).")
    except Exception as e:
        print(f"Notisen gick inte fram: {e}", file=sys.stderr)
        return 0                      # ska aldrig fälla hela jobbet

    os.makedirs("odds", exist_ok=True)
    with open(LOGG, "a", encoding="utf-8") as f:
        for k in kandidater:
            f.write(k["nyckel"] + "\n")
    print(f"{len(kandidater)} nyckel/nycklar tillagda i {LOGG}.")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
