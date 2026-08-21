#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Money Maker — Lira'ya Sor API (Fintables Evo köprüsü)
Akıllı sürüm: Chrome TLS taklidi + retry + backoff
"""

from __future__ import annotations

import json
import os
import time
import uuid
from typing import Optional

from fastapi import FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

try:
    from curl_cffi import requests as cf_requests
except ImportError:
    raise SystemExit("curl_cffi gerekli: pip install curl_cffi")

BASE = "https://agents.fintables.com"
TIMEOUT = int(os.environ.get("EVO_TIMEOUT", "180"))
MODEL = os.environ.get("EVO_MODEL", "fintables:fast")
WEB = os.environ.get("EVO_WEB", "true").lower() in ("1", "true", "yes")
API_SECRET_KEY = (os.environ.get("API_SECRET_KEY") or "").strip()

# Denenecek Chrome profilleri (sırayla)
IMPERSONATE_LIST = [
    p.strip()
    for p in os.environ.get("IMPERSONATE_LIST", "chrome131,chrome124,chrome120").split(",")
    if p.strip()
]

MAX_RETRIES = int(os.environ.get("EVO_RETRIES", "3"))
RETRY_WAIT = float(os.environ.get("EVO_RETRY_WAIT", "2.5"))

app = FastAPI(title="Money Maker Lira'ya Sor", version="1.1")

app.add_middleware(
    CORSMiddleware,
    allow_origins=os.environ.get("CORS_ORIGINS", "*").split(","),
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
)


class SorRequest(BaseModel):
    soru: str = Field(..., min_length=2, max_length=2000)
    thread_id: Optional[str] = None
    new_thread: bool = True  # varsayılan: her zaman yeni thread
    web: Optional[bool] = None


class SorResponse(BaseModel):
    ok: bool
    soru: str
    cevap: str
    thread_id: str
    kaynak: str = ""


def _check_secret(x_api_key: Optional[str]) -> None:
    if not API_SECRET_KEY:
        raise HTTPException(500, "API_SECRET_KEY tanımlı değil (Render env)")
    if not x_api_key or x_api_key != API_SECRET_KEY:
        raise HTTPException(403, "Erişim reddedildi. Geçersiz API anahtarı.")


def _bearer() -> str:
    t = (os.environ.get("BEARER_TOKEN") or "").strip()
    if not t:
        raise HTTPException(500, "BEARER_TOKEN tanımlı değil (Render env)")
    return t


def _cookie() -> str:
    full = (os.environ.get("FULL_COOKIE") or "").strip()
    if full:
        return full
    c = (os.environ.get("CFLB_COOKIE") or "").strip()
    if not c:
        return ""
    return c if c.startswith("__cflb=") else f"__cflb={c}"


def _headers() -> dict:
    """Tarayıcıya yakın header seti."""
    h = {
        "Accept": "*/*",
        "Accept-Language": "tr-TR,tr;q=0.9,en-US;q=0.8,en;q=0.7",
        "Content-Type": "application/json",
        "Origin": "https://fintables.com",
        "Referer": "https://fintables.com/evo",
        "Authorization": f"Bearer {_bearer()}",
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/131.0.0.0 Safari/537.36"
        ),
        "sec-ch-ua": '"Google Chrome";v="131", "Chromium";v="131", "Not_A Brand";v="24"',
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"Windows"',
        "sec-fetch-dest": "empty",
        "sec-fetch-mode": "cors",
        "sec-fetch-site": "same-site",
    }
    ck = _cookie()
    if ck:
        h["Cookie"] = ck
    return h


def _payload(question: str, web: bool) -> dict:
    return {
        "input": {
            "messages": [
                {"id": str(uuid.uuid4()), "type": "human", "content": question}
            ]
        },
        "context": {"model": MODEL, "web": web},
        "metadata": {
            "assistant_id": "evo",
            "model": MODEL,
            "web": web,
            "surface": "web",
        },
        "stream_mode": ["messages-tuple", "values"],
        "stream_resumable": True,
        "assistant_id": "evo",
        "on_disconnect": "continue",
    }


def _from_obj(obj) -> str:
    if obj is None:
        return ""
    if isinstance(obj, str):
        return obj
    if isinstance(obj, list):
        parts = [_from_obj(x) for x in obj]
        parts = [p for p in parts if p]
        if parts and all(len(p) < 120 for p in parts[:30]):
            return "".join(parts)
        return "\n".join(parts)
    if not isinstance(obj, dict):
        return str(obj)
    for key in ("content", "text", "delta", "answer", "output", "token"):
        if key in obj and obj[key] is not None:
            val = obj[key]
            if isinstance(val, str):
                return val
            if isinstance(val, dict):
                if isinstance(val.get("content"), str):
                    return val["content"]
                return _from_obj(val)
            if isinstance(val, list):
                return _from_obj(val)
    if obj.get("type") in ("ai", "AIMessage", "AIMessageChunk", "assistant"):
        return _from_obj(obj.get("content"))
    for key in ("data", "event", "payload", "item", "result", "message"):
        if key in obj:
            t = _from_obj(obj[key])
            if t:
                return t
    msgs = obj.get("messages")
    if isinstance(msgs, list) and msgs:
        for m in reversed(msgs):
            if isinstance(m, dict) and m.get("type") in ("ai", "AIMessage", "assistant"):
                t = _from_obj(m)
                if t:
                    return t
        return _from_obj(msgs[-1])
    return ""


def parse_sse(raw: str) -> str:
    chunks: list[str] = []
    full_answers: list[str] = []
    for line in raw.splitlines():
        line = line.strip()
        if not line.startswith("data:"):
            continue
        data = line[5:].strip()
        if not data or data == "[DONE]":
            continue
        try:
            obj = json.loads(data)
        except Exception:
            chunks.append(data)
            continue
        if isinstance(obj, dict) and "event" in obj and "data" in obj:
            payload = obj.get("data")
            if isinstance(payload, list) and len(payload) >= 2:
                maybe = payload[1] if payload[0] == "messages" else payload[-1]
                t = _from_obj(maybe)
                if t:
                    chunks.append(t)
            else:
                t = _from_obj(payload)
                if t:
                    chunks.append(t)
            continue
        t = _from_obj(obj)
        if t:
            if isinstance(obj, dict) and obj.get("messages"):
                full_answers.append(t)
            else:
                chunks.append(t)
    if full_answers:
        best = max(full_answers, key=len)
        if len(best) > 40:
            return best.strip()
    if not chunks:
        return ""
    if all(len(c) < 100 for c in chunks[:40]):
        joined = "".join(chunks)
        if len(joined) > 40:
            return joined.strip()
    out: list[str] = []
    for c in chunks:
        c = c.strip()
        if c and (not out or c != out[-1]):
            out.append(c)
    return "\n".join(out).strip()


def _is_cloudflare(status: int, text: str) -> bool:
    if status not in (401, 403, 429, 503):
        return False
    low = (text or "").lower()
    return (
        "just a moment" in low
        or "cloudflare" in low
        or "cf-ray" in low
        or "attention required" in low
    )


def _post_with_retry(url: str, json_body: dict, timeout: int = 30):
    """
    Farklı Chrome impersonate + birkaç deneme.
    403/CF olursa bekle, tekrar dene.
    """
    last_err = None
    headers = _headers()

    for attempt in range(1, MAX_RETRIES + 1):
        for imp in IMPERSONATE_LIST:
            try:
                r = cf_requests.post(
                    url,
                    headers=headers,
                    json=json_body,
                    impersonate=imp,
                    timeout=timeout,
                )
            except Exception as e:
                last_err = e
                continue

            # Başarı
            if r.status_code < 400:
                return r

            # Cloudflare / rate limit → bekle, tekrar
            if _is_cloudflare(r.status_code, r.text):
                last_err = f"CF/Auth {r.status_code} ({imp})"
                if attempt < MAX_RETRIES:
                    time.sleep(RETRY_WAIT * attempt)
                continue

            # Diğer hatalar (404 vs.) — retry etme
            return r

        # Tüm impersonate bitti, bir tur daha
        if attempt < MAX_RETRIES:
            time.sleep(RETRY_WAIT * attempt)

    if last_err:
        raise HTTPException(403, f"Cloudflare engeli (retry bitti): {last_err}")
    raise HTTPException(502, "Evo isteği başarısız")


def create_thread() -> Optional[str]:
    for body in (
        {"metadata": {"assistant_id": "evo"}},
        {},
        {"assistant_id": "evo"},
    ):
        try:
            r = _post_with_retry(f"{BASE}/threads", body, timeout=30)
            if r.status_code in (200, 201):
                data = r.json()
                tid = (
                    data.get("thread_id")
                    or data.get("id")
                    or (data.get("thread") or {}).get("thread_id")
                    or (data.get("thread") or {}).get("id")
                )
                if tid:
                    return str(tid)
        except HTTPException:
            continue
        except Exception:
            continue
    return None


def ask_evo(question: str, thread_id: str, web: bool) -> tuple[str, str]:
    url = f"{BASE}/threads/{thread_id}/runs/stream"
    r = _post_with_retry(url, _payload(question, web), timeout=TIMEOUT)

    raw = r.text
    ct = (r.headers.get("content-type") or "").lower()

    if r.status_code in (401, 403) or _is_cloudflare(r.status_code, raw):
        raise HTTPException(
            403,
            "Cloudflare/Auth engeli. BEARER_TOKEN + FULL_COOKIE yenile "
            "(tercihen cf_clearance dahil tüm Cookie satırı).",
        )
    if r.status_code == 404:
        raise HTTPException(404, f"Thread yok: {thread_id}")
    if r.status_code >= 400:
        raise HTTPException(r.status_code, raw[:300])

    if "text/event-stream" in ct or "data:" in raw[:200]:
        text = parse_sse(raw)
    else:
        try:
            text = _from_obj(r.json()) or raw
        except Exception:
            text = raw

    text = (text or "").strip()
    if len(text) < 5:
        raise HTTPException(502, "Boş cevap")
    return text, url


@app.get("/health")
def health():
    return {
        "ok": True,
        "bearer": bool(os.environ.get("BEARER_TOKEN")),
        "cookie": bool(os.environ.get("CFLB_COOKIE") or os.environ.get("FULL_COOKIE")),
        "secret": bool(API_SECRET_KEY),
        "impersonate": IMPERSONATE_LIST,
        "retries": MAX_RETRIES,
    }


@app.post("/api/lira-sor", response_model=SorResponse)
def lira_sor(req: SorRequest, x_api_key: Optional[str] = Header(None)):
    _check_secret(x_api_key)

    web = WEB if req.web is None else req.web
    thread_id = (req.thread_id or os.environ.get("DEFAULT_THREAD_ID") or "").strip()

    # Her zaman mümkünse yeni thread
    if req.new_thread or not thread_id:
        tid = create_thread()
        if tid:
            thread_id = tid
        elif not thread_id:
            raise HTTPException(
                502,
                "Yeni thread açılamadı (Cloudflare?). "
                "FULL_COOKIE / BEARER_TOKEN yenile veya DEFAULT_THREAD_ID kullan.",
            )

    try:
        cevap, kaynak = ask_evo(req.soru.strip(), thread_id, web)
    except HTTPException as e:
        # 403/404 ise bir kez daha yeni thread dene
        if e.status_code in (403, 404):
            tid = create_thread()
            if tid:
                cevap, kaynak = ask_evo(req.soru.strip(), tid, web)
                thread_id = tid
            else:
                raise
        else:
            raise

    return SorResponse(
        ok=True,
        soru=req.soru.strip(),
        cevap=cevap,
        thread_id=thread_id,
        kaynak=kaynak,
    )


@app.get("/", response_class=HTMLResponse)
@app.get("/widget", response_class=HTMLResponse)
def widget():
    return HTMLResponse("<p>Lira API ayakta. POST /api/lira-sor kullan.</p>")
