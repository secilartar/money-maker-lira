#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Money Maker — Lira'ya Sor API (Gemini) v3.1
"""

from __future__ import annotations

import os
import re
import time
from datetime import datetime, timedelta, timezone
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
MODEL_NAME = os.environ.get("GEMINI_MODEL", "gemini-3.5-flash")

KAP_API = "https://www.kap.org.tr/tr/api/disclosure/members/byCriteria"
KAP_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Content-Type": "application/json",
    "Referer": "https://www.kap.org.tr/tr/bildirim-sorgu",
    "Origin": "https://www.kap.org.tr",
}

TR_TZ = timezone(timedelta(hours=3))

app = FastAPI(title="Money Maker Lira'ya Sor", version="3.1")

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
    blacklist = {
        "KAP", "BIST", "TL", "USD", "TRY", "API", "KANKI", "LIRA", "SON",
        "GUN", "HAFTA", "BUGUN", "BANA", "SANA", "NASIL", "NEDEN", "HADI",
        "OLUR", "GIDER", "YARIN", "HISSE", "ALINIR", "MI", "YOKSA", "VAR",
        "GIBI", "ICIN", "GORE", "KANKA", "HOCAM", "MERHABA", "SELAM"
    }
    return [c for c in set(candidates) if c not in blacklist]


def get_stock_info(ticker: str) -> str:
    symbol = ticker if ticker.endswith(".IS") else f"{ticker}.IS"
    try:
        df = yf.download(
            symbol,
            period="15d",
            interval="1d",
            progress=False,
            threads=False,
            auto_adjust=True
        )

        if df.empty:
            stock = yf.Ticker(symbol)
            df = stock.history(period="15d", interval="1d", auto_adjust=True)

        if df.empty:
            return ""

        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)

        df = df.dropna(subset=["Close"])
        if df.empty:
            return ""

        # En son iki günü al
        last = df.iloc[-1]
        prev = df.iloc[-2] if len(df) > 1 else last

        price = float(last["Close"])
        prev_close = float(prev["Close"])
        change = price - prev_close
        change_pct = (change / prev_close * 100) if prev_close else 0
        volume = int(last["Volume"]) if "Volume" in last and pd.notna(last["Volume"]) else 0

        try:
            last_date = last.name.strftime("%Y-%m-%d") if hasattr(last.name, "strftime") else str(last.name)[:10]
        except Exception:
            last_date = str(df.index[-1])[:10]

        # Eğer son tarih Cuma değilse ve bugün hafta sonuysa, bir önceki günü de kontrol et
        # (bazen yfinance Cuma'yı geç gösteriyor)
        bugun = datetime.now(TR_TZ)
        if bugun.weekday() >= 5 and len(df) >= 2:
            # En güncel iki kapanışı da yaz ki Gemini doğru olanı seçsin
            prev_date = prev.name.strftime("%Y-%m-%d") if hasattr(prev.name, "strftime") else str(prev.name)[:10]
            text = (
                f"**{ticker}**\n"
                f"- En son kapanış: **{price:.2f} TL** ({last_date}) → Değişim: {change:+.2f} TL (%{change_pct:+.2f})\n"
                f"- Bir önceki gün: **{prev_close:.2f} TL** ({prev_date})\n"
            )
        else:
            text = (
                f"**{ticker}**\n"
                f"- Son Kapanış: **{price:.2f} TL** ({last_date})\n"
                f"- Değişim: {change:+.2f} TL (%{change_pct:+.2f})\n"
            )

        if volume > 0:
            text += f"- Hacim: {volume:,}\n"

        if bugun.weekday() >= 5:
            text += "- Not: BIST kapalı (hafta sonu).\n"

        return text
    except Exception as e:
        print(f"[yfinance hata] {ticker}: {e}")
        return ""


def fetch_kap_for_ticker(ticker: str, days: int = 5) -> str:
    to_d = datetime.now(TR_TZ).date()
    from_d = to_d - timedelta(days=days)
    payload = {
        "fromDate": from_d.isoformat(),
        "toDate": to_d.isoformat(),
        "mkkMemberOidList": [],
        "subjectList": [],
    }
    data = []
    try:
        with httpx.Client(timeout=8, follow_redirects=True) as client:
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
Sen Lira'sın. Türkçe konuşan, samimi, veri odaklı, detaylı analiz yapabilen ve biraz esprili bir finans asistanısın.

ZORUNLU KURALLAR:
- "kanki", "kankitom", "patron" diyebilirsin. Asla "hocam" deme.
- Sana CANLI VERİ geldiyse, ÖNCE güncel fiyatı (RAKAM OLARAK KESİNLİKLE YAZ) ve durumu şık bir şekilde değerlendir, sonra KAP haberini yorumla.
- Yanıtlarını her zaman detaylı, Markdown ile yapılandırılmış ve okunaklı ver.
- Yatırım tavsiyesi verebilirsin ama riskleri, piyasa volatilitesini ve stop-loss hayat kurtarır gerçeğini hep vurgula.
- Veri yoksa bile "veri ulaşmadı", "API patladı", "aracı kurum", "kap.org.tr" gibi cümleler ASLA KULLANMA.
- Sana verilen SİSTEM SAATİ VE TARİHİ bilgisini dikkate al. Hafta sonuysa doğal şekilde belirt.
- Karakterine uygun emojiler kullan.
"""


@app.get("/health")
def health():
    return {
        "ok": True,
        "gemini": bool(GEMINI_API_KEY),
        "secret": bool(API_SECRET_KEY),
        "model": MODEL_NAME,
        "version": "3.1"
    }


@app.get("/test-price")
def test_price(hisse: str = "BRSAN"):
    """Fiyatın gerçekten gelip gelmediğini test etmek için"""
    info = get_stock_info(hisse.upper())
    return {
        "ticker": hisse.upper(),
        "sonuc": info if info else "BOŞ DÖNDÜ",
        "zaman": datetime.now(TR_TZ).strftime("%Y-%m-%d %H:%M")
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

    su_an = datetime.now(TR_TZ)
    gunler = ["Pazartesi", "Salı", "Çarşamba", "Perşembe", "Cuma", "Cumartesi", "Pazar"]
    zaman_bilgisi = f"[SİSTEM BİLGİSİ: Bugün {su_an.strftime('%d.%m.%Y')} {gunler[su_an.weekday()]}, Saat: {su_an.strftime('%H:%M')}]"

    if extra_parts:
        user_content = (
            zaman_bilgisi + "\n\n"
            + "--- CANLI VERİ ---\n"
            + "\n\n".join(extra_parts)
            + "\n\n--- KULLANICI SORUSU ---\n"
            + soru
        )
    else:
        user_content = zaman_bilgisi + "\n\n" + soru

    cevap = None
    for attempt in range(2):
        try:
            client = _get_client()
            response = client.models.generate_content(
                model=MODEL_NAME,
                contents=user_content,
                config={
                    "system_instruction": SYSTEM_PROMPT,
                    "temperature": 0.65,
                    "max_output_tokens": 3000,
                },
            )
            cevap = (response.text or "").strip()
            if cevap:
                break
        except Exception as e:
            print(f"Gemini hata (attempt {attempt+1}): {e}")
            if attempt == 0:
                time.sleep(1.5)
            continue

    if not cevap:
        if tickers:
            cevap = f"Kanki {tickers[0]} için şu an net rakam çekemedim ama volatil bir hisse, stop’unu ihmal etme."
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
    return HTMLResponse("<h2>Lira API v3.1</h2><p><a href='/health'>/health</a> | <a href='/test-price'>/test-price</a></p>")
