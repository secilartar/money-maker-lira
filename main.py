#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Money Maker — Lira'ya Sor API (Gemini)
Temiz, Cloudflare'sız, modern google-genai sürümü
"""

from __future__ import annotations

import os
from typing import Optional

from fastapi import FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field
from google import genai

# ================== AYARLAR ==================
API_SECRET_KEY = (os.environ.get("API_SECRET_KEY") or "").strip()
GEMINI_API_KEY = (os.environ.get("GEMINI_API_KEY") or "").strip()
MODEL_NAME = os.environ.get("GEMINI_MODEL", "gemini-2.0-flash")  # istersen gemini-1.5-flash / gemini-1.5-pro

app = FastAPI(
    title="Money Maker Lira'ya Sor (Gemini)",
    version="2.1",
    description="Lira asistanı – Gemini destekli"
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


SYSTEM_PROMPT = """
Sen Lira'sın. Türkçe konuşan, samimi ama profesyonel bir finans asistanısın.
BIST hisseleri, yatırım fonları, KAP haberleri, yatırımcı sayıları, ekonomik veriler konusunda yardımcı olursun.

Kurallar:
- Cevaplarını net, anlaşılır ve mümkün olduğunca kısa tut.
- Tablo veya liste gerektiğinde Markdown kullan.
- Yatırım tavsiyesi VERME. Sadece bilgi ve analiz sun.
- Güncel veriye ihtiyacın varsa dürüstçe belirt (senin bilginin kesim tarihi vardır).
- Kullanıcıya "kanki", "hocam" gibi samimi hitap edebilirsin ama abartma.
"""


# ================== ENDPOINTLER ==================
@app.get("/health")
def health():
    return {
        "ok": True,
        "gemini": bool(GEMINI_API_KEY),
        "secret": bool(API_SECRET_KEY),
        "model": MODEL_NAME,
        "version": "2.1"
    }


@app.post("/api/lira-sor", response_model=SorResponse)
def lira_sor(req: SorRequest, x_api_key: Optional[str] = Header(None)):
    _check_secret(x_api_key)

    client = _get_client()

    try:
        response = client.models.generate_content(
            model=MODEL_NAME,
            contents=[
                {"role": "user", "parts": [{"text": SYSTEM_PROMPT}]},
                {"role": "model", "parts": [{"text": "Anladım, Lira olarak hazırım. Ne sormak istersin?"}]},
                {"role": "user", "parts": [{"text": req.soru.strip()}]},
            ],
            config={
                "temperature": 0.45,
                "max_output_tokens": 2048,
            }
        )

        cevap = (response.text or "").strip()
        if not cevap:
            raise HTTPException(status_code=502, detail="Gemini boş cevap döndü")

    except Exception as e:
        # Gemini hata mesajını biraz temizle
        err = str(e)
        if "API key" in err or "401" in err or "403" in err:
            raise HTTPException(status_code=502, detail="Gemini API Key hatası. Anahtarı kontrol et.")
        raise HTTPException(status_code=502, detail=f"Gemini hatası: {err[:350]}")

    return SorResponse(
        ok=True,
        soru=req.soru.strip(),
        cevap=cevap,
        thread_id="gemini",
        kaynak="gemini"
    )


@app.get("/", response_class=HTMLResponse)
@app.get("/widget", response_class=HTMLResponse)
def root():
    return HTMLResponse(
        "<h2>Lira API (Gemini) ayakta</h2>"
        "<p>POST <code>/api/lira-sor</code> kullan.</p>"
        "<p>Health: <a href='/health'>/health</a></p>"
    )
