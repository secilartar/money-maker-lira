#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Money Maker — Lira'ya Sor API (Fintables Evo köprüsü)
Render'da ayrı servis olarak çalışır.
"""

from __future__ import annotations

import json
import os
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
IMPERSONATE = os.environ.get("IMPERSONATE", "chrome131")
TIMEOUT = int(os.environ.get("EVO_TIMEOUT", "180"))
MODEL = os.environ.get("EVO_MODEL", "fintables:fast")
WEB = os.environ.get("EVO_WEB", "true").lower() in ("1", "true", "yes")
API_SECRET_KEY = (os.environ.get("API_SECRET_KEY") or "").strip()

app = FastAPI(title="Money Maker Lira'ya Sor", version="1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=os.environ.get("CORS_ORIGINS", "*").split(","),
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
)


class SorRequest(BaseModel):
    soru: str = Field(..., min_length=2, max_length=2000)
    thread_id: Optional[str] = None
    new_thread: bool = False
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
    h = {
        "Accept": "*/*",
        "Accept-Language": "tr,en;q=0.9",
        "Content-Type": "application/json",
        "Origin": "https://fintables.com",
        "Referer": "https://fintables.com/",
        "Authorization": f"Bearer {_bearer()}",
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


def create_thread() -> Optional[str]:
    for body in ({"metadata": {"assistant_id": "evo"}}, {}, {"assistant_id": "evo"}):
        try:
            r = cf_requests.post(
                f"{BASE}/threads",
                headers=_headers(),
                json=body,
                impersonate=IMPERSONATE,
                timeout=30,
            )
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
        except Exception:
            pass
    return None


def ask_evo(question: str, thread_id: str, web: bool) -> tuple[str, str]:
    url = f"{BASE}/threads/{thread_id}/runs/stream"
    try:
        r = cf_requests.post(
            url,
            headers=_headers(),
            json=_payload(question, web),
            impersonate=IMPERSONATE,
            timeout=TIMEOUT,
        )
    except Exception as e:
        raise HTTPException(502, f"Evo bağlantı hatası: {e}") from e

    raw = r.text
    ct = (r.headers.get("content-type") or "").lower()

    if r.status_code in (401, 403):
        raise HTTPException(
            403,
            "Cloudflare/Auth engeli. BEARER_TOKEN ve CFLB_COOKIE'yi Render env'de yenile.",
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
    }


@app.post("/api/lira-sor", response_model=SorResponse)
def lira_sor(req: SorRequest, x_api_key: Optional[str] = Header(None)):
    _check_secret(x_api_key)

    web = WEB if req.web is None else req.web
    thread_id = (req.thread_id or os.environ.get("DEFAULT_THREAD_ID") or "").strip()

    if req.new_thread or not thread_id:
        tid = create_thread()
        if tid:
            thread_id = tid
        elif not thread_id:
            raise HTTPException(502, "Yeni thread açılamadı")

    try:
        cevap, kaynak = ask_evo(req.soru.strip(), thread_id, web)
    except HTTPException as e:
        if e.status_code in (403, 404) and not req.new_thread:
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


# ---- Widget (siteden iframe veya direkt açılabilir) ----
WIDGET_HTML = """<!DOCTYPE html>
<html lang="tr">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>Lira'ya Sor — Money Maker</title>
<style>
  :root {
    --bg: #0f1419;
    --card: #1a2332;
    --border: #2a3544;
    --text: #e7ecf3;
    --muted: #8b9bb4;
    --accent: #3d9cf0;
    --accent2: #2dd4a8;
  }
  * { box-sizing: border-box; }
  body {
    margin: 0; font-family: system-ui, -apple-system, Segoe UI, Roboto, sans-serif;
    background: var(--bg); color: var(--text);
    min-height: 100vh; display: flex; align-items: flex-start; justify-content: center;
    padding: 24px 12px;
  }
  .wrap { width: 100%; max-width: 720px; }
  h1 { font-size: 1.25rem; margin: 0 0 4px; font-weight: 650; }
  .sub { color: var(--muted); font-size: 0.85rem; margin-bottom: 16px; }
  .card {
    background: var(--card); border: 1px solid var(--border);
    border-radius: 14px; padding: 16px;
  }
  textarea {
    width: 100%; min-height: 90px; resize: vertical;
    background: #0d1218; color: var(--text);
    border: 1px solid var(--border); border-radius: 10px;
    padding: 12px 14px; font-size: 0.95rem; line-height: 1.45;
  }
  textarea:focus { outline: 2px solid var(--accent); border-color: transparent; }
  .row { display: flex; gap: 10px; margin-top: 12px; flex-wrap: wrap; }
  button {
    border: 0; border-radius: 10px; padding: 10px 18px;
    font-weight: 600; cursor: pointer; font-size: 0.95rem;
  }
  .btn-primary { background: linear-gradient(135deg, var(--accent), #2563eb); color: #fff; }
  .btn-primary:disabled { opacity: 0.55; cursor: wait; }
  .btn-ghost { background: transparent; color: var(--muted); border: 1px solid var(--border); }
  #status { margin-top: 12px; font-size: 0.85rem; color: var(--muted); min-height: 1.2em; }
  #answer {
    margin-top: 14px; white-space: pre-wrap; line-height: 1.55;
    font-size: 0.95rem; display: none;
    background: #0d1218; border: 1px solid var(--border);
    border-radius: 10px; padding: 14px;
  }
  #answer.show { display: block; }
  .badge {
    display: inline-block; font-size: 0.7rem; padding: 2px 8px;
    border-radius: 999px; background: #14322a; color: var(--accent2);
    margin-bottom: 8px;
  }
</style>
</head>
<body>
  <div class="wrap">
    <h1>🎙️ Lira'ya Sor</h1>
    <p class="sub">Money Maker · Fintables Evo köprüsü · Yatırım tavsiyesi değildir</p>
    <div class="card">
      <textarea id="q" placeholder="Örn: AKGRT taşıyan fonlar hangileri? / TLY yatırımcı sayısı son 1 hafta"></textarea>
      <div class="row">
        <button class="btn-primary" id="go" type="button">Sor</button>
        <button class="btn-ghost" id="clr" type="button">Temizle</button>
      </div>
      <div id="status"></div>
      <div id="answer"></div>
    </div>
  </div>
<script>
  // Widget kendi origin'inde çalışır; API path relative.
  // Siteden iframe ile açacaksan API secret'ı burada kullanma — backend proxy önerilir.
  const API = "";

  const q = document.getElementById("q");
  const go = document.getElementById("go");
  const clr = document.getElementById("clr");
  const status = document.getElementById("status");
  const answer = document.getElementById("answer");

  async function ask() {
    const soru = (q.value || "").strip();
    if (!soru) { status.textContent = "Soru yaz."; return; }
    go.disabled = true;
    status.textContent = "Lira düşünüyor…";
    answer.classList.remove("show");
    answer.textContent = "";
    try {
      const r = await fetch((API || "") + "/api/lira-sor", {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          // Widget doğrudan secret göndermesin; siteden çağırırken kendi backend'in üzerinden geçir.
        },
        body: JSON.stringify({ soru, new_thread: true }),
      });
      const data = await r.json().catch(() => ({}));
      if (!r.ok) {
        status.textContent = data.detail || ("Hata " + r.status);
        return;
      }
      status.textContent = "Hazır";
      answer.innerHTML = '<div class="badge">Lira</div>' +
        escapeHtml(data.cevap || "");
      answer.classList.add("show");
    } catch (e) {
      status.textContent = "Bağlantı hatası: " + e;
    } finally {
      go.disabled = false;
    }
  }

  function escapeHtml(s) {
    return String(s)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;")
      .replace(/>/g, "&gt;").replace(/\\n/g, "<br>");
  }

  go.addEventListener("click", ask);
  clr.addEventListener("click", () => {
    q.value = ""; answer.classList.remove("show"); answer.textContent = "";
    status.textContent = "";
  });
  q.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) ask();
  });
</script>
</body>
</html>
"""


@app.get("/", response_class=HTMLResponse)
def widget():
    return HTMLResponse(WIDGET_HTML)


@app.get("/widget", response_class=HTMLResponse)
def widget_alias():
    return HTMLResponse(WIDGET_HTML)
