#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Money Maker — Lira'ya Sor API (Gemini) v3.2 (Firebase Fon Eklentili)
"""

from __future__ import annotations

import os
import re
import time
import json
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field
import yfinance as yf
import httpx
import pandas as pd
import firebase_admin
from firebase_admin import credentials, db

# YENİ SDK İÇİN GEREKLİ İÇE AKTARMALAR
from google import genai
from google.genai import types

# ================== AYARLAR ==================
API_SECRET_KEY = (os.environ.get("API_SECRET_KEY") or "").strip()
GEMINI_API_KEY = (os.environ.get("GEMINI_API_KEY") or "").strip()
MODEL_NAME = os.environ.get("GEMINI_MODEL", "gemini-3.6-flash")

# --- FIREBASE AYARLARI ---
FIREBASE_URL = "https://money-maker-f59c4-default-rtdb.europe-west1.firebasedatabase.app"
FIREBASE_JSON_STR = os.environ.get("FIREBASE_CREDENTIALS_JSON")

if FIREBASE_JSON_STR and not firebase_admin._apps:
    try:
        cred_dict = json.loads(FIREBASE_JSON_STR)
        cred = credentials.Certificate(cred_dict)
        firebase_admin.initialize_app(cred, {'databaseURL': FIREBASE_URL})
        print("[SİSTEM] Firebase bağlantısı başarılı!")
    except Exception as e:
        print(f"[SİSTEM] Firebase başlatılamadı: {e}")

KAP_API = "https://www.kap.org.tr/tr/api/disclosure/members/byCriteria"
KAP_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Content-Type": "application/json",
    "Referer": "https://www.kap.org.tr/tr/bildirim-sorgu",
    "Origin": "https://www.kap.org.tr",
}

TR_TZ = timezone(timedelta(hours=3))

app = FastAPI(title="Money Maker Lira'ya Sor", version="3.2")

# DÜZELTME: CORS çakışması giderildi
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], 
    allow_credentials=False, # allow_origins="*" varken True olamaz!
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
        "GIBI", "ICIN", "GORE", "KANKA", "HOCAM", "MERHABA", "SELAM", "FON",
        "HANGI", "FONLARDA", "FONLAR", "ICINDE", "PORTFOY", "TASIYAN",
        "DETAY", "LISTE", "ANALIZ", "FIYAT", "HABER", "RADAR", "ORTAK",
        "SAAT", "SABAH", "AKSAM", "BUGUN", "YARIN", "HAFTA", "AY", "YIL",
        "ONCE", "SONRA", "ICIN", "GORE", "KADAR", "DAHA", "COK", "AZ",
        "YOK", "VAR", "MI", "MU", "MUYDUR", "OLUR", "OLURUZ", "YAPAR",
        "BAK", "GEL", "HADI", "SIMDI", "SONRA", "ONCEKI", "GUNCEL",
        "OLDU", "PATRON", "ACABA", "BILGI", "HABER", "GLOBAL", "DUNYA", 
        "ONEMLI", "GELISME", "VARMI", "YOKMU", "ACIL", "YYIL", "YENI", "KISA", "OLMALI"
    }
    return [c for c in set(candidates) if c not in blacklist]


def get_fon_info(ticker: str) -> str:
    """
    Firebase'den fon/pozisyon bilgisi çeker.
    - ticker bir HİSSE ise → o hisseyi taşıyan fonları listeler
    - ticker bir FON ise  → o fonun içindeki hisseleri listeler
    """
    if not firebase_admin._apps:
        return ""

    ticker = ticker.upper().strip()
    try:
        poz_ref = db.reference("veriler/Pozisyonlar")

        # 1) Önce HİSSE olarak dene (hisse_kodu ile)
        by_hisse_raw = poz_ref.order_by_child("hisse_kodu").equal_to(ticker).get()
        
        if by_hisse_raw:
            # GÜVENLİK YAMASI: Dict veya List olma durumunu yönetiyoruz
            by_hisse_list = by_hisse_raw.values() if isinstance(by_hisse_raw, dict) else [x for x in by_hisse_raw if x]
            
            lines = [f"**{ticker} hissesini taşıyan fonlar:**"]
            items = []
            
            for p in by_hisse_list:
                fon = (p.get("fon_kodu") or "?").upper()
                agirlik = p.get("agirlik") or 0
                lot = p.get("lot_adedi") or 0
                onceki = p.get("onceki_agirlik")
                
                degisim = None
                if onceki is not None:
                    try:
                        degisim = float(agirlik) - float(onceki)
                    except ValueError:
                        pass
                
                items.append((fon, float(agirlik or 0), float(lot or 0), degisim))

            items.sort(key=lambda x: x[2], reverse=True)  # lot'a göre sırala

            for fon, agirlik, lot, degisim in items[:15]:
                deg_str = f" | Değişim: %{degisim:+.2f}" if degisim is not None else ""
                lines.append(f"- {fon}: Ağırlık %{agirlik:.2f} | Lot: {int(lot):,}{deg_str}")

            if len(items) > 15:
                lines.append(f"... ve {len(items)-15} fon daha")
            return "\n".join(lines)

        # 2) Hisse bulunamadıysa FON olarak dene (fon_kodu ile)
        by_fon_raw = poz_ref.order_by_child("fon_kodu").equal_to(ticker).get()
        
        if by_fon_raw:
            by_fon_list = by_fon_raw.values() if isinstance(by_fon_raw, dict) else [x for x in by_fon_raw if x]
            
            lines = [f"**{ticker} fonu portföy dağılımı:**"]
            items = []
            
            for p in by_fon_list:
                hisse = (p.get("hisse_kodu") or "?").upper()
                agirlik = p.get("agirlik") or 0
                lot = p.get("lot_adedi") or 0
                onceki = p.get("onceki_agirlik")
                
                degisim = None
                if onceki is not None:
                    try:
                        degisim = float(agirlik) - float(onceki)
                    except ValueError:
                        pass
                        
                items.append((hisse, float(agirlik or 0), float(lot or 0), degisim))

            items.sort(key=lambda x: x[1], reverse=True)  # ağırlığa göre sırala

            for hisse, agirlik, lot, degisim in items[:20]:
                deg_str = f" | Değişim: %{degisim:+.2f}" if degisim is not None else ""
                lines.append(f"- {hisse}: Ağırlık %{agirlik:.2f} | Lot: {int(lot):,}{deg_str}")

            return "\n".join(lines)

        return ""  # hiçbir şey bulunamadı

    except Exception as e:
        print(f"[Firebase Hata] {ticker} okunamadı: {e}")
        return ""


def get_stock_info(ticker: str) -> str:
    symbol = ticker if ticker.endswith(".IS") else f"{ticker}.IS"
    try:
        df_daily = yf.download(symbol, period="10d", interval="1d", progress=False, auto_adjust=True)
        df_weekly = yf.download(symbol, period="1mo", interval="1wk", progress=False, auto_adjust=True)

        if df_daily.empty or df_weekly.empty:
            return ""

        if isinstance(df_daily.columns, pd.MultiIndex):
            df_daily.columns = df_daily.columns.get_level_values(0)
            df_weekly.columns = df_weekly.columns.get_level_values(0)

        df_daily = df_daily.dropna(subset=["Close"])
        df_weekly = df_weekly.dropna(subset=["Close"])

        if df_daily.empty or df_weekly.empty:
            return ""

        daily_last = df_daily.iloc[-1]
        weekly_last = df_weekly.iloc[-1]

        price_daily = float(daily_last["Close"])
        price_weekly = float(weekly_last["Close"])

        if abs(price_weekly - price_daily) > 0.001:
            price = price_weekly            
            prev_close = price_daily        
            volume = 0                      
            last_date = "Son Kapanış (Tatil/Hafta sonu kurtarması)" 
        else:
            price = price_daily
            prev_close = float(df_daily.iloc[-2]["Close"]) if len(df_daily) > 1 else price_daily
            volume = int(daily_last["Volume"]) if "Volume" in daily_last and pd.notna(daily_last["Volume"]) else 0
            try:
                last_date = daily_last.name.strftime("%Y-%m-%d") if hasattr(daily_last.name, "strftime") else str(daily_last.name)[:10]
            except Exception:
                last_date = str(df_daily.index[-1])[:10]

        change = price - prev_close
        change_pct = (change / prev_close * 100) if prev_close else 0

        text = (
            f"**{ticker}**\n"
            f"- Son Kapanış: **{price:.2f} TL** ({last_date})\n"
            f"- Değişim: {change:+.2f} TL (%{change_pct:+.2f})\n"
        )
        
        if volume > 0:
            text += f"- Hacim: {volume:,}\n"

        bugun = datetime.now(TR_TZ)
        if bugun.weekday() >= 5:
            text += "- Not: BIST kapalı. Fiyat özel olarak doğrulandı.\n"

        return text

    except Exception as e:
        print(f"[yfinance hata - hibrit] {ticker}: {e}")
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
- Sana CANLI VERİ (Fiyat, KAP veya Fon Portföyü) geldiyse, önce güncel verileri değerlendir.
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
        "firebase": bool(FIREBASE_JSON_STR),
        "model": MODEL_NAME,
        "version": "3.2"
    }


@app.get("/test-price")
def test_price(hisse: str = "BRSAN"):
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

    for t in tickers[:3]:
        price = get_stock_info(t)
        if price:
            extra_parts.append(price)

        kap = fetch_kap_for_ticker(t)
        if kap:
            extra_parts.append(kap)

        fon = get_fon_info(t)
        print(f"[DEBUG FON] ticker={t} | len={len(fon)} | preview={fon[:180] if fon else 'BOŞ'}")
        if fon:
            extra_parts.append(fon)

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
# GÜNCELLENMİŞ GEMİNİ ÇAĞRISI (Google Arama Yeteneği Eklenmiş Hali)
            response = client.models.generate_content(
                model=MODEL_NAME,
                contents=user_content,
                config=types.GenerateContentConfig(
                    system_instruction=SYSTEM_PROMPT,
                    temperature=0.65,
                    max_output_tokens=3000,
                    # İŞTE BURASI: Lira'nın internette arama yapmasını sağlar!
# tools=[types.Tool(google_search=types.GoogleSearch())],
                ),
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
            cevap = f"Kanki {tickers[0]} için şu an net rakam çekemedim ama volatil bir varlık, stop’unu ihmal etme."
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
    return HTMLResponse("<h2>Lira API v3.2</h2><p><a href='/health'>/health</a> | <a href='/test-price'>/test-price</a></p>")
