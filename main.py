#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Money Maker — Lira'ya Sor API (Gemini) v3.4 (Yfinance + Firebase Ham Veri Eklentili)
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
from supabase import create_client, Client

from google import genai
from google.genai import types

from constants import BEYAZ_LISTE, VERI_SOZLUGU

# ================== AYARLAR ==================
API_SECRET_KEY = (os.environ.get("API_SECRET_KEY") or "").strip()
GEMINI_API_KEY = (os.environ.get("GEMINI_API_KEY") or "").strip()
MODEL_NAME = os.environ.get("GEMINI_MODEL", "gemini-3.8-flash")

# Fallback modeller (yoğunluk / 503 durumunda sırayla dener)
GEMINI_MODELS = [
    "gemini-3.8-flash",
    "gemini-3.7-flash",
    "gemini-3.6-flash",
    "gemini-3.5-flash-lite",
]

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

# --- SUPABASE AYARLARI ---
SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY", "")

supabase: Optional[Client] = None
if SUPABASE_URL and SUPABASE_KEY:
    try:
        supabase = create_client(SUPABASE_URL, SUPABASE_KEY)
        print("[SİSTEM] Supabase bağlantısı başarılı!")
    except Exception as e:
        print(f"[SİSTEM] Supabase başlatılamadı: {e}")

KAP_API = "https://www.kap.org.tr/tr/api/disclosure/members/byCriteria"
KAP_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Content-Type": "application/json",
    "Referer": "https://www.kap.org.tr/tr/bildirim-sorgu",
    "Origin": "https://www.kap.org.tr",
}

TR_TZ = timezone(timedelta(hours=3))

app = FastAPI(title="Money Maker Lira'ya Sor", version="3.4")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
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


def generate_with_retry(user_content: str, max_attempts: int = 4):
    """Model listesinde sırayla dener, 503/yüksek talep durumunda diğer modele geçer."""
    last_err = None
    client = _get_client()

    for attempt in range(max_attempts):
        model = GEMINI_MODELS[min(attempt, len(GEMINI_MODELS) - 1)]
        try:
            chat = client.chats.create(
                model=model,
                config=types.GenerateContentConfig(
                    system_instruction=SYSTEM_PROMPT,
                    temperature=0.65,
                    max_output_tokens=8192,
                )
            )
            response = chat.send_message(user_content)
            cevap = (response.text or "").strip()

            # Çok kısa geldiyse bir kez daha zorla
            if cevap and len(cevap) < 900:
                print(f"[Gemini-{model}] Cevap kısa geldi ({len(cevap)} karakter), tekrar deniyorum...")
                response = chat.send_message(
                    user_content + "\n\nLütfen cevabı yarıda kesme, sonuna kadar detaylı ve tamamlanmış şekilde yaz."
                )
                cevap = (response.text or "").strip()

            if cevap:
                print(f"[Gemini] Başarılı → {model}")
                return cevap

        except Exception as e:
            last_err = e
            msg = str(e).lower()
            retryable = any(x in msg for x in (
                "503", "unavailable", "429", "resource_exhausted",
                "high demand", "overloaded", "try again"
            ))
            print(f"[Gemini] {model} hata (attempt {attempt+1}): {e}")
            if retryable and attempt < max_attempts - 1:
                time.sleep(min(1.5 * (2 ** attempt), 12))
                continue
            if attempt < max_attempts - 1:
                time.sleep(1.5)
                continue

    raise last_err if last_err else Exception("Tüm modeller başarısız")


def extract_tickers(text: str) -> list[str]:
    candidates = re.findall(r'\b([A-Z]{3,6})\b', text.upper())
    return [c for c in set(candidates) if c in BEYAZ_LISTE]


# ================== YENİ: FIREBASE HİSSE HAM VERİSİ (ÇEVİRİCİ İLE) ==================
def get_stock_info_from_firebase(ticker: str) -> str:
    if not firebase_admin._apps:
        return ""
    
    ticker = ticker.upper().strip()
    try:
        ref = db.reference(f"veriler/Hisseler/{ticker}")
        data = ref.get()
        
        if not data:
            return ""

        temiz_veri = {}
        for key, val in data.items():
            temiz_key = re.sub(r'^(X|SA13)', '', key, flags=re.IGNORECASE).upper()
            turkce_key = VERI_SOZLUGU.get(temiz_key, key)
            temiz_veri[turkce_key] = val

        ham_veri_metni = json.dumps(temiz_veri, ensure_ascii=False, indent=2)
        
        return f"**{ticker} İNDİKATÖR VERİLERİ:**\n```json\n{ham_veri_metni}\n```"
    except Exception as e:
        print(f"[Firebase Hisse Hata] {ticker}: {e}")
        return ""


# ================== FIREBASE FON BİLGİSİ ==================
def get_fon_info(ticker: str, soru: str = "") -> str:
    if not firebase_admin._apps:
        return ""

    ticker = ticker.upper().strip()
    soru_lower = soru.lower()

    try:
        poz_ref = db.reference("veriler/Pozisyonlar")

        once_fon_dene = ("fon" in soru_lower) or (len(ticker) == 3)

        if once_fon_dene:
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

                items.sort(key=lambda x: x[1], reverse=True)

                for hisse, agirlik, lot, degisim in items[:20]:
                    deg_str = f" | Değişim: %{degisim:+.2f}" if degisim is not None else ""
                    lines.append(f"- {hisse}: Ağırlık %{agirlik:.2f} | Lot: {int(lot):,}{deg_str}")

                return "\n".join(lines)

        by_hisse_raw = poz_ref.order_by_child("hisse_kodu").equal_to(ticker).get()
        if by_hisse_raw:
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

            items.sort(key=lambda x: x[2], reverse=True)

            for fon, agirlik, lot, degisim in items[:15]:
                deg_str = f" | Değişim: %{degisim:+.2f}" if degisim is not None else ""
                lines.append(f"- {fon}: Ağırlık %{agirlik:.2f} | Lot: {int(lot):,}{deg_str}")

            return "\n".join(lines)

        return ""
    except Exception as e:
        print(f"[Firebase Hata] {ticker} okunamadı: {e}")
        return ""


# ================== SUPABASE SPOT/VİOP RAPORLARI ==================
def get_supabase_reports(ticker: str) -> str:
    if not supabase:
        return ""
    
    ticker = ticker.upper().strip()
    sonuc_metni = ""
    
    try:
        rapor_resp = supabase.table("piyasa_raporlari").select("*").limit(1).execute()
        if rapor_resp.data:
            rapor_str = json.dumps(rapor_resp.data, ensure_ascii=False, indent=2)
            sonuc_metni += (
                f"**GÜNLÜK PİYASA RAPORU (Supabase - 21.08.2026 tarzı):**\n"
                f"```json\n{rapor_str}\n```\n"
                f"(Not: Sadece {ticker} ile ilgili kısımları kullan, diğerlerini yoksay)\n\n"
            )
                
        yorum_resp = supabase.table("piyasa_yorumu").select("*").limit(1).execute()
        if yorum_resp.data:
            yorum_str = json.dumps(yorum_resp.data, ensure_ascii=False, indent=2)
            sonuc_metni += (
                f"**GÜNLÜK PİYASA YORUMU (Supabase):**\n"
                f"```json\n{yorum_str}\n```\n"
                f"(Not: Sadece {ticker} ile ilgili model/balina notlarını kullan)\n\n"
            )
                
        return sonuc_metni.strip()
        
    except Exception as e:
        print(f"[Supabase Hata] {ticker}: {e}")
        return ""
        

# ================== YFINANCE HİSSE FİYATI ==================
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
            f"**{ticker} YFINANCE CANLI PİYASA:**\n"
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
        print(f"[yfinance hata] {ticker}: {e}")
        return ""
        

def fetch_kap_for_ticker(ticker: str, days: int = 7) -> str:
    """Son X günün KAP ODA bildirimlerini çeker."""
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
        with httpx.Client(timeout=12, follow_redirects=True) as client:
            r = client.post(KAP_API, json=payload, headers=KAP_HEADERS)
            if r.status_code == 200:
                raw = r.json()
                data = raw if isinstance(raw, list) else []
            else:
                print(f"[KAP] HTTP {r.status_code} for {ticker}")
    except Exception as e:
        print(f"[KAP hata] {ticker}: {e}")
        return ""

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
        
        stock_list = [str(s).upper().strip() for s in stocks]
        
        if ticker_up in stock_list:
            relevant.append(d)

    if not relevant:
        return ""

    relevant = sorted(relevant, key=lambda x: x.get("publishDate") or "", reverse=True)[:5]
    
    lines = [f"**{ticker} – Son KAP Bildirimleri (son {days} gün):**"]
    for d in relevant:
        when = (d.get("publishDate") or "")[:16]
        subj = (d.get("subject") or "").strip()
        summ = (d.get("summary") or "").strip()[:180]
        idx = d.get("disclosureIndex")
        
        lines.append(f"- [{when}] {subj}")
        if summ:
            lines.append(f"  Özet: {summ}")
        if idx:
            lines.append(f"  Link: https://www.kap.org.tr/tr/Bildirim/{idx}")
    
    return "\n".join(lines)
    

# ================== FIREBASE EN SON YAPAY ZEKA ANALİZİ ==================
def get_latest_ai_analysis() -> str:
    if not firebase_admin._apps:
        return ""
    try:
        ref = db.reference("YapayZekaAnaliz")
        latest_data = ref.order_by_key().limit_to_last(1).get()
        
        if not latest_data:
            return ""

        for tarih, icerik in latest_data.items():
            analiz_metni = json.dumps(icerik, ensure_ascii=False, indent=2)
            return f"**EN SON YAPAY ZEKA PİYASA ANALİZİ ({tarih}):**\n```json\n{analiz_metni}\n```"
            
        return ""
    except Exception as e:
        print(f"[Firebase YapayZekaAnaliz Hata]: {e}")
        return ""
        

SYSTEM_PROMPT = """
Sen Lira'sın. Türkçe konuşan, samimi, veri odaklı, detaylı analiz yapabilen ve biraz esprili bir finans asistanısın.

ZORUNLU KURALLAR:

- Veri Öncelik Sırası (çok önemli):
  1. Önce fiyat, kim aldı/sattı (takas, para girişi, en çok alan/satan kurum) ve önemli KAP haberleri.
  2. Sonra Supabase’den gelen piyasa_raporlari ve piyasa_yorumu. Bu tablolarda hisse kodunu filtrele, sadece sorulan hisseyle ilgili kısımları kullan. (3 harfli kodlar genelde fondur, 4+ harfli kodlar hisse kabul et.)
  3. En son Kahin sinyalleri, Devir-Teslim, Likidite Sıkışması, Ping-Pong, fon pozisyonları ve genel AI analizi.

- Kullanıcı bir hisse sorduğunda mutlaka şu sırayla başla:
  1. “Bugün/hafta nasıl kapandı, kim almış satmış, önemli KAP var mı?”
  2. Supabase yorum/raporunda bu hisse geçiyor mu?
  3. Sonra diğer modellere geç.

- Kapanış sonrası / yarın etkisi soruları (çok önemli):
  Kullanıcı “kapanış sonrası”, “seans kapandıktan sonra”, “yarın”, “olumlu etkilenecek”, “haber var mı”, “KAP”, “özel durum” gibi ifadeler kullandığında şu kuralları uygula:
  
  1. Önceliği KAP Özel Durum (ODA) bildirimlerine ver.
  2. Özellikle şu olumlu haber türlerini yarın için potansiyel katalizör olarak öne çıkar:
     - Yeni İş İlişkisi / büyük sipariş / satış sözleşmesi
     - Pay geri alım (fiili alım)
     - Bedelsiz sermaye artırımı / SPK onayı
     - Temettü dağıtım onayı
     - İhale kazanma / ruhsat / lisans
     - Kapasite artışı / önemli proje onayı
  3. Olumsuz haberleri de net şekilde risk olarak belirt:
     - İhale iptali
     - Görüşme sonlandırma
     - Geri alınan payların elden çıkarılması
     - Fesih
     - Dava aleyhe sonuç
  4. Genel sorularda (belirli hisse söylemeden) bugün ve dün gelen en önemli 4-6 özel durum haberini 
     “Hisse + Etiket + Kısa gerekçe” formatında özetle.
  5. Saat 18:10’dan sonra soruluyorsa “kapanış sonrası haber” olduğunu doğal şekilde belirt.
  
  Not: Belirli bir hisse sorulduğunda zaten son günlerin KAP’ına bakmaya devam et. 
  Bu kural sadece genel “yarın ne olur / haber var mı” sorularını güçlendirir. 
  Mevcut öncelik sırasını bozma.
  
- "kanki", "kankitom", "patron" diyebilirsin. Asla "hocam" deme.
- Sana CANLI VERİ (Fiyat, KAP, Özel İndikatörler veya Fon Portföyü) geldiyse, önce güncel verileri değerlendir.
- Özellikle Kahin sinyalleri, haftalık/aylık performans ve SA13 gibi sana iletilen özel indikatör metriklerini yorumuna dahil et.
- Yanıtlarını her zaman detaylı, Markdown ile yapılandırılmış ve okunaklı ver.
- Yatırım tavsiyesi verebilirsin ama riskleri, piyasa volatilitesini ve stop-loss hayat kurtarır gerçeğini hep vurgula.
- Veri yoksa bile "veri ulaşmadı", "API patladı", "aracı kurum", "kap.org.tr" gibi cümleler ASLA KULLANMA.
- Sana verilen SİSTEM SAATİ VE TARİHİ bilgisini dikkate al. Hafta sonuysa doğal şekilde belirt.
- Karakterine uygun emojiler kullan.
- Sana GÜNLÜK PİYASA YORUMU veya RAPORU geldiğinde, bu raporlar genel piyasayı kapsayabilir. Sen SADECE kullanıcının sorduğu hisse ile ilgili (örneğin balina pozisyonları, short/long durumları veya model sinyalleri) kısımları süz ve yorumuna güçlü bir şekilde dahil et. İlgisiz hisseleri yoruma katma.
- Veritabanı Sütun İsimlerini Gizle: Sana gelen JSON veya ham verideki başlıkları (örneğin: Aort, FrkAy1, Al_Hcm) kullanıcıya doğrudan parantez içinde veya metin olarak YAZMA. Bunları "Ağırlıklı Ortalama", "1 Aylık Performans", "Alım Hacmi" gibi tamamen doğal Türkçe ifadelerle açıkla.
- Fon Değişimleri Aylıktır: Fon portföy dağılımları ve hisse ağırlık değişimleri her ay yayımlanır. Bu nedenle fon pozisyonlarındaki değişimlerden bahsederken "son dönemde" veya "geçenlerde" gibi muğlak ifadeler yerine her zaman "aylık bazda", "geçen aya göre" veya "son açıklanan aylık rapora göre" ifadelerini kullan.
- Kahin Kodları ve "Patron" Esprisi: Verilerde Kahin kodları (Z: Aşırı Riskli, KY2: Aşırı Ucuz, KY1: Ucuz, Y2: İskontolu, Y1: Makul, B1: Nötr, B2: Adil, K1: Primli, K2: Pahalı, KK1: Çok Pahalı, KK2: Aşırı Pahalı, KK3: Balon Bölgesi) karşına çıkabilir. Bu kodları mutlak birer kanun gibi ciddiye alma; eğlenceli ve takılarak yaklaş. "Patron buraya [Kod/Anlam] demiş ama tahtanın soluğu başka diyor kanki" ya da "Patrona kalırsa buralar balon ama veriler ne söyler ona bakalım" gibi hafiften patrona laf atarak esprili bir dille yorumla.
"""


@app.get("/health")
def health():
    return {
        "ok": True,
        "gemini": bool(GEMINI_API_KEY),
        "secret": bool(API_SECRET_KEY),
        "firebase": bool(FIREBASE_JSON_STR),
        "model": MODEL_NAME,
        "version": "3.4"
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
        # 1. Yfinance'den ham canlı fiyat
        price = get_stock_info(t)
        if price:
            extra_parts.append(price)

        # 2. Firebase'den özel analiz verilerin
        fb_veri = get_stock_info_from_firebase(t)
        if fb_veri:
            extra_parts.append(fb_veri)

        # 3. KAP bildirimleri
        kap = fetch_kap_for_ticker(t)
        if kap:
            extra_parts.append(kap)
            
        # 4. SUPABASE RAPORLARI VE YORUMLARI
        supa_rapor = get_supabase_reports(t)
        if supa_rapor:
            extra_parts.append(supa_rapor)

        # 5. Fon verileri
        fon = get_fon_info(t, soru)
        if fon:
            extra_parts.append(fon)
            
        time.sleep(0.6)

    # En son yapay zeka analizini genel bağlam olarak ekle
    ai_analiz = get_latest_ai_analysis()
    if ai_analiz:
        extra_parts.append(ai_analiz)
        
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
    try:
        cevap = generate_with_retry(user_content, max_attempts=4)
    except Exception as e:
        print(f"[Gemini] Tüm denemeler başarısız: {e}")

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
    return HTMLResponse("<h2>Lira API v3.4</h2><p><a href='/health'>/health</a> | <a href='/test-price'>/test-price</a></p>")
