#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Money Maker — Lira'ya Sor API (Gemini) v2.7
Hata gizleme + doğal Lira fallback
"""

from __future__ import annotations

import os
import re
import time
from datetime import date, timedelta
from typing import Optional

from fastapi import FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field
from google import genai
import yfinance as yf
import httpx

# ================== AYARLAR ==================
API_SECRET_KEY = (os.environ.get("API_SECRET_KEY") or "").strip()
GEMINI_API_KEY = (os.environ.get("GEMINI_API_KEY") or "").strip()
MODEL_NAME = os.environ.get("GEMINI_MODEL", "gemini-3.6-flash")

KAP_API = "https://www.kap.org.tr/tr/api/disclosure/members/byCriteria"
KAP_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Content-Type": "application/json",
    "Referer": "https://www.kap.org.tr/tr/bildirim-sorgu",
    "Origin": "https://www.kap.org.tr",
}

app = FastAPI(
    title="Money Maker Lira'ya Sor (Gemini)",
    version="2.7",
    description="Akıllı Lira – Gemini + yfinance + KAP"
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=os.environ.get("CORS_ORIGINS", "*").split(","),
    allow_credentials=True,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
)


# ================== MODELLER ==================
class SorRequest(BaseModel):
    soru: str = Field(..., min_length=2, max_length=2000)
    thread_id: Optional[str] = None
    new_thread: bool = True
    web: Optional[bool] = None


class SorResponse(BaseModel):
    ok: bool
    soru: str
    cevap: str
    thread_id: str = "gemini"
    kaynak: str = "gemini"


# ================== YARDIMCI ==================
def _check_secret(x_api_key: Optional[str]) -> None:
    if not API_SECRET_KEY:
        raise HTTPException(status_code=500, detail="API_SECRET_KEY tanımlı değil")
    if not x_api_key or x_api_key != API_SECRET_KEY:
        raise HTTPException(status_code=403, detail="Erişim reddedildi. Geçersiz API anahtarı.")


def _get_client() -> genai.Client:
    if not GEMINI_API_KEY:
        raise HTTPException(status_code=500, detail="GEMINI_API_KEY tanımlı değil")
    return genai.Client(api_key=GEMINI_API_KEY)


def extract_tickers(text: str) -> list[str]:
    candidates = re.findall(r'\b([A-Z]{3,6})\b', text.upper())
    blacklist = {"KAP", "BIST", "TL", "USD", "TRY", "API", "KANKI", "LIRA", "SON", "GUN", "HAFTA", "BUGUN"}
    return [c for c in set(candidates) if c not in blacklist]


def get_stock_info(ticker: str) -> str:
    symbol = ticker if ticker.endswith(".IS") else f"{ticker}.IS"
    try:
        stock = yf.Ticker(symbol)
        info = stock.info
        price = info.get("currentPrice") or info.get("regularMarketPrice") or info.get("previousClose")
        prev = info.get("previousClose")
        name = info.get("shortName") or info.get("longName") or ticker
        volume = info.get("volume") or info.get("regularMarketVolume")

        if price:
            text = f"**{name} ({ticker})**\n- Son Fiyat: **{price:.2f} TL**\n"
            if prev and prev != 0:
                change = price - prev
                change_pct = (change / prev) * 100
                text += f"- Günlük Değişim: {change:+.2f} TL (%{change_pct:+.2f})\n"
            if volume:
                text += f"- Hacim: {int(volume):,}\n"
            return text

        hist = stock.history(period="5d")
        if not hist.empty:
            last = hist.iloc[-1]
            prev_close = hist.iloc[-2]["Close"] if len(hist) > 1 else last["Close"]
            price = float(last["Close"])
            change = price - float(prev_close)
            change_pct = (change / float(prev_close)) * 100 if prev_close else 0
            return (
                f"**{name} ({ticker})**\n"
                f"- Son Fiyat: **{price:.2f} TL**\n"
                f"- Günlük Değişim: {change:+.2f} TL (%{change_pct:+.2f})\n"
            )
        return ""
    except Exception:
        return ""


def fetch_kap_for_ticker(ticker: str, days: int = 5) -> str:
    to_d = date.today()
    from_d = to_d - timedelta(days=days)
    payload = {
        "fromDate": from_d.isoformat(),
        "toDate": to_d.isoformat(),
        "mkkMemberOidList": [],
        "subjectList": [],
    }

    data = []
    for attempt in range(2):
        try:
            with httpx.Client(timeout=12, follow_redirects=True) as client:
                r = client.post(KAP_API, json=payload, headers=KAP_HEADERS)
                if r.status_code == 200:
                    data = r.json()
                    if isinstance(data, list):
                        break
                time.sleep(1.5)
        except Exception:
            time.sleep(1)
            continue

    if not data:
        return ""

    relevant = []
    ticker_up = ticker.upper()
    for d in data:
        if d.get("disclosureClass") != "ODA":
            continue
        stocks = d.get("relatedStocks") or d.get("stockCodes") or []
        if isinstance(stocks, str):
            stocks = [s.strip() for s in stocks.replace(",", " ").split()]
        stocks_up = [str(s).upper() for s in stocks]
        if ticker_up in stocks_up or any(ticker_up in s for s in stocks_up):
            relevant.append(d)

    if not relevant:
        return ""

    relevant = sorted(relevant, key=lambda x: x.get("publishDate") or "", reverse=True)[:4]

    lines = [f"**{ticker} – Son KAP Bildirimleri:**"]
    for d in relevant:
        when = (d.get("publishDate") or "")[:16]
        subj = (d.get("subject") or "").strip()
        summ = (d.get("summary") or "").strip()[:140]
        lines.append(f"- [{when}] {subj}")
        if summ:
            lines.append(f"  {summ}")
    return "\n".join(lines)


SYSTEM_PROMPT = """
Sen Lira'sın. Türkçe konuşan, samimi, net ve biraz esprili bir finans asistanısın.

Kurallar:
- "kanki", "kankitom" diyebilirsin. Asla "hocam" deme.
- Yatırım tavsiyesi verebilirsin, riskleri de söyle.
- Sana canlı fiyat veya KAP bildirimi geldiyse onları kullan.
- Veri gelmezse site önerme, "kap.org.tr" deme, "aracı kurum" deme.
- Cevapları kısa ve doğal tut, arkadaş gibi konuş.
"""


def natural_fallback(soru: str, tickers: list[str]) -> str:
    """Gemini çökünce Lira kendi ağzından cevap versin"""
    if tickers:
        t = tickers[0]
        return (
            f"Kanki şu an biraz yoğunum, {t} için net rakam ve son haberleri "
            f"hemen çekemedim. Biraz sonra tekrar sorarsan bakarız. "
            f"Genel olarak {t} volatil bir hisse, stop’unu ihmal etme."
        )
    return (
        "Kanki şu an sistem biraz yoğun, net cevap veremiyorum. "
        "Biraz sonra tekrar dene, hemen bakarım."
    )


# ================== ENDPOINTLER ==================
@app.get("/health")
def health():
    return {
        "ok": True,
        "gemini": bool(GEMINI_API_KEY),
        "secret": bool(API_SECRET_KEY),
        "model": MODEL_NAME,
        "version": "2.7",
        "yfinance": True,
        "kap": True
    }


@app.post("/api/lira-sor", response_model=SorResponse)
def lira_sor(req: SorRequest, x_api_key: Optional[str] = Header(None)):
    _check_secret(x_api_key)

    soru = req.soru.strip()
    tickers = extract_tickers(soru)
    extra_parts = []

    for t in tickers[:2]:
        price_info = get_stock_info(t)
        if price_info:
            extra_parts.append(price_info)
        kap_info = fetch_kap_for_ticker(t, days=5)
        if kap_info:
            extra_parts.append(kap_info)

    extra_context = ""
    if extra_parts:
        extra_context = (
            "\n\n--- CANLI VERİ ---\n"
            + "\n\n".join(extra_parts)
            + "\n\nBu verileri kullanarak cevap ver."
        )

    full_prompt = SYSTEM_PROMPT + extra_context

    # Gemini'yi 2 kere dene, olmazsa doğal fallback
    cevap = None
    for attempt in range(2):
        try:
            client = _get_client()
            response = client.models.generate_content(
                model=MODEL_NAME,
                contents=[
                    {"role": "user", "parts": [{"text": full_prompt}]},
                    {"role": "model", "parts": [{"text": "Tamam kanki, Lira hazır. Sor."}]},
                    {"role": "user", "parts": [{"text": soru}]},
                ],
                config={
                    "temperature": 0.5,
                    "max_output_tokens": 1800,
                }
            )
            cevap = (response.text or "").strip()
            if cevap:
                break
        except Exception:
            if attempt == 0:
                time.sleep(1.5)
            continue

    if not cevap:
        cevap = natural_fallback(soru, tickers)

    return SorResponse(
        ok=True,
        soru=soru,
        cevap=cevap,
        thread_id="gemini",
        kaynak="gemini+data" if extra_parts else "gemini"
    )


@app.get("/", response_class=HTMLResponse)
@app.get("/widget", response_class=HTMLResponse)
def root():
    return HTMLResponse(
        "<h2>Lira API v2.7</h2>"
        "<p>POST /api/lira-sor</p>"
        "<p><a href='/health'>/health</a></p>"
    )
