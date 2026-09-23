#!/usr/bin/env python3
"""AniVox Monitor v6.0.0 (Original Polar Chart Dashboard + Smart Voice Search & Spy)."""

from __future__ import annotations

import base64
import html
import http.server
import json
import logging
import os
import re
import signal
import socket
import socketserver
import ssl
import struct
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Optional

APP_NAME = "AniVox Monitor"
VERSION = "6.0.0"
CONFIG_PATH = Path(__file__).with_name("anivox_monitor.json")
LOG_PATH = Path(__file__).with_name("anivox_monitor.log")
REPORT_PATH = Path(__file__).with_name("anivox_report.html")

PROFILE_RE = re.compile(r"^https?://(?:www\.)?anivox\.fun/profile/([0-9]+)(?:[/?#].*)?$", re.IGNORECASE)
ANIME_RE = re.compile(r"^https?://(?:www\.)?anivox\.fun/anime/([0-9]+)(?:[/?#].*)?$", re.IGNORECASE)

DEFAULT_INTERVAL = 10
DEFAULT_USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", handlers=[logging.StreamHandler(sys.stdout)])
logger = logging.getLogger(APP_NAME)

TELEGRAM_KEYBOARD = {
    "keyboard": [
        ["📊 Проверить всех", "🌐 Открыть сайт-отчет"],
        ["👥 Мои профили", "➕ Добавить профиль", "🗑 Удалить профиль"],
        ["🎬 Аниме трекер", "💬 Чат друзей"],
        ["📈 История", "🎭 Друзья"],
        ["⚙️ Настройки", "🔕 Уведомления"],
        ["⏸ Пауза", "▶️ Продолжить"]
    ],
    "resize_keyboard": True,
}

CANCEL_KEYBOARD = {"keyboard": [["❌ Отмена"]], "resize_keyboard": True}
CHAT_KEYBOARD = {"keyboard": [["🚪 Выйти из чата"]], "resize_keyboard": True}

def almaty_tz() -> timezone:
    return timezone(timedelta(hours=5), name="ALMT")

def compact(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()

def save_json(path: Path, data: dict[str, Any]) -> None:
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temp, path)

def load_json(path: Path) -> dict[str, Any]:
    if not path.exists(): return {}
    try: return json.loads(path.read_text(encoding="utf-8"))
    except: return {}

def normalize_profile_url(value: str) -> tuple[str, str]:
    candidate = value.strip()
    if not candidate.startswith(("http://", "https://")):
        candidate = "https://" + candidate
    parsed = urllib.parse.urlparse(candidate)
    normalized = urllib.parse.urlunparse(("https", parsed.netloc.lower(), parsed.path.rstrip("/"), "", "", ""))
    match = PROFILE_RE.match(normalized)
    if not match:
        raise ValueError("Нужна ссылка вида https://anivox.fun/profile/27788")
    return normalized, match.group(1)

@dataclass
class Profile:
    url: str
    profile_id: str
    label: str = ""
    last_signature: str = ""
    last_check: str = ""
    last_ok: bool = False
    last_error: str = ""
    last_status: str = ""

class Store:
    def __init__(self, path: Path) -> None:
        raw = load_json(path)
        self.path = path
        self.token = str(raw.get("token", "")).strip() or os.getenv("BOT_TOKEN", "").strip()
        self.chat_id = str(raw.get("chat_id", "")).strip() or os.getenv("CHAT_ID", "").strip()
        
        self.allowed_users = [str(u) for u in raw.get("allowed_users", [])]
        if self.chat_id and self.chat_id not in self.allowed_users:
            self.allowed_users.append(self.chat_id)
            
        self.anime_cache = raw.get("anime_cache", {})
        self.anime_subs = raw.get("anime_subs", {})
        
        raw_settings = raw.get("settings", {})
        self.interval_minutes = int(raw_settings.get("interval_minutes", DEFAULT_INTERVAL))
        self.notify_on_change = raw_settings.get("notify_on_change", True)
        
        self.profiles = {}
        for item in raw.get("profiles", []):
            try:
                p = Profile(**item)
                self.profiles[p.profile_id] = p
            except: pass
                
        self.history = raw.get("history", {})

    def save(self) -> None:
        save_json(self.path, {
            "token": self.token, "chat_id": self.chat_id, "allowed_users": self.allowed_users, 
            "anime_cache": self.anime_cache, "anime_subs": self.anime_subs, 
            "settings": {"interval_minutes": self.interval_minutes, "notify_on_change": self.notify_on_change},
            "profiles": [asdict(p) for p in self.profiles.values()],
            "history": self.history,
        })

    def add_history(self, profile_id: str, result: "FetchResult", changes: list[str]) -> None:
        if not result.ok or not changes: return
        entries = self.history.setdefault(profile_id, [])
        entries.append({
            "checked_at": result.checked_at, "ok": result.ok,
            "nickname": result.nickname, "level": result.level,
            "presence": result.presence, "title": result.title,
            "signature": result.signature, "changes": changes
        })
        self.history[profile_id] = entries[-500:]

class TelegramClient:
    def __init__(self, token: str) -> None:
        self.token = token.strip()
        self.base_url = f"https://api.telegram.org/bot{self.token}/"
        self.offset = 0

    def request(self, method: str, payload: dict = None) -> Any:
        body = urllib.parse.urlencode(payload or {}).encode()
        req = urllib.request.Request(self.base_url + method, data=body, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                return json.loads(resp.read().decode("utf-8")).get("result")
        except Exception: return None

    def send_message(self, chat_id: str, text: str, reply_markup: dict = None) -> None:
        self.request("sendMessage", {
            "chat_id": chat_id, "text": text[:4096], "parse_mode": "HTML",
            "disable_web_page_preview": "true", "reply_markup": json.dumps(reply_markup or TELEGRAM_KEYBOARD)
        })

    def edit_message_text(self, chat_id: str, message_id: int, text: str, reply_markup: dict = None) -> None:
        req_data = {"chat_id": chat_id, "message_id": message_id, "text": text, "parse_mode": "HTML"}
        if reply_markup: req_data["reply_markup"] = json.dumps(reply_markup)
        self.request("editMessageText", req_data)

    def answer_callback(self, cb_id: str, text: str) -> None:
        self.request("answerCallbackQuery", {"callback_query_id": cb_id, "text": text})

    def get_updates(self) -> list:
        res = self.request("getUpdates", {"offset": self.offset, "timeout": 2})
        updates = res if isinstance(res, list) else []
        if updates: self.offset = updates[-1]["update_id"] + 1
        return updates

class FetchResult:
    def __init__(self, ok: bool, profile_id: str, url: str, nickname="неизвестно", level="неизвестно", presence="неизвестно", title="", error=""):
        self.ok = ok
        self.profile_id = profile_id
        self.url = url
        self.nickname = nickname
        self.level = level
        self.presence = presence
        self.title = title
        self.error = error
        self.checked_at = datetime.now(almaty_tz()).isoformat(timespec="seconds")
    
    @property
    def signature(self) -> str:
        state = f"watching:{self.title.lower()}" if self.presence.startswith("Смотрит:") else ("online" if "Онлайн" in self.presence else "offline")
        return f"{self.nickname.lower()}|{self.level}|{state}|{self.title.lower()}"

def anivox_websocket_profile(profile_id: str, timeout: int = 15) -> dict:
    host = "anivox.fun"
    raw_socket = socket.create_connection((host, 443), timeout=timeout)
    context = ssl.create_default_context()
    sock = context.wrap_socket(raw_socket, server_hostname=host)
    sock.settimeout(timeout)
    key = base64.b64encode(os.urandom(16)).decode("ascii")
    handshake = (f"GET /api HTTP/1.1\r\nHost: {host}\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                 f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\nOrigin: https://{host}\r\n"
                 f"User-Agent: {DEFAULT_USER_AGENT}\r\n\r\n").encode("ascii")
    try:
        sock.sendall(handshake)
        resp = b""
        while b"\r\n\r\n" not in resp: resp += sock.recv(4096)
        
        data = json.dumps({"type": "get_profile", "id": int(profile_id)}).encode()
        mask = os.urandom(4)
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
        length = len(data)
        header = bytes((0x81, 0x80 | length))
        sock.sendall(header + mask + masked)
        
        dl = time.monotonic() + timeout
        while time.monotonic() < dl:
            h = sock.recv(2)
            if not h: break
            length = h[1] & 0x7F
            if length == 126: length = struct.unpack("!H", sock.recv(2))[0]
            elif length == 127: length = struct.unpack("!Q", sock.recv(8))[0]
            mask = sock.recv(4) if (h[1] & 0x80) else b""
            payload = sock.recv(length)
            if mask: payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
            if (h[0] & 0x0F) in (1, 0):
                msg = payload.decode(errors="replace")
                try:
                    js = json.loads(msg)
                    if js.get("type") == "get_profile": return js
                except: pass
        raise TimeoutError("Таймаут WS")
    finally:
        try: sock.close()
        except: pass

def fetch_profile(profile: Profile) -> FetchResult:
    try:
        data = anivox_websocket_profile(profile.profile_id)
        user = data.get("data", {}).get("user", {})
        if not user: return FetchResult(False, profile.profile_id, profile.url, error="Нет данных")
        
        online = user.get("online", 0)
        st_text = data.get("data", {}).get("online_status", "")
        if isinstance(st_text, dict): st_text = st_text.get("text", "")
        
        presence, title = "Оффлайн", ""
        if online == 0 and st_text:
            title = re.sub(r"^(?:смотрит|watching)\s*[:：-]?\s*", "", st_text, flags=re.IGNORECASE).strip()
            presence = f"Смотрит: {title}" if title else "Онлайн"
        elif online > 0:
            presence = f"Онлайн (был {datetime.fromtimestamp(online, tz=almaty_tz()).strftime('%H:%M')})"
            
        return FetchResult(True, profile.profile_id, profile.url, user.get("username", "неизвестно"), str(user.get("level", "0")), presence, title)
    except Exception as e: return FetchResult(False, profile.profile_id, profile.url, error=str(e))

def changed_fields(old: str, result: FetchResult) -> list[str]:
    if not old: return ["новый профиль"]
    prev, curr = (old.split("|") + [""]*4)[:4], (result.signature.split("|") + [""]*4)[:4]
    ch = []
    if prev[0] != curr[0]: ch.append("ник")
    if prev[1] != curr[1]: ch.append("уровень")
    if prev[2] != curr[2]: ch.append("статус")
    if prev[2].startswith("watching:") and curr[2].startswith("watching:") and prev[3] != curr[3]: ch.append("тайтл")
    return ch

# --- УМНЫЙ ПОИСК ОЗВУЧЕК (ANIVOX + SHIKIMORI/KODIK) ---
def find_anime_voices(anime_id: str) -> tuple[str, dict]:
    title = f"Аниме ID {anime_id}"
    voices = {}
    
    # 1. Запрос на Shikimori для жанров и русского названия
    try:
        req_title = urllib.request.Request(f"https://shikimori.one/api/animes/{anime_id}")
        with urllib.request.urlopen(req_title, timeout=5) as r:
            s_data = json.loads(r.read().decode())
            title = s_data.get("russian", title)
    except: pass

    # 2. Основной запрос на Anivox
    try:
        req = urllib.request.Request(f"https://anivox.fun/api/anime/{anime_id}", headers={"User-Agent": DEFAULT_USER_AGENT})
        with urllib.request.urlopen(req, timeout=5) as r:
            data = json.loads(r.read().decode())
            translations = data.get("data", {}).get("translations", [])
            for t in translations:
                v_name = t.get("name")
                if v_name: voices[v_name] = max(voices.get(v_name, 0), int(t.get("episodes_count", 0)))
    except: pass

    # 3. Доп. запрос на Kodik
    if not voices:
        try:
            req_v = urllib.request.Request(f"https://kodikapi.com/search?token=41f4f585f39e31d4e0e4b85d3a5ca78d&shikimori_id={anime_id}&with_episodes=true")
            with urllib.request.urlopen(req_v, timeout=5) as r:
                for item in json.loads(r.read().decode()).get("results", []):
                    v_name = item.get("translation", {}).get("title")
                    if v_name: voices[v_name] = max(voices.get(v_name, 0), int(item.get("last_episode") or item.get("episodes_count") or 1))
        except: pass

    return title, voices

# --- ПОИСК ЖАНРОВ АНИМЕ ЧЕРЕЗ SHIKIMORI ---
def fetch_genres(title: str, cache: dict) -> list[str]:
    if title in cache and "genres" in cache[title]: return cache[title]["genres"]
    genres = []
    try:
        search_url = f"https://shikimori.one/api/animes?search={urllib.parse.quote(title)}&limit=1"
        req = urllib.request.Request(search_url, headers={"User-Agent": DEFAULT_USER_AGENT})
        with urllib.request.urlopen(req, timeout=5) as r:
            s_data = json.loads(r.read().decode())
            if s_data:
                a_id = s_data[0]["id"]
                det_url = f"https://shikimori.one/api/animes/{a_id}"
                req2 = urllib.request.Request(det_url, headers={"User-Agent": DEFAULT_USER_AGENT})
                with urllib.request.urlopen(req2, timeout=5) as r2:
                    det = json.loads(r2.read().decode())
                    genres = [g.get("russian") or g.get("name") for g in det.get("genres", [])]
    except: pass
    if title not in cache: cache[title] = {}
    cache[title]["genres"] = genres
    return genres

# --- РАСЧЕТ СТАТИСТИКИ (ДЛЯ ОРИГИНАЛЬНЫХ ДИАГРАММ) ---
def calc_stats(entries: list, cache: dict) -> tuple[float, float, float, dict, dict]:
    w_time, on_time, off_time = 0.0, 0.0, 0.0
    titles = {}
    genres_time = {}
    now = datetime.now(almaty_tz()).timestamp()

    for i in range(len(entries)):
        curr = entries[i]
        try:
            t1 = datetime.fromisoformat(curr["checked_at"]).timestamp()
            t2 = datetime.fromisoformat(entries[i+1]["checked_at"]).timestamp() if i + 1 < len(entries) else now
            dur_hours = max(0, (t2 - t1) / 3600.0)
            pres = curr.get("presence", "")

            if "Смотрит" in pres:
                w_time += dur_hours
                t = curr.get("title", "Неизвестно")
                titles[t] = titles.get(t, 0) + dur_hours
                
                g_list = fetch_genres(t, cache)
                for g in g_list:
                    genres_time[g] = genres_time.get(g, 0) + dur_hours
                    
            elif "Онлайн" in pres: on_time += dur_hours
            else: off_time += dur_hours
        except: continue
            
    return round(w_time, 1), round(on_time, 1), round(off_time, 1), titles, genres_time

# --- ВЕБ-СЕРВЕР ---
class QuietHTTPRequestHandler(http.server.SimpleHTTPRequestHandler):
    def do_GET(self):
        if self.path in ("/", "/index.html"): self.path = "/anivox_report.html"
        return super().do_GET()
    def log_message(self, format, *args): pass

def run_web_server():
    port = int(os.getenv("PORT", 15887))
    os.chdir(os.path.dirname(os.path.abspath(CONFIG_PATH)))
    try:
        socketserver.TCPServer.allow_reuse_address = True
        with socketserver.TCPServer(("0.0.0.0", port), QuietHTTPRequestHandler) as httpd:
            httpd.serve_forever()
    except: pass

class Monitor:
    def __init__(self, store: Store, telegram: TelegramClient):
        self.store = store
        self.telegram = telegram
        self.stop_event = threading.Event()
        self.paused = False
        self.check_lock = threading.RLock()
        self.user_states = {}

    def notify_spy(self, user: dict, action: str):
        if not self.store.chat_id: return
        uid = str(user.get("id", ""))
        if uid == str(self.store.chat_id): return
        msg = f"👁 <b>Шпион:</b> {html.escape(user.get('first_name', ''))} (<code>{uid}</code>) -> {html.escape(action)}"
        try: self.telegram.send_message(self.store.chat_id, msg)
        except: pass

    def check_all(self, announce: bool = True):
        for profile in list(self.store.profiles.values()):
            if self.stop_event.is_set(): break
            res = fetch_profile(profile)
            with self.check_lock:
                old = profile.last_signature
                ch = changed_fields(old, res)
                profile.last_check, profile.last_ok, profile.last_error = res.checked_at, res.ok, res.error
                self.store.add_history(profile.profile_id, res, ch)
                if res.ok:
                    if announce and old and ch and self.store.notify_on_change:
                        msg = f"🔔 <b>Изменение:</b> {', '.join(ch)}\n👤 <b>{res.nickname}</b> — {res.presence}"
                        for uid in self.store.allowed_users: self.telegram.send_message(uid, msg)
                    profile.last_signature, profile.last_status = res.signature, res.presence
            self.store.save()
        self.generate_html_report()

    def check_anime_updates(self):
        updated = False
        a_ids = set([k.split('_')[0] for k in self.store.anime_subs.keys()])
        for a_id in a_ids:
            _, voices = find_anime_voices(a_id)
            for sub_key, data in list(self.store.anime_subs.items()):
                if sub_key.startswith(f"{a_id}_"):
                    t_voice, last_ep = data.get("voice", ""), int(data.get("last_ep", 0))
                    curr_ep = voices.get(t_voice, 0)
                    if curr_ep > last_ep:
                        data["last_ep"] = curr_ep
                        updated = True
                        for uid in data.get("users", self.store.allowed_users):
                            self.telegram.send_message(uid, f"🎉 <b>Новая серия!</b>\n🎬 <b>{data['title']}</b>\n🎞 Серия: <b>{curr_ep}</b>\n🎙 Озвучка: <i>{t_voice}</i>")
        if updated:
            self.store.save()
            self.generate_html_report()

    # --- ВОССТАНОВЛЕННЫЙ ОРИГИНАЛЬНЫЙ САЙТ С КРУГОВЫМ И ПОЛЯРНЫМ ГРАФИКАМИ ---
    def generate_html_report(self):
        now = datetime.now(almaty_tz()).strftime("%d.%m %H:%M")
        js_data = {"profiles": [], "anime": []}
        
        for p in self.store.profiles.values():
            entries = self.store.history.get(p.profile_id, [])
            w_time, on_time, off_time, titles, genres = calc_stats(entries, self.store.anime_cache)
            
            top_titles = []
            for k, v in sorted(titles.items(), key=lambda x: x[1], reverse=True)[:15]:
                g_str = ", ".join(self.store.anime_cache.get(k, {}).get("genres", [])) or "-"
                top_titles.append({"title": k, "genres": g_str, "hours": round(v, 1)})
                
            top_genres = [{"genre": k, "hours": round(v, 1)} for k, v in sorted(genres.items(), key=lambda x: x[1], reverse=True)[:6]]
            
            hist_list = []
            for i, e in enumerate(entries[-15:][::-1]):
                time_str = datetime.fromisoformat(e.get("checked_at", "")).strftime("%d.%m<br>%H:%M") if e.get("checked_at") else ""
                hist_list.append({"num": i+1, "time": time_str, "status": e.get("presence", "")})
            
            js_data["profiles"].append({
                "id": p.profile_id, "name": p.label or p.profile_id,
                "stats": [w_time, on_time, off_time],
                "top": top_titles, "genres": top_genres, "history": hist_list
            })
            
        for a_id, d in self.store.anime_subs.items():
            js_data["anime"].append({"title": d["title"], "voice": d["voice"], "ep": d["last_ep"]})

        html_content = f"""<!DOCTYPE html>
<html lang="ru">
<head>
    <meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0"><meta http-equiv="refresh" content="30">
    <title>AniVox Dashboard</title>
    <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
    <style>
        body {{ background: #18181b; color: #e4e4e7; font-family: sans-serif; margin: 0; padding: 10px; }}
        header {{ display: flex; align-items: center; justify-content: center; gap: 10px; border-bottom: 2px solid #27272a; padding-bottom: 15px; margin-bottom: 15px; }}
        h1 {{ margin: 0; font-size: 20px; color: #fff; }}
        
        .tabs {{ display: flex; gap: 10px; justify-content: center; margin-bottom: 20px; flex-wrap: wrap; border-bottom: 1px solid #c084fc; padding-bottom: 5px; }}
        .tab-btn {{ background: transparent; color: #a1a1aa; border: none; font-size: 14px; font-weight: bold; cursor: pointer; padding: 10px; }}
        .tab-btn.active {{ color: #c084fc; border-bottom: 2px solid #c084fc; }}
        .tab-content {{ display: none; }}
        .tab-content.active {{ display: block; }}
        
        .prof-container {{ background: #27272a; border-radius: 12px; padding: 15px; margin-bottom: 20px; }}
        .prof-header {{ display: flex; align-items: center; gap: 10px; font-size: 18px; font-weight: bold; margin-bottom: 15px; }}
        
        .stats-grid {{ display: grid; grid-template-columns: 1fr; gap: 10px; text-align: center; margin-bottom: 20px; }}
        .stat-box {{ background: #3f3f46; padding: 15px; border-radius: 8px; font-size: 14px; font-weight: bold; }}
        .stat-val {{ font-size: 20px; font-weight: bold; color: #c084fc; margin-top: 5px; }}
        
        .chart-box {{ position: relative; height: 280px; width: 100%; margin: 20px 0; }}
        
        table {{ width: 100%; border-collapse: collapse; font-size: 13px; margin-top: 20px; }}
        th {{ background: #b48ead; color: #fff; padding: 12px; text-align: left; font-size: 14px; }}
        td {{ padding: 12px; border-bottom: 1px solid #3f3f46; vertical-align: middle; }}
        .col-id {{ width: 30px; font-weight: bold; }}
        .col-time {{ width: 80px; color: #a1a1aa; }}
        
        .anime-card {{ background: #27272a; padding: 15px; border-radius: 8px; margin-bottom: 10px; border-left: 4px solid #c084fc; }}
    </style>
</head>
<body>
    <header>
        <img src="https://anivox.fun/favicon.ico" width="24" height="24">
        <h1>AniVox Dashboard</h1>
    </header>
    
    <div class="tabs" id="user-tabs"></div>
    <div id="content-area"></div>

    <script>
        const data = {json.dumps(js_data, ensure_ascii=False)};
        
        function render() {{
            const tabs = document.getElementById('user-tabs');
            const area = document.getElementById('content-area');
            
            // Вкладки профилей
            data.profiles.forEach((p, i) => {{
                let btn = document.createElement('button');
                btn.className = 'tab-btn' + (i===0 ? ' active' : '');
                btn.innerText = (i+1) + '. ' + p.name;
                btn.onclick = () => switchTab('prof-'+i, btn);
                tabs.appendChild(btn);
                
                let html = `<div id="prof-${{i}}" class="tab-content ${{i===0 ? 'active' : ''}}">
                    <div class="prof-container">
                        <div class="prof-header">👤 ${{i+1}}. ${{p.name}}</div>
                        
                        <div class="stats-grid">
                            <div class="stat-box">В аниме<div class="stat-val">${{p.stats[0]}} ч.</div></div>
                            <div class="stat-box">Онлайн<div class="stat-val">${{p.stats[1]}} ч.</div></div>
                            <div class="stat-box">Оффлайн<div class="stat-val">${{p.stats[2]}} ч.</div></div>
                        </div>
                        
                        <div class="chart-box"><canvas id="chart-doughnut-${{i}}"></canvas></div>
                        <div class="chart-box" style="margin-top: 40px;"><canvas id="chart-polar-${{i}}"></canvas></div>
                        
                        <h3 style="color:#d8b4e2; margin-top:30px;">🏆 Топ тайтлов:</h3>
                        <table><tr><th>Тайтл</th><th>Жанры</th><th>Часы</th></tr>`;
                
                p.top.forEach(t => {{ html += `<tr><td style="color:#b48ead;">${{t.title}}</td><td style="color:#e4e4e7; font-size:11px;">${{t.genres}}</td><td style="font-weight:bold;">${{t.hours}}</td></tr>`; }});
                
                html += `</table><br><h3 style="color:#d8b4e2; margin-top:30px;">📜 История статусов:</h3>
                <table><tr><th>#</th><th>Дата<br>(Алматы)</th><th>Статус</th></tr>`;
                
                p.history.forEach(h => {{ html += `<tr><td class="col-id">${{h.num}}</td><td class="col-time">${{h.time}}</td><td style="color:#e4e4e7;">${{h.status}}</td></tr>`; }});
                html += `</table></div></div>`;
                area.innerHTML += html;
            }});

            // Вкладка Аниме
            let a_btn = document.createElement('button');
            a_btn.className = 'tab-btn'; a_btn.innerText = '🎬 Аниме Трекер';
            a_btn.onclick = () => switchTab('anime-tab', a_btn);
            tabs.appendChild(a_btn);
            
            let a_html = `<div id="anime-tab" class="tab-content">`;
            if (data.anime.length === 0) a_html += `<div style="text-align:center; padding:20px;">Тайтлов нет. Добавьте через бота.</div>`;
            data.anime.forEach(a => {{
                a_html += `<div class="anime-card"><b>${{a.title}}</b><br>Серия: <b>${{a.ep}}</b> (Озвучка: <i>${{a.voice}}</i>)</div>`;
            }});
            a_html += `</div>`;
            area.innerHTML += a_html;

            // Рендер Графиков
            data.profiles.forEach((p, i) => {{
                // Doughnut (Круговая)
                new Chart(document.getElementById('chart-doughnut-'+i), {{
                    type: 'doughnut',
                    data: {{
                        labels: ['Смотрит', 'Онлайн', 'Оффлайн'],
                        datasets: [{{ data: p.stats, backgroundColor: ['#b48ead', '#2dd4bf', '#fb7185'], borderWidth: 2, borderColor: '#18181b' }}]
                    }},
                    options: {{ 
                        responsive: true, maintainAspectRatio: false, 
                        plugins: {{ legend: {{ position: 'bottom', labels: {{color: '#a1a1aa', font: {{size: 14}} }} }} }},
                        cutout: '55%'
                    }}
                }});
                
                // Polar Area (Жанры)
                if (p.genres.length > 0) {{
                    new Chart(document.getElementById('chart-polar-'+i), {{
                        type: 'polarArea',
                        data: {{
                            labels: p.genres.map(g => g.genre),
                            datasets: [{{ data: p.genres.map(g => g.hours), backgroundColor: '#7e57c2', borderWidth: 2, borderColor: '#18181b' }}]
                        }},
                        options: {{ 
                            responsive: true, maintainAspectRatio: false,
                            scales: {{ r: {{ ticks: {{display: false}}, grid: {{color: '#3f3f46'}} }} }},
                            plugins: {{ legend: {{ position: 'bottom', labels: {{color: '#a1a1aa'}} }} }}
                        }}
                    }});
                }}
            }});
        }}
        
        function switchTab(id, btnObj) {{
            document.querySelectorAll('.tab-content').forEach(el => el.classList.remove('active'));
            document.querySelectorAll('.tab-btn').forEach(el => el.classList.remove('active'));
            document.getElementById(id).classList.add('active');
            btnObj.classList.add('active');
        }}
        
        render();
    </script>
</body>
</html>"""
        REPORT_PATH.write_text(html_content, encoding="utf-8")

    def handle_callback(self, cb: dict):
        data = cb.get("data", "")
        uid = str(cb.get("from", {}).get("id", ""))
        chat_id = str(cb.get("message", {}).get("chat", {}).get("id", ""))
        msg_id = cb.get("message", {}).get("message_id")

        if data.startswith("sub|"):
            _, a_id, voice = data.split("|", 2)
            title = self.store.anime_cache.get(a_id, {}).get("title", f"Аниме ID {a_id}")
            last_ep = self.store.anime_cache.get(a_id, {}).get("voices", {}).get(voice, 0)
            
            self.store.anime_subs[f"{a_id}_{voice}"] = {"title": title, "voice": voice, "last_ep": last_ep, "users": [uid]}
            self.store.save()
            self.telegram.answer_callback(cb.get("id"), f"Подписка на {voice} оформлена!")
            self.telegram.edit_message_text(chat_id, msg_id, f"✅ Подписка оформлена!\n🎬 <b>{title}</b>\n🎙 Озвучка: <b>{voice}</b>")
            self.generate_html_report()

    def handle_message(self, msg: dict):
        user = msg.get("from", {})
        uid = str(user.get("id", ""))
        chat_id = str(msg.get("chat", {}).get("id", ""))
        text = compact(msg.get("text", ""))

        if not text: return
        
        if text.lower() in ["/cancel", "отмена", "❌ отмена"]:
            self.user_states.pop(uid, None)
            self.telegram.send_message(chat_id, "❌ Действие отменено.", reply_markup=TELEGRAM_KEYBOARD)
            return

        if not self.store.chat_id:
            self.store.chat_id = uid
            if uid not in self.store.allowed_users: self.store.allowed_users.append(uid)
            self.store.save()

        if uid not in self.store.allowed_users:
            return self.telegram.send_message(chat_id, "⛔ У вас нет доступа к боту.")

        self.notify_spy(user, text)
        state = self.user_states.get(uid)

        if state == "await_profile":
            try:
                url, p_id = normalize_profile_url(text)
                self.store.profiles[p_id] = Profile(url=url, profile_id=p_id, label=p_id)
                self.store.save()
                self.user_states.pop(uid, None)
                self.telegram.send_message(chat_id, f"✅ Профиль {p_id} добавлен!", reply_markup=TELEGRAM_KEYBOARD)
            except Exception as e: self.telegram.send_message(chat_id, f"❌ Ошибка: отправьте ссылку или '❌ Отмена'.")
            return
            
        if state == "await_profile_del":
            if text in self.store.profiles:
                del self.store.profiles[text]
                self.store.save()
                self.user_states.pop(uid, None)
                self.telegram.send_message(chat_id, f"🗑 Профиль {text} удален.", reply_markup=TELEGRAM_KEYBOARD)
            else:
                self.telegram.send_message(chat_id, "❌ ID не найден. Отправьте ID или '❌ Отмена'.")
            return
            
        if state == "await_anime":
            match = re.search(r'(\d+)', text)
            if not match: return self.telegram.send_message(chat_id, "❌ Не найден ID. Отправьте ссылку на Anivox.")
            a_id = match.group(1)
            
            self.telegram.send_message(chat_id, "⏳ Ищу доступные озвучки на Anivox...")
            title, voices = find_anime_voices(a_id)
            self.user_states.pop(uid, None)

            if not voices: return self.telegram.send_message(chat_id, "😔 Озвучки не найдены. Вероятно аниме еще не вышло.", reply_markup=TELEGRAM_KEYBOARD)

            self.store.anime_cache[a_id] = {"title": title, "voices": voices}
            kb = []
            v_list = list(voices.keys())[:14]
            for i in range(0, len(v_list), 2):
                kb.append([{"text": v, "callback_data": f"sub|{a_id}|{v[:20]}"} for v in v_list[i:i+2]])

            self.telegram.send_message(chat_id, f"🎬 <b>{title}</b>\nВыберите озвучку:", reply_markup={"inline_keyboard": kb})
            return

        if state == "chat":
            if text == "🚪 Выйти из чата":
                self.user_states.pop(uid, None)
                return self.telegram.send_message(chat_id, "🚪 Вы вышли из чата.", reply_markup=TELEGRAM_KEYBOARD)
            for f_id in self.store.allowed_users:
                if f_id != uid:
                    try: self.telegram.send_message(f_id, f"💬 <b>{user.get('first_name', 'Друг')}:</b>\n{text}")
                    except: pass
            return

        # ОСНОВНОЕ МЕНЮ
        if text in ["/start", "меню"]: self.telegram.send_message(chat_id, "👋 Привет!", reply_markup=TELEGRAM_KEYBOARD)
        elif text == "📊 Проверить всех": 
            self.telegram.send_message(chat_id, "⏳ Проверяю...")
            threading.Thread(target=self.check_all, daemon=True).start()
        elif text == "🌐 Открыть сайт-отчет":
            self.generate_html_report()
            url = os.getenv("RENDER_EXTERNAL_URL", f"http://0.0.0.0:{os.getenv('PORT', '15887')}")
            self.telegram.send_message(chat_id, f"🌐 Ваш отчет с графиками:\n{url}/anivox_report.html")
        elif text == "➕ Добавить профиль":
            self.user_states[uid] = "await_profile"
            self.telegram.send_message(chat_id, "Отправьте <b>ссылку на профиль Anivox</b>:", reply_markup=CANCEL_KEYBOARD)
        elif text == "🗑 Удалить профиль":
            self.user_states[uid] = "await_profile_del"
            msg = "Отправьте <b>ID профиля</b> для удаления.\nВаши профили:\n" + "\n".join([f"• <code>{k}</code>" for k in self.store.profiles.keys()])
            self.telegram.send_message(chat_id, msg, reply_markup=CANCEL_KEYBOARD)
        elif text == "👥 Мои профили":
            msg = "👥 <b>Отслеживаемые профили:</b>\n"
            for p in self.store.profiles.values(): msg += f"• <b>{p.label or p.profile_id}</b> — {p.last_status or 'не проверялся'}\n"
            self.telegram.send_message(chat_id, msg or "Список пуст.")
        elif text == "🎬 Аниме трекер":
            self.user_states[uid] = "await_anime"
            self.telegram.send_message(chat_id, "Отправьте ссылку на аниме (anivox.fun/anime/ID):", reply_markup=CANCEL_KEYBOARD)
        elif text == "💬 Чат друзей":
            self.user_states[uid] = "chat"
            self.telegram.send_message(chat_id, "Вы вошли в чат.", reply_markup=CHAT_KEYBOARD)
        elif text == "🎭 Друзья":
            msg = "🎭 <b>Ваши друзья:</b>\n" + "\n".join([f"• <code>{f}</code>" for f in self.store.allowed_users])
            msg += "\n\n(Управление временно работает только через конфиг файл)"
            self.telegram.send_message(chat_id, msg)

    def run(self):
        last_check, last_anime = 0.0, 0.0
        self.generate_html_report()
        while not self.stop_event.is_set():
            now = time.time()
            if not self.paused and (now - last_check >= self.store.interval_minutes * 60):
                last_check = now
                threading.Thread(target=self.check_all, daemon=True).start()
            if now - last_anime >= 600:
                last_anime = now
                threading.Thread(target=self.check_anime_updates, daemon=True).start()

            try:
                for upd in self.telegram.get_updates():
                    if "message" in upd: self.handle_message(upd["message"])
                    elif "callback_query" in upd: self.handle_callback(upd["callback_query"])
            except: time.sleep(1)
            time.sleep(0.5)

def main():
    store = Store(CONFIG_PATH)
    telegram = TelegramClient(store.token)
    monitor = Monitor(store, telegram)
    threading.Thread(target=run_web_server, daemon=True).start()
    monitor.run()

if __name__ == "__main__":
    main()
