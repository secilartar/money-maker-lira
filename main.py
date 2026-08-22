#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Money Maker — Lira'ya Sor API (Gemini) v2.8
yf.download (analiz kodundaki gibi) + KAP + doğal cevap
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
import pandas as pd

# ================== AYARLAR ==================
API_SECRET_KEY = (os.environ.get("API_SECRET_KEY") or "").strip()
GEMINI_API_KEY = (os.environ.get("GEMINI_API_KEY") or "").strip()
MODEL_NAME = os.environ.get("GEMINI_MODEL", "gemini-3.6-flash")

KAP_API = "https://www.kap.org.tr/tr/api/disclosure/members/byCriteria"
KAP_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Content-Type": "application/json",
    "Referer": "https://www.kap.org.tr/tr/bildirim-sorgu",
    "Origin": "https://www.kap.org.tr",
}

app = FastAPI(title="Money Maker Lira'ya Sor", version="2.8")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


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


def _check_secret(x_api_key: Optional[str]) -> None:
    if not API_SECRET_KEY:
        raise HTTPException(500, "API_SECRET_KEY tanımlı değil")
    if not x_api_key or x_api_key != API_SECRET_KEY:
        raise HTTPException(403, "Erişim reddedildi")


def _get_client():
    if not GEMINI_API_KEY:
        raise HTTPException(500, "GEMINI_API_KEY tanımlı değil")
    return genai.Client(api_key=GEMINI_API_KEY)


def extract_tickers(text: str) -> list[str]:
    candidates = re.findall(r'\b([A-Z]{3,6})\b', text.upper())
    blacklist = {"KAP", "BIST", "TL", "USD", "TRY", "API", "KANKI", "LIRA", "SON", "GUN", "HAFTA", "BUGUN"}
    return [c for c in set(candidates) if c not in blacklist]


def get_stock_info(ticker: str) -> str:
    """Analiz kodundaki gibi yf.download kullanıyor – daha güvenilir"""
    symbol = ticker if ticker.endswith(".IS") else f"{ticker}.IS"
    try:
        df = yf.download(symbol, period="10d", interval="1d", progress=False, threads=False)
        if df.empty:
            return ""

        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)

        last = df.iloc[-1]
        prev = df.iloc[-2] if len(df) > 1 else last

        price = float(last["Close"])
        prev_close = float(prev["Close"])
        change = price - prev_close
        change_pct = (change / prev_close) * 100 if prev_close else 0
        volume = int(last.get("Volume", 0))
        last_date = str(df.index[-1].date())

        text = (
            f"**{ticker}**\n"
            f"- Son Kapanış: **{price:.2f} TL** ({last_date})\n"
            f"- Değişim: {change:+.2f} TL (%{change_pct:+.2f})\n"
        )
        if volume:
            text += f"- Hacim: {volume:,}\n"

        if date.today().weekday() >= 5:
            text += "- Not: BIST kapalı (hafta sonu), Cuma kapanışıdır.\n"

        return text
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
    try:
        with httpx.Client(timeout=10, follow_redirects=True) as client:
            r = client.post(KAP_API, json=payload, headers=KAP_HEADERS)
            if r.status_code == 200:
                data = r.json() if isinstance(r.json(), list) else []
    except Exception:
        pass

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
        if ticker_up in [str(s).upper() for s in stocks]:
            relevant.append(d)

    if not relevant:
        return ""

    relevant = sorted(relevant, key=lambda x: x.get("publishDate") or "", reverse=True)[:4]
    lines = [f"**{ticker} – Son KAP:**"]
    for d in relevant:
        when = (d.get("publishDate") or "")[:16]
        subj = (d.get("subject") or "").strip()
        summ = (d.get("summary") or "").strip()[:120]
        lines.append(f"- [{when}] {subj}")
        if summ:
            lines.append(f"  {summ}")
    return "\n".join(lines)


SYSTEM_PROMPT = """
Sen Lira'sın. Türkçe konuşan, samimi, net ve biraz esprili bir finans asistanısın.

Kurallar:
- "kanki", "kankitom", "patron", "donçiçim"  diyebilirsin.
- Yatırım tavsiyesi verebilirsin, riskleri de belirt.
- Sana --- CANLI VERİ --- altında fiyat veya KAP geldiyse, ÖNCE onları söyle.
- Fiyat varsa direkt "BRSAN Cuma günü XXX TL'den kapattı" diye başla.
- KAP bildirimi varsa hemen özetle.
- Veri yoksa bile "veri ulaşmadı" deme, kısa genel bilgi ver.
- Cevabı yarım bırakma, tamamla.
- Uzun genel sektör muhabbeti yapma. Soruya net cevap ver.
-Emoji serbest.
"""


@app.get("/health")
def health():
    return {
        "ok": True,
        "gemini": bool(GEMINI_API_KEY),
        "secret": bool(API_SECRET_KEY),
        "model": MODEL_NAME,
        "version": "2.8"
    }


@app.post("/api/lira-sor", response_model=SorResponse)
def lira_sor(req: SorRequest, x_api_key: Optional[str] = Header(None)):
    _check_secret(x_api_key)

    soru = req.soru.strip()
    tickers = extract_tickers(soru)
    extra_parts = []

    for t in tickers[:2]:
        price = get_stock_info(t)
        if price:
            extra_parts.append(price)
        kap = fetch_kap_for_ticker(t)
        if kap:
            extra_parts.append(kap)

    extra_context = ""
    if extra_parts:
        extra_context = (
            "\n\n--- CANLI VERİ ---\n"
            + "\n\n".join(extra_parts)
            + "\n\nBu verileri kullanarak CEVABA DİREKT BAŞLA. Önce fiyat, sonra haber."
        )

    full_prompt = SYSTEM_PROMPT + extra_context

    cevap = None
    for attempt in range(2):
        try:
            client = _get_client()
            response = client.models.generate_content(
                model=MODEL_NAME,
                contents=[
                    {"role": "user", "parts": [{"text": full_prompt}]},
                    {"role": "model", "parts": [{"text": "Tamam kanki, hazırım. Net konuşacağım."}]},
                    {"role": "user", "parts": [{"text": soru}]},
                ],
                config={
                    "temperature": 0.45,
                    "max_output_tokens": 3000,
                },
            )
            cevap = (response.text or "").strip()
            if cevap:
                break
        except Exception:
            if attempt == 0:
                time.sleep(1.2)
            continue

    if not cevap:
        if tickers:
            cevap = f"Kanki {tickers[0]} için şu an net veri çekemedim ama genel olarak volatil bir hisse, stop’unu unutma."
        else:
            cevap = "Kanki şu an biraz yoğunum, biraz sonra tekrar sor."

    return SorResponse(
        ok=True,
        soru=soru,
        cevap=cevap,
        thread_id="gemini",
        kaynak="gemini+data" if extra_parts else "gemini"
    )


@app.get("/")
def root():
    return HTMLResponse("<h2>Lira API v2.8</h2><p><a href='/health'>/health</a></p>")
