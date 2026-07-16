"""
Signaal-bot v5: scant AEX + S&P 500 voor een top-selectie, laat je via Telegram
zelf aandelen volgen met /track en /untrack, en geeft je met /data op elk moment
een verse check -- zonder dat je gespamd wordt met een te strak tijdschema.

Voert ZELF NOOIT orders uit -- alleen meldingen, jij beslist en handelt zelf.

Benodigdheden:
    pip install yfinance ta requests matplotlib lxml

Telegram commando's (stuur ze gewoon in je Telegram-chat met de bot):
    /track nvidia   -> begint met volgen van dat aandeel
    /untrack nvidia -> stopt met volgen
    /list           -> toont wat je nu volgt
    /data           -> vraag nu meteen een verse check op (geen wachten op schema)
    /investigate nvidia -> cijfermatig onderzoeksrapport over een bedrijf
                           (omzet, winst, marge, waardering, schuld, lange termijn)
    /help           -> toont deze commando's

Draaien:
    python signal_bot.py --loop
        Laat 'm continu draaien:
        - commando's ('/track', '/data', etc.) worden elke 30 seconden gecheckt
          (dit kost niks, er wordt alleen iets teruggestuurd als jij een commando stuurt)
        - automatische controle (gevolgde aandelen + AEX/S&P-scan) gebeurt standaard
          elke 6 uur (AUTO_CHECK_INTERVAL_HOURS hieronder aan te passen)

    python signal_bot.py
        Draait alles precies 1x en stopt (handig om te testen).
"""

import os
import io
import json
import time
import argparse
from datetime import datetime

import requests
import pandas as pd
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import yfinance as yf
from ta.momentum import RSIIndicator
from ta.trend import SMAIndicator
from ta.volatility import BollingerBands

# ---------------------------------------------------------------------------
# CONFIGURATIE
# ---------------------------------------------------------------------------

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN") or "VUL_HIER_JE_TOKEN_IN"
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID") or "VUL_HIER_JE_CHAT_ID_IN"
print(f"[DEBUG] TELEGRAM_TOKEN ingesteld: {'JA' if 'VUL_HIER' not in TELEGRAM_TOKEN else 'NEE (nog placeholder!)'} (lengte {len(TELEGRAM_TOKEN)})")
print(f"[DEBUG] TELEGRAM_CHAT_ID ingesteld: {'JA' if 'VUL_HIER' not in TELEGRAM_CHAT_ID else 'NEE (nog placeholder!)'}")

INCLUDE_AEX = True
INCLUDE_SP500 = True
TOP_N = 5
AUTO_CHECK_INTERVAL_HOURS = 6      # hoe vaak de ZWARE AEX/S&P-scan draait
TRACKED_CHECK_INTERVAL_MINUTES = 5  # hoe vaak JOUW gevolgde aandelen gecheckt worden (lichter, dus mag vaker)

SETTINGS = {
    "pct_change_threshold": 5.0,      # drempel voor de grote AEX/S&P-scan
    "safety_pct_threshold": 3.0,      # strenger drempel voor JOUW gevolgde aandelen
    "rsi_period": 14,
    "rsi_overbought": 70,
    "rsi_oversold": 30,
    "sma_short": 20,
    "sma_long": 50,
    "high_low_window": 10,   # ~2 handelsweken -- voor dag/week-trading, niet maanden/jaren
    "bb_window": 20,
    "bb_std": 2,
    "lookback_days": 120,
    "news_items_per_ticker": 2,
    "batch_size": 50,
}

STATE_FILE = "signal_bot_state.json"
TRACKED_FILE = "tracked_tickers.json"
OFFSET_FILE = "telegram_offset.json"
LAST_AUTO_FILE = "last_auto_run.json"          # laatste zware AEX/S&P-scan
LAST_TRACKED_FILE = "last_tracked_run.json"    # laatste check van jouw /track-lijst

def should_run_auto():
    if not os.path.exists(LAST_AUTO_FILE):
        return True
    with open(LAST_AUTO_FILE) as f:
        last = json.load(f).get("last_auto", 0)
    return (time.time() - last) >= AUTO_CHECK_INTERVAL_HOURS * 3600

def mark_auto_done():
    with open(LAST_AUTO_FILE, "w") as f:
        json.dump({"last_auto": time.time()}, f)

def should_run_tracked():
    if not os.path.exists(LAST_TRACKED_FILE):
        return True
    with open(LAST_TRACKED_FILE) as f:
        last = json.load(f).get("last_tracked", 0)
    return (time.time() - last) >= TRACKED_CHECK_INTERVAL_MINUTES * 60

def mark_tracked_done():
    with open(LAST_TRACKED_FILE, "w") as f:
        json.dump({"last_tracked": time.time()}, f)

AEX_TICKERS = [
    "ASML.AS", "SHELL.AS", "UNA.AS", "INGA.AS", "ADYEN.AS", "PRX.AS", "AD.AS",
    "MT.AS", "REN.AS", "AGN.AS", "RAND.AS", "WKL.AS", "HEIA.AS", "PHIA.AS",
    "NN.AS", "ASM.AS", "IMCD.AS", "BESI.AS", "EXO.AS", "ABN.AS",
    "AALB.AS", "KPN.AS", "DSFIR.AS", "UMG.AS",
]

def get_sp500_tickers():
    fallback = [
        "AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "META", "BRK-B", "TSLA",
        "AVGO", "JPM", "LLY", "V", "XOM", "UNH", "MA", "COST", "HD", "PG",
    ]
    try:
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
        resp = requests.get("https://en.wikipedia.org/wiki/List_of_S%26P_500_companies", headers=headers, timeout=15)
        resp.raise_for_status()
        tables = pd.read_html(io.StringIO(resp.text))
        symbols = tables[0]["Symbol"].tolist()
        return [s.replace(".", "-") for s in symbols]
    except Exception as e:
        print(f"[WAARSCHUWING] Kon S&P 500-lijst niet ophalen ({e}), gebruik fallback-lijst.")
        return fallback

# ---------------------------------------------------------------------------
# TELEGRAM: BERICHTEN STUREN
# ---------------------------------------------------------------------------

def send_telegram_text(message):
    if "VUL_HIER" in TELEGRAM_TOKEN or "VUL_HIER" in TELEGRAM_CHAT_ID:
        print("[WAARSCHUWING] Telegram niet geconfigureerd. Bericht:")
        print(message)
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    try:
        r = requests.post(url, data={"chat_id": TELEGRAM_CHAT_ID, "text": message,
                                      "disable_web_page_preview": True}, timeout=15)
        r.raise_for_status()
    except Exception as e:
        print(f"[FOUT] Telegram tekst versturen mislukt: {e}")

def send_telegram_photo(image_bytes, caption):
    if "VUL_HIER" in TELEGRAM_TOKEN or "VUL_HIER" in TELEGRAM_CHAT_ID:
        print(f"[WAARSCHUWING] Telegram niet geconfigureerd. (Grafiek niet verstuurd)\n{caption}")
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendPhoto"
    try:
        files = {"photo": ("chart.png", image_bytes, "image/png")}
        data = {"chat_id": TELEGRAM_CHAT_ID, "caption": caption[:1024]}
        r = requests.post(url, data=data, files=files, timeout=30)
        r.raise_for_status()
    except Exception as e:
        print(f"[FOUT] Telegram foto versturen mislukt: {e}")
        send_telegram_text(caption)

# ---------------------------------------------------------------------------
# TELEGRAM: COMMANDO'S ONTVANGEN
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# STATE (welke signalen zijn al gemeld, voorkomt dubbele meldingen)
# ---------------------------------------------------------------------------

def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            return json.load(f)
    return {}

def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f)

def load_offset():
    if os.path.exists(OFFSET_FILE):
        with open(OFFSET_FILE) as f:
            return json.load(f).get("offset", 0)
    return 0

def save_offset(offset):
    with open(OFFSET_FILE, "w") as f:
        json.dump({"offset": offset}, f)

def get_telegram_updates(offset):
    if "VUL_HIER" in TELEGRAM_TOKEN:
        return []
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates"
    try:
        r = requests.get(url, params={"offset": offset, "timeout": 0}, timeout=15)
        r.raise_for_status()
        return r.json().get("result", [])
    except Exception as e:
        print(f"[FOUT] Telegram updates ophalen mislukt: {e}")
        return []

def load_tracked():
    if os.path.exists(TRACKED_FILE):
        with open(TRACKED_FILE) as f:
            return json.load(f)
    return []

def save_tracked(tracked):
    with open(TRACKED_FILE, "w") as f:
        json.dump(tracked, f)

def resolve_ticker(name):
    """Zoekt een bedrijfsnaam op en geeft (ticker, mooie_naam) terug, of (None, None)."""
    try:
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
        url = "https://query2.finance.yahoo.com/v1/finance/search"
        r = requests.get(url, params={"q": name, "quotesCount": 5, "newsCount": 0},
                          headers=headers, timeout=10)
        r.raise_for_status()
        quotes = r.json().get("quotes", [])
        for q in quotes:
            if q.get("quoteType") == "EQUITY":
                return q.get("symbol"), q.get("shortname") or q.get("longname") or name
        if quotes:
            q = quotes[0]
            return q.get("symbol"), q.get("shortname") or name
    except Exception as e:
        print(f"[FOUT] Ticker opzoeken mislukt voor '{name}': {e}")
    return None, None

def handle_track(name):
    if not name:
        send_telegram_text("Gebruik: /track <naam>, bijvoorbeeld: /track nvidia")
        return
    ticker, resolved_name = resolve_ticker(name)
    if not ticker:
        send_telegram_text(f"Kon geen aandeel vinden voor '{name}'. Probeer de volledige naam of het tickersymbool (bv. NVDA).")
        return
    tracked = load_tracked()
    if any(t["ticker"] == ticker for t in tracked):
        send_telegram_text(f"'{resolved_name}' ({ticker}) wordt al gevolgd.")
        return

    entry_price = get_current_price(ticker)
    tracked.append({
        "ticker": ticker,
        "name": resolved_name,
        "entry_price": entry_price,
        "entry_date": datetime.now().strftime("%Y-%m-%d %H:%M"),
    })
    save_tracked(tracked)

    prijs_tekst = f" tegen een koers van {entry_price:.2f}." if entry_price is not None else " (koers kon niet direct opgehaald worden)."
    send_telegram_text(
        f"✅ '{resolved_name}' ({ticker}) wordt nu gevolgd{prijs_tekst}\n"
        f"Je krijgt een melding bij koersschommelingen >{SETTINGS['safety_pct_threshold']}% (in een dag) "
        f"of technische signalen -- steeds met je eigen winst/verlies sinds tracking erbij.\n"
        f"Dit wordt elke {TRACKED_CHECK_INTERVAL_MINUTES} minuten gecheckt."
    )

def handle_untrack(name):
    if not name:
        send_telegram_text("Gebruik: /untrack <naam>, bijvoorbeeld: /untrack nvidia")
        return
    tracked = load_tracked()
    name_lower = name.lower()
    match = None
    for t in tracked:
        if name_lower in t["name"].lower() or name_lower == t["ticker"].lower():
            match = t
            break
    if not match:
        send_telegram_text(f"'{name}' stond niet in je gevolgde lijst.")
        return
    tracked = [t for t in tracked if t["ticker"] != match["ticker"]]
    save_tracked(tracked)
    send_telegram_text(f"🛑 '{match['name']}' ({match['ticker']}) wordt niet meer gevolgd.")

def handle_list():
    tracked = load_tracked()
    if not tracked:
        send_telegram_text("Je volgt momenteel niks. Gebruik /track <naam> om te beginnen.")
        return
    lines = ["Je volgt momenteel:"]
    for t in tracked:
        entry_price = t.get("entry_price")
        entry_date = t.get("entry_date")
        line = f"- {t['name']} ({t['ticker']})"
        if entry_price is not None:
            current_price = get_current_price(t["ticker"])
            if current_price is not None:
                pct = (current_price - entry_price) / entry_price * 100
                teken = "+" if pct >= 0 else ""
                line += f": instap {entry_price:.2f} ({entry_date}) -> nu {current_price:.2f} ({teken}{pct:.1f}%)"
            else:
                line += f": instap {entry_price:.2f} ({entry_date})"
        lines.append(line)
    send_telegram_text("\n".join(lines))

def handle_help():
    send_telegram_text(
        "Commando's:\n"
        "/track <naam> -- begin met volgen, bv: /track nvidia\n"
        "/untrack <naam> -- stop met volgen\n"
        "/list -- toon wat je nu volgt\n"
        "/data -- vraag nu meteen een verse check op (gevolgde aandelen + marktscan)\n"
        "/investigate <naam> -- cijfermatig onderzoeksrapport (omzet, winst, marge,\n"
        "    waardering, schuld, lange termijn) -- bv: /investigate asml\n"
        "/help -- toon dit bericht\n\n"
        f"Automatisch wordt er ook elke {AUTO_CHECK_INTERVAL_HOURS} uur gecheckt, "
        "dus /data is alleen nodig als je tussendoor iets wil weten."
    )

def handle_data():
    send_telegram_text("🔎 Even data ophalen, momentje...")
    check_tracked_tickers(on_demand=True)
    run_scan(on_demand=True)

# ---------------------------------------------------------------------------
# ONDERZOEK (/investigate) -- puur cijfermatig bedrijfsrapport, geen AI-oordeel
# ---------------------------------------------------------------------------

def get_fundamentals(ticker):
    try:
        return yf.Ticker(ticker).info
    except Exception as e:
        print(f"[FOUT] fundamentals ophalen mislukt voor {ticker}: {e}")
        return None

def handle_investigate(name):
    if not name:
        send_telegram_text("Gebruik: /investigate <naam>, bijvoorbeeld: /investigate asml")
        return
    ticker, resolved_name = resolve_ticker(name)
    if not ticker:
        send_telegram_text(f"Kon geen aandeel vinden voor '{name}'. Probeer de volledige naam of het tickersymbool.")
        return

    send_telegram_text(f"🔬 Bezig met onderzoeken van {resolved_name} ({ticker}), momentje...")

    info = get_fundamentals(ticker)
    if not info:
        send_telegram_text(f"Kon geen bedrijfsdata ophalen voor {resolved_name} ({ticker}).")
        return

    lines = [f"🔬 Onderzoek: {resolved_name} ({ticker})", ""]

    sector = info.get("sector")
    industry = info.get("industry")
    if sector:
        lines.append(f"Sector: {sector}" + (f" / {industry}" if industry else ""))

    price = info.get("currentPrice") or info.get("regularMarketPrice")
    market_cap = info.get("marketCap")
    currency = info.get("currency", "")
    if price:
        lines.append(f"Huidige koers: {price:.2f} {currency}")
    if market_cap:
        lines.append(f"Marktkapitalisatie: {market_cap / 1e9:.1f} miljard {currency}")

    revenue = info.get("totalRevenue")
    revenue_growth = info.get("revenueGrowth")
    profit_margin = info.get("profitMargins")
    net_income = info.get("netIncomeToCommon")

    lines.append("")
    lines.append("Omzet & winst:")
    if revenue:
        lines.append(f"- Omzet (laatste 12 mnd): {revenue / 1e9:.2f} miljard")
    if revenue_growth is not None:
        lines.append(f"- Omzetgroei (jaar-op-jaar): {revenue_growth * 100:.1f}%")
    if net_income is not None:
        lines.append(f"- Nettowinst: {net_income / 1e9:.2f} miljard" if net_income >= 0
                      else f"- Nettoverlies: {abs(net_income) / 1e9:.2f} miljard")
    if profit_margin is not None:
        lines.append(f"- Winstmarge: {profit_margin * 100:.1f}%")
    if revenue is None and net_income is None:
        lines.append("- Geen omzet/winstcijfers beschikbaar")

    trailing_pe = info.get("trailingPE")
    forward_pe = info.get("forwardPE")
    peg = info.get("pegRatio")

    lines.append("")
    lines.append("Waardering:")
    if trailing_pe:
        lines.append(f"- K/W-ratio (trailing): {trailing_pe:.1f}")
    if forward_pe:
        lines.append(f"- K/W-ratio (forward): {forward_pe:.1f}")
    if peg:
        lines.append(f"- PEG-ratio: {peg:.2f}")
    if not trailing_pe and not forward_pe:
        lines.append("- Geen waarderingscijfers beschikbaar (bv. bedrijf maakt geen winst)")

    debt_to_equity = info.get("debtToEquity")
    total_debt = info.get("totalDebt")
    total_cash = info.get("totalCash")

    lines.append("")
    lines.append("Schuldpositie:")
    if debt_to_equity is not None:
        lines.append(f"- Schuld/eigen vermogen: {debt_to_equity:.0f}%")
    if total_debt:
        lines.append(f"- Totale schuld: {total_debt / 1e9:.2f} miljard")
    if total_cash:
        lines.append(f"- Kaspositie: {total_cash / 1e9:.2f} miljard")

    dividend_yield = info.get("dividendYield")
    lines.append("")
    lines.append("Dividend:")
    lines.append(f"- Dividendrendement: {dividend_yield * 100:.2f}%" if dividend_yield
                  else "- Geen dividend uitgekeerd")

    hist = download_single(ticker, "5y")
    perf_texts = []
    if hist is not None and not hist.empty:
        close = hist["Close"]
        if isinstance(close, pd.DataFrame):
            close = close.iloc[:, 0]
        close = close.dropna()
        if len(close) > 0:
            current = float(close.iloc[-1])
            for label, days in [("1 jaar", 252), ("3 jaar", 756), ("5 jaar", 1260)]:
                if len(close) > days:
                    past = float(close.iloc[-days])
                    change = (current - past) / past * 100
                    perf_texts.append(f"- Koers vs {label} geleden: {change:+.1f}%")

    lines.append("")
    lines.append("Lange termijn koersontwikkeling:")
    lines.extend(perf_texts if perf_texts else ["- Onvoldoende historische data beschikbaar"])

    # Puur cijfermatig afgeleide sterke punten -- geen mening, alleen drempelwaarden op de data hierboven
    plus = []
    if revenue_growth is not None and revenue_growth > 0.10:
        plus.append(f"Omzet groeit stevig ({revenue_growth * 100:.1f}% jaar-op-jaar)")
    if profit_margin is not None and profit_margin > 0.15:
        plus.append(f"Gezonde winstmarge ({profit_margin * 100:.1f}%)")
    if debt_to_equity is not None and debt_to_equity < 50:
        plus.append("Relatief lage schuldpositie t.o.v. eigen vermogen")
    if perf_texts and all(t.split(": ")[-1].startswith("+") for t in perf_texts):
        plus.append("Koers is over alle bekeken periodes gestegen")
    if dividend_yield:
        plus.append(f"Keert dividend uit ({dividend_yield * 100:.2f}%)")

    lines.append("")
    lines.append("Sterke punten (cijfermatig):")
    lines.extend([f"- {p}" for p in plus] if plus else ["- Geen duidelijke pluspunten op deze criteria"])

    # Puur cijfermatig afgeleide aandachtspunten
    minus = []
    if revenue_growth is not None and revenue_growth < 0:
        minus.append(f"Omzet krimpt ({revenue_growth * 100:.1f}% jaar-op-jaar)")
    if net_income is not None and net_income < 0:
        minus.append("Bedrijf maakt momenteel verlies")
    elif profit_margin is not None and profit_margin < 0.05:
        minus.append(f"Lage winstmarge ({profit_margin * 100:.1f}%)")
    if debt_to_equity is not None and debt_to_equity > 150:
        minus.append("Hoge schuldpositie t.o.v. eigen vermogen")
    if trailing_pe is not None and trailing_pe > 40:
        minus.append(f"Hoge waardering (K/W {trailing_pe:.0f}) -- veel toekomstige groei is al ingeprijsd")

    lines.append("")
    lines.append("Aandachtspunten (cijfermatig):")
    lines.extend([f"- {m}" for m in minus] if minus else ["- Geen duidelijke knelpunten op deze criteria"])

    lines.append("")
    lines.append("👉 Dit is een puur cijfermatige analyse, geen koop/verkoopadvies. "
                  "De cijfers zeggen niets over toekomstige koersontwikkeling -- doe altijd zelf verder onderzoek.")

    caption = "\n".join(lines)

    try:
        if hist is not None and not hist.empty:
            chart_buf = make_chart(ticker, hist, SETTINGS)
            send_telegram_photo(chart_buf, f"{resolved_name} ({ticker}) -- koers laatste 5 jaar")
        send_telegram_text(caption)
    except Exception as e:
        print(f"[FOUT] investigate-rapport versturen mislukt voor {ticker}: {e}")
        send_telegram_text(caption)

def process_telegram_commands():
    offset = load_offset()
    updates = get_telegram_updates(offset)
    print(f"[DEBUG] {len(updates)} Telegram-update(s) opgehaald (offset={offset}).")
    for update in updates:
        offset = update["update_id"] + 1
        message = update.get("message", {})
        text = (message.get("text") or "").strip()
        chat_id = message.get("chat", {}).get("id")

        if str(chat_id) != str(TELEGRAM_CHAT_ID):
            print(f"[DEBUG] bericht genegeerd: chat_id={chat_id} komt niet overeen met TELEGRAM_CHAT_ID={TELEGRAM_CHAT_ID}")
            continue  # negeer berichten uit andere chats, voor de veiligheid

        if not text.startswith("/"):
            print(f"[DEBUG] genegeerd (geen commando): '{text}'")
            continue

        parts = text.split(maxsplit=1)
        command = parts[0].lower()
        argument = parts[1].strip() if len(parts) > 1 else ""
        print(f"[DEBUG] commando ontvangen: '{command}' argument='{argument}' van chat_id={chat_id}")

        if command == "/track":
            handle_track(argument)
        elif command == "/untrack":
            handle_untrack(argument)
        elif command == "/list":
            handle_list()
        elif command == "/data":
            handle_data()
        elif command == "/investigate":
            handle_investigate(argument)
        elif command in ("/help", "/start"):
            handle_help()

    save_offset(offset)

# ---------------------------------------------------------------------------
# NIEUWS
# ---------------------------------------------------------------------------

def get_news_headlines(ticker, max_items):
    try:
        news = yf.Ticker(ticker).news
    except Exception:
        return []
    headlines = []
    for item in news[:max_items]:
        content = item.get("content", item)
        title = content.get("title")
        link = (content.get("clickThroughUrl") or {}).get("url") or content.get("link")
        if title:
            headlines.append((title, link))
    return headlines

# ---------------------------------------------------------------------------
# DATA OPHALEN
# ---------------------------------------------------------------------------

def batch_download(tickers, period, batch_size):
    result = {}
    for i in range(0, len(tickers), batch_size):
        chunk = tickers[i:i + batch_size]
        print(f"  ophalen batch {i // batch_size + 1} ({len(chunk)} tickers)...")
        try:
            data = yf.download(chunk, period=period, group_by="ticker",
                                progress=False, auto_adjust=True, threads=True)
        except Exception as e:
            print(f"[FOUT] batch download mislukt: {e}")
            continue
        for t in chunk:
            try:
                df = data if len(chunk) == 1 else data[t]
                if df is not None and not df.empty:
                    result[t] = df.dropna(how="all")
            except Exception:
                continue
    return result

def download_single(ticker, period):
    try:
        df = yf.download(ticker, period=period, progress=False, auto_adjust=True)
        return df.dropna(how="all") if df is not None and not df.empty else None
    except Exception as e:
        print(f"[FOUT] download {ticker} mislukt: {e}")
        return None

def get_current_price(ticker):
    """Snelle huidige-koers-lookup, gebruikt om de instapprijs bij /track vast te leggen."""
    try:
        df = download_single(ticker, "5d")
        if df is None or df.empty:
            return None
        close = df["Close"]
        if isinstance(close, pd.DataFrame):
            close = close.iloc[:, 0]
        close = close.dropna()
        return float(close.iloc[-1]) if len(close) else None
    except Exception as e:
        print(f"[FOUT] huidige koers ophalen mislukt voor {ticker}: {e}")
        return None

# ---------------------------------------------------------------------------
# ANALYSE
# ---------------------------------------------------------------------------

def analyze_df(df, settings, pct_threshold):
    if df is None or df.empty or len(df) < settings["sma_long"] + 1:
        return 0, [], None

    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    close = df["Close"]
    if isinstance(close, pd.DataFrame):
        close = close.iloc[:, 0]
    close = close.dropna()
    if len(close) < settings["sma_long"] + 1:
        return 0, [], None

    last_price = float(close.iloc[-1])
    prev_price = float(close.iloc[-2])
    pct_change = (last_price - prev_price) / prev_price * 100

    score = 0
    texts = []

    if abs(pct_change) >= pct_threshold:
        score += min(abs(pct_change), 20)
        if pct_change > 0:
            texts.append(f"Stevige stijging: +{pct_change:.1f}% in 1 dag. Kan wijzen op goed nieuws of hype, maar ook op een tijdelijke uitschieter.")
        else:
            texts.append(f"Stevige daling: {pct_change:.1f}% in 1 dag. Kan een tijdelijke inzinking zijn, of een teken van een structureel probleem -- check het nieuws.")

    rsi = RSIIndicator(close, window=settings["rsi_period"]).rsi()
    last_rsi = float(rsi.iloc[-1])
    if last_rsi >= settings["rsi_overbought"]:
        score += 10
        texts.append(f"De koers is de laatste tijd hard gestegen (RSI {last_rsi:.0f}/100) -- kan een sterke trend zijn, of oververhitting.")
    elif last_rsi <= settings["rsi_oversold"]:
        score += 10
        texts.append(f"De koers is de laatste tijd hard gedaald (RSI {last_rsi:.0f}/100) -- kan een overdreven reactie zijn, of aanhoudend negatief sentiment.")

    sma_short = SMAIndicator(close, window=settings["sma_short"]).sma_indicator()
    sma_long = SMAIndicator(close, window=settings["sma_long"]).sma_indicator()
    if len(sma_short.dropna()) > 1 and len(sma_long.dropna()) > 1:
        cross_now = sma_short.iloc[-1] - sma_long.iloc[-1]
        cross_prev = sma_short.iloc[-2] - sma_long.iloc[-2]
        if cross_prev <= 0 < cross_now:
            score += 15
            texts.append("'Golden cross': de kortetermijntrend is net boven de langetermijntrend gekomen -- vaak gezien als teken dat de opwaartse trend sterker wordt.")
        elif cross_prev >= 0 > cross_now:
            score += 15
            texts.append("'Death cross': de kortetermijntrend is net onder de langetermijntrend gezakt -- vaak gezien als teken dat de trend verzwakt.")

    # --- Kortetermijn hoog/laag punt (gericht op dag/week-trading, niet maanden/jaren) ---
    hl_window = settings["high_low_window"]
    if len(close) >= hl_window:
        recent = close.iloc[-hl_window:]
        recent_high = float(recent.max())
        recent_low = float(recent.min())
        span = recent_high - recent_low
        if span > 0:
            dist_to_high_pct = (recent_high - last_price) / span * 100
            dist_to_low_pct = (last_price - recent_low) / span * 100
            if dist_to_high_pct <= 5:
                score += 12
                texts.append(f"🔴 MOGELIJK VERKOOPMOMENT (cijfermatig signaal): koers ({last_price:.2f}) staat op/bij het "
                              f"hoogste punt van de laatste {hl_window} handelsdagen ({recent_high:.2f}). "
                              f"Statistisch gezien is dit het punt waarop kortetermijn-traders vaak winst nemen, "
                              f"omdat de koers hierna geregeld weer terugzakt. Kan ook doorzetten bij aanhoudend momentum -- "
                              f"geen garantie, puur een patroon uit de cijfers.")
            elif dist_to_low_pct <= 5:
                score += 12
                texts.append(f"🟢 MOGELIJK KOOPMOMENT (cijfermatig signaal): koers ({last_price:.2f}) staat op/bij het "
                              f"laagste punt van de laatste {hl_window} handelsdagen ({recent_low:.2f}). "
                              f"Statistisch gezien is dit het punt waarop kortetermijn-traders vaak instappen, "
                              f"omdat de koers hierna geregeld weer opveert. Kan ook wijzen op aanhoudende verkoopdruk -- "
                              f"geen garantie, puur een patroon uit de cijfers.")

    # --- Bollinger Bands: staat de koers kortetermijn "te ver" van zijn gemiddelde? ---
    if len(close) >= settings["bb_window"]:
        bb = BollingerBands(close, window=settings["bb_window"], window_dev=settings["bb_std"])
        upper = bb.bollinger_hband()
        lower = bb.bollinger_lband()
        if not upper.dropna().empty and not lower.dropna().empty:
            last_upper = float(upper.iloc[-1])
            last_lower = float(lower.iloc[-1])
            if last_price >= last_upper:
                score += 10
                texts.append(f"🔴 MOGELIJK VERKOOPMOMENT (cijfermatig signaal): koers ({last_price:.2f}) zit op/boven de "
                              f"bovenste Bollinger-band ({last_upper:.2f}). Statistisch gezien is de koers hiermee "
                              f"kortetermijn 'te duur' t.o.v. zijn eigen recente gemiddelde -- vaak volgt hierna een korte terugval.")
            elif last_price <= last_lower:
                score += 10
                texts.append(f"🟢 MOGELIJK KOOPMOMENT (cijfermatig signaal): koers ({last_price:.2f}) zit op/onder de "
                              f"onderste Bollinger-band ({last_lower:.2f}). Statistisch gezien is de koers hiermee "
                              f"kortetermijn 'te goedkoop' t.o.v. zijn eigen recente gemiddelde -- vaak volgt hierna een korte opleving.")

    return score, texts, last_price

def make_chart(ticker, df, settings):
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    close = df["Close"]
    if isinstance(close, pd.DataFrame):
        close = close.iloc[:, 0]

    sma_short = SMAIndicator(close, window=settings["sma_short"]).sma_indicator()
    sma_long = SMAIndicator(close, window=settings["sma_long"]).sma_indicator()

    fig, ax = plt.subplots(figsize=(8, 4.5))
    ax.plot(close.index, close.values, label="Koers", linewidth=1.5)
    ax.plot(sma_short.index, sma_short.values, label=f"SMA{settings['sma_short']}", linewidth=1)
    ax.plot(sma_long.index, sma_long.values, label=f"SMA{settings['sma_long']}", linewidth=1)
    ax.set_title(ticker)
    ax.legend(loc="upper left", fontsize=8)
    ax.grid(alpha=0.3)
    fig.tight_layout()

    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=120)
    plt.close(fig)
    buf.seek(0)
    return buf

def build_and_send_alert(ticker, display_name, texts, price, df, state, state_key_prefix):
    today_key = datetime.now().strftime("%Y-%m-%d")
    seen_key = f"{state_key_prefix}_{ticker}_{today_key}"
    already_sent = state.get(seen_key, [])
    new_texts = [t for t in texts if t not in already_sent]
    if not new_texts:
        return False

    caption_lines = [f"*{display_name}* (koers: {price:.2f})", ""]
    for t in new_texts:
        caption_lines.append(f"• {t}")

    news = get_news_headlines(ticker, SETTINGS["news_items_per_ticker"])
    if news:
        caption_lines.append("")
        caption_lines.append("📰 Recent nieuws:")
        for title, link in news:
            caption_lines.append(f"- {title}" + (f"\n  {link}" if link else ""))

    caption_lines.append("")
    caption_lines.append("👉 Dit zijn cijfermatige patronen (wat de statistiek/indicatoren zeggen), "
                          "geen persoonlijk koop-/verkoopadvies. De koers kan altijd tegen het patroon in bewegen.")
    caption = "\n".join(caption_lines)

    try:
        chart_buf = make_chart(ticker, df, SETTINGS)
        send_telegram_photo(chart_buf, caption)
    except Exception as e:
        print(f"[FOUT] grafiek maken voor {ticker} mislukt: {e}")
        send_telegram_text(caption)

    print(caption)
    print("---")
    state[seen_key] = already_sent + new_texts
    return True

# ---------------------------------------------------------------------------
# GEVOLGDE AANDELEN (jouw /track lijst) -- vaker en strenger gecontroleerd
# ---------------------------------------------------------------------------

def check_tracked_tickers(on_demand=False):
    tracked = load_tracked()
    if not tracked:
        if on_demand:
            send_telegram_text("Je volgt momenteel niks. Gebruik /track <naam> om te beginnen.")
        return
    state = load_state()
    found_any = False
    for entry in tracked:
        ticker, name = entry["ticker"], entry["name"]
        entry_price = entry.get("entry_price")
        entry_date = entry.get("entry_date")
        df = download_single(ticker, f"{SETTINGS['lookback_days']}d")
        if df is None:
            continue
        score, texts, price = analyze_df(df, SETTINGS, SETTINGS["safety_pct_threshold"])

        gain_line = ""
        if entry_price is not None and price is not None:
            pct = (price - entry_price) / entry_price * 100
            teken = "+" if pct >= 0 else ""
            gain_line = f" | Jouw instap: {entry_price:.2f} op {entry_date} -> nu {teken}{pct:.1f}%"

        if texts:
            header = f"🔔 Gevolgd aandeel -- {name}{gain_line}"
            texts_with_prefix = [f"[Veiligheidsmelding] {t}" for t in texts]
            sent = build_and_send_alert(ticker, header, texts_with_prefix, price, df, state, "tracked")
            found_any = found_any or sent
        elif on_demand and price is not None:
            send_telegram_text(f"ℹ️ {name} ({ticker}): koers {price:.2f}{gain_line}, geen bijzondere signalen op dit moment.")
            found_any = True
    if on_demand and not found_any:
        send_telegram_text("Geen bijzonderheden bij je gevolgde aandelen op dit moment.")
    save_state(state)

# ---------------------------------------------------------------------------
# GROTE SCAN (AEX + S&P 500) -- minder vaak, bredere top-selectie
# ---------------------------------------------------------------------------

def run_scan(on_demand=False):
    state = load_state()
    tickers = []
    if INCLUDE_AEX:
        tickers += AEX_TICKERS
    if INCLUDE_SP500:
        tickers += get_sp500_tickers()
    tickers = sorted(set(tickers))

    print(f"Scan van {len(tickers)} tickers gestart, dit kan even duren...")
    data = batch_download(tickers, f"{SETTINGS['lookback_days']}d", SETTINGS["batch_size"])

    results = []
    for ticker in tickers:
        df = data.get(ticker)
        if df is None:
            continue
        score, texts, price = analyze_df(df, SETTINGS, SETTINGS["pct_change_threshold"])
        if score > 0 and texts:
            results.append((score, ticker, texts, price, df))

    results.sort(key=lambda x: x[0], reverse=True)
    top_picks = results[:TOP_N]

    if not top_picks:
        print(f"[{datetime.now()}] Geen interessante signalen in deze scan.")
        if on_demand:
            send_telegram_text(f"Geen bijzondere signalen gevonden in de markt-scan ({len(tickers)} tickers gecheckt).")
        save_state(state)
        return

    send_telegram_text(
        f"📊 Top {len(top_picks)} interessantste aandelen "
        f"({datetime.now().strftime('%d-%m %H:%M')}), uit {len(tickers)} gescand:"
    )
    for score, ticker, texts, price, df in top_picks:
        build_and_send_alert(ticker, ticker, texts, price, df, state, "scan")

    save_state(state)

# ---------------------------------------------------------------------------
# ENTRYPOINT
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--loop", action="store_true")
    parser.add_argument("--command-interval", type=int, default=30,
                         help="Seconden tussen het pollen op nieuwe Telegram-commando's (default 30s, dit is gratis/lichtgewicht)")
    parser.add_argument("--tracked-interval", type=int, default=TRACKED_CHECK_INTERVAL_MINUTES * 60,
                         help=f"Seconden tussen checks van je /track-lijst (default {TRACKED_CHECK_INTERVAL_MINUTES} min)")
    parser.add_argument("--auto-interval", type=int, default=AUTO_CHECK_INTERVAL_HOURS * 3600,
                         help=f"Seconden tussen de zware AEX/S&P-scan (default {AUTO_CHECK_INTERVAL_HOURS} uur)")
    args = parser.parse_args()

    if args.loop:
        print(f"Bot draait continu. Commando's elke {args.command_interval}s, "
              f"tracked-check elke {args.tracked_interval/60:.0f} min, "
              f"scan elke {args.auto_interval/3600:.1f}u. Ctrl+C om te stoppen.")
        last_tracked = 0
        last_scan = 0
        while True:
            try:
                process_telegram_commands()
            except Exception as e:
                print(f"[FOUT] commando-verwerking: {e}")

            now = time.time()
            if now - last_tracked >= args.tracked_interval:
                try:
                    check_tracked_tickers()
                except Exception as e:
                    print(f"[FOUT] tracked-check: {e}")
                last_tracked = now

            if now - last_scan >= args.auto_interval:
                try:
                    run_scan()
                except Exception as e:
                    print(f"[FOUT] scan: {e}")
                last_scan = now

            time.sleep(args.command_interval)
    else:
        # Eenmalige run (bv. aangeroepen door een GitHub Actions cron-schema).
        # Elk van de twee checks houdt zijn EIGEN laatst-gedraaid-tijdstip bij
        # in een los bestand, zodat ze onafhankelijk van elkaar op hun eigen
        # interval draaien -- ongeacht hoe vaak deze workflow zelf getriggerd wordt.
        process_telegram_commands()

        if should_run_tracked():
            check_tracked_tickers()
            mark_tracked_done()
        else:
            print("Tracked-check nog niet aan de beurt (TRACKED_CHECK_INTERVAL_MINUTES nog niet verstreken).")

        if should_run_auto():
            run_scan()
            mark_auto_done()
        else:
            print("Marktscan nog niet aan de beurt (AUTO_CHECK_INTERVAL_HOURS nog niet verstreken).")
