#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Money Maker — Lira'ya Sor API (Gemini) v2.3
Akıllı sürüm: yfinance entegrasyonu + güçlü sistem promptu
"""

from __future__ import annotations

import os
import re
from typing import Optional

from fastapi import FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field
from google import genai
import yfinance as yf

# ================== AYARLAR ==================
API_SECRET_KEY = (os.environ.get("API_SECRET_KEY") or "").strip()
GEMINI_API_KEY = (os.environ.get("GEMINI_API_KEY") or "").strip()
MODEL_NAME = os.environ.get("GEMINI_MODEL", "gemini-3.6-flash")

app = FastAPI(
    title="Money Maker Lira'ya Sor (Gemini)",
    version="2.3",
    description="Akıllı Lira – Gemini + yfinance"
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


# ================== YARDIMCI FONKSİYONLAR ==================
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
    """Soru içinden BIST hisse kodlarını yakalar (ör: BRSAN, THYAO, GARAN)"""
    candidates = re.findall(r'\b([A-Z]{3,6})\b', text.upper())
    return list(set(candidates))


def get_stock_info(ticker: str) -> str:
    """yfinance ile son fiyat + günlük değişim çeker"""
    try:
        symbol = ticker if ticker.endswith(".IS") else f"{ticker}.IS"
        stock = yf.Ticker(symbol)
        info = stock.info
        hist = stock.history(period="5d")

        if hist.empty:
            return f"{ticker}: Veri bulunamadı."

        last = hist.iloc[-1]
        prev = hist.iloc[-2] if len(hist) > 1 else last

        price = last["Close"]
        change = price - prev["Close"]
        change_pct = (change / prev["Close"]) * 100 if prev["Close"] != 0 else 0

        name = info.get("shortName") or info.get("longName") or ticker
        volume = int(last.get("Volume", 0))

        return (
            f"**{name} ({ticker})**\n"
            f"- Son Fiyat: **{price:.2f} TL**\n"
            f"- Günlük Değişim: {change:+.2f} TL (%{change_pct:+.2f})\n"
            f"- Hacim: {volume:,}\n"
        )
    except Exception as e:
        return f"{ticker}: Veri çekilemedi ({str(e)[:80]})"


SYSTEM_PROMPT = """
Sen Lira'sın. Türkçe konuşan, samimi, net ve biraz esprili bir finans asistanısın.
BIST hisseleri, fonlar, ekonomik veriler ve genel piyasa konularında yardımcı olursun.

Karakterin:
- Samimi ve kanki tarzı konuş (kanki, kankitom, kanki diyebilirsin)
- Asla "hocam" deme
- Yatırım tavsiyesi verebilirsin, riskleri de belirt
- Güncel fiyat verisi sana verildiğinde onu mutlaka kullan ve yorumla
- Bilmediğin konularda dürüst ol, ama mümkünse alternatif yol göster

Cevap stilin:
- Kısa, net ve öz tut
- Gerekirse Markdown tablo veya madde işaretleri kullan
- Gereksiz uzun girişler yapma
"""


# ================== ENDPOINTLER ==================
@app.get("/health")
def health():
    return {
        "ok": True,
        "gemini": bool(GEMINI_API_KEY),
        "secret": bool(API_SECRET_KEY),
        "model": MODEL_NAME,
        "version": "2.3",
        "yfinance": True
    }


@app.post("/api/lira-sor", response_model=SorResponse)
def lira_sor(req: SorRequest, x_api_key: Optional[str] = Header(None)):
    _check_secret(x_api_key)

    client = _get_client()
    soru = req.soru.strip()

    # Hisse kodu varsa canlı veri çek
    tickers = extract_tickers(soru)
    extra_context = ""

    if tickers:
        data_parts = []
        for t in tickers[:4]:
            data_parts.append(get_stock_info(t))
        if data_parts:
            extra_context = (
                "\n\n--- CANLI VERİ (yfinance) ---\n"
                + "\n".join(data_parts)
                + "\nBu verileri kullanarak cevap ver. "
                "Fiyat ve değişim rakamlarını mutlaka belirt."
            )

    full_prompt = SYSTEM_PROMPT + extra_context

    try:
        response = client.models.generate_content(
            model=MODEL_NAME,
            contents=[
                {"role": "user", "parts": [{"text": full_prompt}]},
                {"role": "model", "parts": [{"text": "Tamam kanki, Lira hazır. Ne sormak istersin?"}]},
                {"role": "user", "parts": [{"text": soru}]},
            ],
            config={
                "temperature": 0.5,
                "max_output_tokens": 2048,
            }
        )

        cevap = (response.text or "").strip()
        if not cevap:
            raise HTTPException(status_code=502, detail="Gemini boş cevap döndü")

    except Exception as e:
        err = str(e)
        if "API key" in err or "401" in err or "403" in err:
            raise HTTPException(status_code=502, detail="Gemini API Key hatası. Anahtarı kontrol et.")
        raise HTTPException(status_code=502, detail=f"Gemini hatası: {err[:350]}")

    return SorResponse(
        ok=True,
        soru=soru,
        cevap=cevap,
        thread_id="gemini",
        kaynak="gemini+yfinance" if tickers else "gemini"
    )


@app.get("/", response_class=HTMLResponse)
@app.get("/widget", response_class=HTMLResponse)
def root():
    return HTMLResponse(
        "<h2>Lira API v2.3 (Gemini + yfinance)</h2>"
        "<p>POST <code>/api/lira-sor</code></p>"
        "<p><a href='/health'>/health</a></p>"
    )
