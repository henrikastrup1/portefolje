#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
update_portfolio.py  ·  Tokenfri daglig opdatering af portfolio.json
====================================================================
Kalder yfinance-BIBLIOTEKET direkte (ikke via MCP/agent) -> 0 AI-tokens.
Køres af cron kl. 23:00. Læser portfolio.json, opdaterer de kursafhængige
felter pr. papir, skriver filen igen. Identitetsfelter (navn, ticker,
valuta, kat, antal, gak, flag, priskilde) røres ALDRIG af scriptet —
dem ændrer Oscar kun, når Henrik sender en ny Nordnet-CSV.

Felter scriptet beregner pr. papir:
  kurs_afhængige (kun hvis priskilde != "nordnet"):
    vaerdiDKK, afkastDKK, sidenU, udbDKK, udbPrAktie
  altid (hvis ticker findes):
    ret = {w1, m1, m3, ytd}

Guardrails (deterministiske — ingen AI):
  - priskilde=="nordnet": værdifelter FRYSES (beholdes), kun ret opdateres.
  - flag=="guard"        : udbytte beholdes som det er (yfinance upålideligt).
  - ticker uden data     : alt beholdes, ret sættes til null. ALDRIG udb=0.
  - .L (London)          : kurs er i pence -> divideres med 100.
  - SANITY: hvis seneste kurs afviger >25% fra median af seneste lukkekurser,
            bruges sidste fornuftige kurs i stedet, og der logges en advarsel.
"""

import json, sys, datetime as dt, statistics, pathlib
import pandas as pd
import yfinance as yf

HERE = pathlib.Path(__file__).resolve().parent
JSON_PATH = HERE / "portfolio.json"
SANITY_MAX_JUMP = 0.25          # 25% maks. dagsudsving uden split
FX_PAIRS = {"USD": "USDDKK=X", "NOK": "NOKDKK=X", "EUR": "EURDKK=X", "GBP": "GBPDKK=X"}

MÅNED = ["", "januar","februar","marts","april","maj","juni","juli",
         "august","september","oktober","november","december"]

def log(msg): print(f"[{dt.datetime.now():%H:%M:%S}] {msg}")

# ---------------------------------------------------------------- valutakurser
def hent_fx():
    fx = {"DKK": 1.0}
    for cur, tk in FX_PAIRS.items():
        try:
            s = yf.Ticker(tk).history(period="5d")["Close"].dropna()
            fx[cur] = float(s.iloc[-1])
        except Exception as e:
            log(f"ADVARSEL: kunne ikke hente {cur}-kurs ({e}); bruger 1.0")
            fx[cur] = fx.get(cur, 1.0)
    return fx

# ---------------------------------------------------------------- kurshistorik
def hent_historik(ticker):
    """Returnerer en renset Close-serie (pence/100 hvis London) eller None."""
    if not ticker:
        return None
    try:
        s = yf.Ticker(ticker).history(start="2025-12-15",
                                      end=str(dt.date.today() + dt.timedelta(days=1)))["Close"].dropna()
        if len(s) == 0:
            return None
        s.index = pd.to_datetime(s.index).tz_localize(None)
        if ticker.upper().endswith(".L"):     # London -> pence
            s = s / 100.0
        return s
    except Exception as e:
        log(f"ADVARSEL: kurshistorik fejlede for {ticker}: {e}")
        return None

def naermeste(serie, dato):
    s = serie[serie.index <= dato]
    if len(s):
        return float(s.iloc[-1])
    s2 = serie[serie.index > dato]
    return float(s2.iloc[0]) if len(s2) else None

def sikker_seneste(serie, navn):
    """Seneste kurs med sanity-tjek mod median af de seneste lukkekurser."""
    seneste = float(serie.iloc[-1])
    tail = serie.tail(6).iloc[:-1]            # de foregående lukkekurser
    if len(tail) >= 3:
        med = statistics.median([float(x) for x in tail])
        if med > 0 and abs(seneste / med - 1) > SANITY_MAX_JUMP:
            log(f"SANITY: '{navn}' seneste kurs {seneste:.2f} afviger "
                f">{int(SANITY_MAX_JUMP*100)}% fra median {med:.2f} — bruger forrige kurs.")
            return float(tail.iloc[-1])
    return seneste

def udbytte_ytd(ticker, aar):
    try:
        d = yf.Ticker(ticker).dividends
        if d is None or len(d) == 0:
            return 0.0
        d.index = pd.to_datetime(d.index).tz_localize(None)
        return float(d[(d.index >= f"{aar}-01-01") & (d.index <= f"{aar}-12-31")].sum())
    except Exception:
        return 0.0

# ---------------------------------------------------------------- hovedløkke
def main():
    data = json.loads(JSON_PATH.read_text(encoding="utf-8"))
    fx = hent_fx()
    today = pd.Timestamp(dt.date.today())
    aar = today.year
    vinduer = {"w1": today - pd.Timedelta(days=7),
               "m1": today - pd.Timedelta(days=30),
               "m3": today - pd.Timedelta(days=91),
               "ytd": pd.Timestamp(f"{aar}-01-02")}

    for h in data["holdings"]:
        navn = h.get("navn", "?")
        prisk = h.get("priskilde", "yfinance")
        serie = hent_historik(h.get("ticker", ""))

        # ---- ret{} (procentafkast) — altid, hvis vi har kurshistorik ----
        if serie is not None and len(serie) > 0:
            kurs_nu = sikker_seneste(serie, navn)
            ret = {}
            for k, d0 in vinduer.items():
                p0 = naermeste(serie, d0)
                ret[k] = round((kurs_nu / p0 - 1) * 100, 1) if p0 else None
            h["ret"] = ret
        else:
            kurs_nu = None
            h["ret"] = None                    # manglende ticker -> null (aldrig 0)

        # ---- kursafhængige værdifelter ----
        if prisk == "nordnet":
            # andelsklasse-mismatch: værdi FRYSES (kun ret opdateret ovenfor)
            continue
        if kurs_nu is None:
            continue                           # ingen kurs -> behold gamle værdier

        FXv = fx.get(h.get("valuta", "DKK"), 1.0)
        antal, gak = h.get("antal", 0), h.get("gak", 0)
        h["vaerdiDKK"] = round(antal * kurs_nu * FXv, 2)
        h["afkastDKK"] = round(antal * (kurs_nu - gak) * FXv, 2)
        h["sidenU"]    = round((kurs_nu / gak - 1) * 100, 2) if gak else h.get("sidenU", 0)

        # ---- udbytte ----
        if h.get("flag") == "guard":
            pass                               # behold guard-værdi (fx iShares ~150)
        else:
            ups = udbytte_ytd(h.get("ticker", ""), aar)
            h["udbPrAktie"] = round(ups, 4)
            h["udbDKK"]     = round(ups * antal * FXv, 0)

    # ---- meta ----
    data.setdefault("meta", {})["opdateret"] = f"{today.day}. {MÅNED[today.month]} {aar}"
    data["meta"]["valutakurser"] = (f"USD {fx['USD']:.2f} · NOK {fx['NOK']:.2f} · "
                                    f"EUR {fx['EUR']:.2f} → DKK")

    JSON_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    tot_v = sum(h.get("vaerdiDKK", 0) for h in data["holdings"])
    tot_u = sum(h.get("udbDKK", 0) for h in data["holdings"])
    log(f"OK · {len(data['holdings'])} papirer · værdi {tot_v:,.0f} kr · udbytte {tot_u:,.0f} kr"
        .replace(",", "."))

if __name__ == "__main__":
    main()
