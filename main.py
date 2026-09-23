#!/usr/bin/env python3
"""AniVox Monitor v4.8.0 (Original Web Design, Smart Kodik Voices, Fixes)."""

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
VERSION = "4.8.0"
CONFIG_PATH = Path(__file__).with_name("anivox_monitor.json")
LOG_PATH = Path(__file__).with_name("anivox_monitor.log")
REPORT_PATH = Path(__file__).with_name("anivox_report.html")

PROFILE_RE = re.compile(
    r"^https?://(?:www\.)?anivox\.fun/profile/([0-9]+)(?:[/?#].*)?$",
    re.IGNORECASE,
)
DEFAULT_INTERVAL = 10
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Linux; Android 10; Mobile) AppleWebKit/537.36 "
    "Chrome/124.0 Mobile Safari/537.36 AniVoxMonitor/4.8"
)

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

CHAT_KEYBOARD = {
    "keyboard": [["🚪 Выйти из чата"]],
    "resize_keyboard": True,
}

CANCEL_KEYBOARD = {
    "keyboard": [["❌ Отмена"]],
    "resize_keyboard": True,
}

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
        
        self.allowed_users: list[str] = [str(u) for u in raw.get("allowed_users", [])]
        if self.chat_id and self.chat_id not in self.allowed_users:
            self.allowed_users.append(self.chat_id)
            
        self.anime_cache: dict[str, Any] = raw.get("anime_cache", {})
        self.anime_subs: dict[str, dict[str, Any]] = raw.get("anime_subs", {})
        
        raw_settings = raw.get("settings", {})
        self.interval_minutes = int(raw_settings.get("interval_minutes", DEFAULT_INTERVAL))
        self.notify_on_change = raw_settings.get("notify_on_change", True)
        
        self.profiles: dict[str, Profile] = {}
        for item in raw.get("profiles", []):
            try:
                p = Profile(**item)
                self.profiles[p.profile_id] = p
            except: pass
                
        self.history = raw.get("history", {})

    def save(self) -> None:
        save_json(self.path, {
            "token": self.token, "chat_id": self.chat_id,
            "allowed_users": self.allowed_users, "anime_cache": self.anime_cache,
            "anime_subs": self.anime_subs, 
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
        except Exception as e:
            return None

    def send_message(self, chat_id: str, text: str, reply_markup: dict = None) -> None:
        self.request("sendMessage", {
            "chat_id": chat_id, "text": text[:4096], "parse_mode": "HTML",
            "disable_web_page_preview": "true",
            "reply_markup": json.dumps(reply_markup or TELEGRAM_KEYBOARD)
        })

    def edit_message_text(self, chat_id: str, message_id: int, text: str, reply_markup: dict = None) -> None:
        req_data = {"chat_id": chat_id, "message_id": message_id, "text": text, "parse_mode": "HTML"}
        if reply_markup:
            req_data["reply_markup"] = json.dumps(reply_markup)
        self.request("editMessageText", req_data)

    def answer_callback(self, cb_id: str, text: str) -> None:
        self.request("answerCallbackQuery", {"callback_query_id": cb_id, "text": text})

    def get_updates(self) -> list:
        res = self.request("getUpdates", {"offset": self.offset, "timeout": 2})
        updates = res if isinstance(res, list) else []
        if updates:
            self.offset = updates[-1]["update_id"] + 1
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

# --- ВОССТАНОВЛЕННЫЙ WEBSOCKET АРХИТЕКТУРЫ (ДЛЯ РАБОТЫ СКРАПЕРА) ---
def recv_exact(sock: socket.socket, length: int) -> bytes:
    chunks = []
    remaining = length
    while remaining:
        chunk = sock.recv(remaining)
        if not chunk: raise ConnectionError("WebSocket закрыт")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)

def ws_send_text(sock: socket.socket, payload: dict) -> None:
    data = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    mask = os.urandom(4)
    masked = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
    length = len(data)
    if length < 126: header = bytes((0x81, 0x80 | length))
    elif length < 65536: header = bytes((0x81, 0x80 | 126)) + struct.pack("!H", length)
    else: header = bytes((0x81, 0x80 | 127)) + struct.pack("!Q", length)
    sock.sendall(header + mask + masked)

def ws_read_message(sock: socket.socket) -> Optional[str]:
    fragments = []
    while True:
        header = recv_exact(sock, 2)
        opcode = header[0] & 0x0F
        masked = bool(header[1] & 0x80)
        length = header[1] & 0x7F
        if length == 126: length = struct.unpack("!H", recv_exact(sock, 2))[0]
        elif length == 127: length = struct.unpack("!Q", recv_exact(sock, 8))[0]
        mask = recv_exact(sock, 4) if masked else b""
        data = recv_exact(sock, length)
        if masked: data = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
        if opcode == 8: return None
        if opcode == 9: 
            sock.sendall(bytes((0x8A, len(data))) + data)
            continue
        if opcode == 10: continue
        if opcode in (1, 0):
            fragments.append(data)
            if header[0] & 0x80: return b"".join(fragments).decode("utf-8", errors="replace")

def anivox_websocket_profile(profile_id: str, timeout: int = 15) -> dict:
    host = "anivox.fun"
    raw_socket = socket.create_connection((host, 443), timeout=timeout)
    context = ssl.create_default_context()
    sock = context.wrap_socket(raw_socket, server_hostname=host)
    sock.settimeout(timeout)
    key = base64.b64encode(os.urandom(16)).decode("ascii")
    handshake = (
        f"GET /api HTTP/1.1\r\nHost: {host}\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
        f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\nOrigin: https://{host}\r\n"
        f"User-Agent: {DEFAULT_USER_AGENT}\r\n\r\n"
    ).encode("ascii")
    try:
        sock.sendall(handshake)
        response = b""
        while b"\r\n\r\n" not in response:
            response += sock.recv(4096)
        if " 101 " not in response.decode("latin1").splitlines()[0]:
            raise ConnectionError("Отказ WebSocket Anivox")
        
        ws_send_text(sock, {"type": "get_profile", "id": int(profile_id)})
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            msg = ws_read_message(sock)
            if not msg: continue
            try:
                data = json.loads(msg)
                if data.get("type") == "get_profile": return data
            except json.JSONDecodeError: continue
        raise TimeoutError("Таймаут WebSocket")
    finally:
        try: sock.close()
        except: pass

def fetch_profile(profile: Profile, timeout: int = 15) -> FetchResult:
    try:
        data = anivox_websocket_profile(profile.profile_id, timeout)
        container = data.get("data", {})
        user = container.get("user", {})
        if not user: return FetchResult(False, profile.profile_id, profile.url, error="Нет данных user")
        
        online = user.get("online", 0)
        status_text = container.get("online_status", "")
        if isinstance(status_text, dict): status_text = status_text.get("text", "")
        
        presence = "Оффлайн"
        title = ""
        if online == 0 and status_text:
            title = re.sub(r"^(?:смотрит|watching)\s*[:：-]?\s*", "", status_text, flags=re.IGNORECASE).strip()
            presence = f"Смотрит: {title}" if title else "Онлайн"
        elif online > 0:
            presence = f"Онлайн (был {datetime.fromtimestamp(online, tz=almaty_tz()).strftime('%H:%M')})"
            
        return FetchResult(True, profile.profile_id, profile.url, user.get("username", "неизвестно"), str(user.get("level", "0")), presence, title)
    except Exception as e:
        return FetchResult(False, profile.profile_id, profile.url, error=str(e))

def changed_fields(old: str, result: FetchResult) -> list[str]:
    if not old: return ["новый профиль"]
    prev, curr = (old.split("|") + [""]*4)[:4], (result.signature.split("|") + [""]*4)[:4]
    ch = []
    if prev[0] != curr[0]: ch.append("ник")
    if prev[1] != curr[1]: ch.append("уровень")
    if prev[2] != curr[2]: ch.append("статус")
    if prev[2].startswith("watching:") and curr[2].startswith("watching:") and prev[3] != curr[3]: ch.append("тайтл")
    return ch

# --- WEB СЕРВЕР ---
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

    def notify_owner_spy(self, user: dict, action: str):
        if not self.store.chat_id: return
        uid = str(user.get("id", ""))
        if uid == str(self.store.chat_id): return
        msg = f"👁 <b>Шпион:</b> {html.escape(user.get('first_name', ''))} (ID: <code>{uid}</code>) -> {action}"
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
                        if res.title: msg += f"\n🍿 Смотрит: <b>{res.title}</b>"
                        for uid in self.store.allowed_users:
                            self.telegram.send_message(uid, msg)
                    profile.last_signature, profile.last_status = res.signature, res.presence
            self.store.save()
        self.generate_html_report()

    def check_anime_updates(self):
        updated = False
        # Группируем подписки по ID аниме (shikimori_id), чтобы не спамить Kodik
        anime_ids = set([s.split('_')[0] for s in self.store.anime_subs.keys()])
        
        for a_id in anime_ids:
            try:
                req = urllib.request.Request(f"https://kodikapi.com/search?token=41f4f585f39e31d4e0e4b85d3a5ca78d&shikimori_id={a_id}&with_episodes=true")
                with urllib.request.urlopen(req, timeout=8) as resp:
                    results = json.loads(resp.read().decode()).get("results", [])
                    
                    for item in results:
                        v_name = item.get("translation", {}).get("title", "Оригинал")
                        ep_count = int(item.get("last_episode") or item.get("episodes_count") or 1)
                        
                        # Проверяем все подписки для этого a_id
                        for sub_key, data in list(self.store.anime_subs.items()):
                            if sub_key.startswith(f"{a_id}_"):
                                target_voice = data.get("voice", "")
                                last_ep = int(data.get("last_ep", 0))
                                
                                if target_voice == v_name and ep_count > last_ep:
                                    data["last_ep"] = ep_count
                                    updated = True
                                    msg = f"🎉 <b>Новая серия!</b>\n🎬 <b>{data['title']}</b>\n🎞 Серия: <b>{ep_count}</b>\n🎙 Озвучка: <i>{v_name}</i>"
                                    for uid in data.get("users", self.store.allowed_users):
                                        self.telegram.send_message(uid, msg)
            except Exception as e:
                logger.debug(f"Ошибка Kodik для {a_id}: {e}")
                
        if updated:
            self.store.save()
            self.generate_html_report()

    # --- ВОССТАНОВЛЕННЫЙ КРАСИВЫЙ САЙТ С ВКЛАДКАМИ ---
    def generate_html_report(self):
        now = datetime.now(almaty_tz()).strftime("%d.%m %H:%M:%S")
        prof_html, anime_html = "", ""
        
        for p in self.store.profiles.values():
            st = p.last_status or "Не проверялся"
            is_online = "онлайн" in st.lower() or "смотрит" in st.lower()
            badge = "badge-online" if is_online else "badge-offline"
            history = self.store.history.get(p.profile_id, [])
            last_title = history[-1].get("title", "") if history else ""
            
            hist_rows = "".join([f"<li><span class='time'>{h.get('checked_at','')[11:16]}</span> — {html.escape(h.get('presence',''))}</li>" for h in history[-5:][::-1]])
            prof_html += f"""
            <div class='card'>
                <div class='card-header'>
                    <span class='name'>{html.escape(p.label or p.profile_id)}</span>
                    <span class='badge {badge}'>{html.escape(st)}</span>
                </div>
                <div class='card-body'>
                    {f'<p class="watching">🍿 Смотрит: <b>{html.escape(last_title)}</b></p>' if last_title else ''}
                    <div class='history'><b>Последние события:</b><ul>{hist_rows or '<li>Пусто</li>'}</ul></div>
                </div>
            </div>"""

        for a_data in self.store.anime_subs.values():
            anime_html += f"""
            <div class='card anime-badge'>
                🎬 <b>{html.escape(a_data['title'])}</b> — Серия: <span class='ep-num'>{a_data['last_ep']}</span>
                <br>🎙 Озвучка: <i>{html.escape(a_data['voice'])}</i>
            </div>"""

        html_content = f"""<!DOCTYPE html>
<html lang="ru">
<head>
    <meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0"><meta http-equiv="refresh" content="30">
    <title>AniVox Monitor</title>
    <style>
        :root {{ --bg: #0f172a; --card: #1e293b; --text: #f8fafc; --accent: #38bdf8; --green: #22c55e; --red: #ef4444; }}
        body {{ font-family: -apple-system, sans-serif; background: var(--bg); color: var(--text); margin: 0; padding: 20px; }}
        .container {{ max-width: 900px; margin: 0 auto; }}
        header {{ text-align: center; margin-bottom: 25px; border-bottom: 1px solid #334155; padding-bottom: 15px; }}
        h1 {{ color: var(--accent); margin: 0 0 5px 0; font-size: 24px; }}
        .updated {{ font-size: 13px; color: #94a3b8; }}
        
        .tabs {{ display: flex; gap: 10px; margin-bottom: 20px; justify-content: center; }}
        .tab-btn {{ background: var(--card); color: #94a3b8; border: 1px solid #334155; padding: 10px 20px; border-radius: 8px; cursor: pointer; font-size: 16px; font-weight: bold; transition: 0.2s; }}
        .tab-btn.active {{ background: var(--accent); color: #fff; border-color: var(--accent); }}
        .tab-content {{ display: none; }}
        .tab-content.active {{ display: block; }}

        .grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(280px, 1fr)); gap: 15px; }}
        .card {{ background: var(--card); border-radius: 12px; padding: 15px; border: 1px solid #334155; box-shadow: 0 4px 6px rgba(0,0,0,0.2); }}
        .card-header {{ display: flex; justify-content: space-between; align-items: center; margin-bottom: 10px; }}
        .name {{ font-weight: bold; font-size: 17px; }}
        .badge {{ padding: 4px 10px; border-radius: 20px; font-size: 12px; font-weight: bold; }}
        .badge-online {{ background: rgba(34, 197, 94, 0.2); color: var(--green); border: 1px solid var(--green); }}
        .badge-offline {{ background: rgba(239, 68, 68, 0.2); color: var(--red); border: 1px solid var(--red); }}
        .watching {{ color: #fde047; margin: 8px 0; font-size: 14px; }}
        .history {{ margin-top: 10px; font-size: 13px; }}
        .history ul {{ margin: 5px 0; padding-left: 20px; color: #cbd5e1; }}
        .time {{ color: #94a3b8; font-weight: bold; }}
        .anime-badge {{ border-left: 4px solid var(--accent); }}
        .ep-num {{ color: var(--green); font-size: 16px; }}
    </style>
    <script>
        function switchTab(id) {{
            document.querySelectorAll('.tab-content, .tab-btn').forEach(el => el.classList.remove('active'));
            document.getElementById(id).classList.add('active');
            document.getElementById('btn-' + id).classList.add('active');
            localStorage.setItem('activeTab', id);
        }}
        window.onload = function() {{
            let saved = localStorage.getItem('activeTab') || 'tab-profiles';
            switchTab(saved);
        }}
    </script>
</head>
<body>
    <div class="container">
        <header>
            <h1>⚡ AniVox Monitor</h1>
            <div class="updated">Обновлено: {now} • Автообновление каждые 30 сек</div>
        </header>
        <div class="tabs">
            <button id="btn-tab-profiles" class="tab-btn" onclick="switchTab('tab-profiles')">👥 Профили</button>
            <button id="btn-tab-anime" class="tab-btn" onclick="switchTab('tab-anime')">🎬 Аниме Трекер</button>
        </div>
        <div id="tab-profiles" class="tab-content grid">{prof_html or '<p>Нет добавленных профилей.</p>'}</div>
        <div id="tab-anime" class="tab-content grid">{anime_html or '<p>Аниме пока не добавлены.</p>'}</div>
    </div>
</body>
</html>"""
        REPORT_PATH.write_text(html_content, encoding="utf-8")

    def handle_callback(self, cb: dict) -> None:
        data = cb.get("data", "")
        uid = str(cb.get("from", {}).get("id", ""))
        chat_id = str(cb.get("message", {}).get("chat", {}).get("id", ""))
        msg_id = cb.get("message", {}).get("message_id")

        if data.startswith("sub|"):
            # Выбор конкретной озвучки аниме
            _, a_id, voice = data.split("|", 2)
            title = self.store.anime_cache.get(a_id, {}).get("title", f"Аниме ID {a_id}")
            sub_id = f"{a_id}_{voice}"
            
            # Сохраняем подписку с актуальной серией, которую нашли ранее
            last_ep = self.store.anime_cache.get(a_id, {}).get("voices", {}).get(voice, 0)
            
            self.store.anime_subs[sub_id] = {"title": title, "voice": voice, "last_ep": last_ep, "users": [uid]}
            self.store.save()
            
            self.telegram.answer_callback(cb.get("id"), f"Подписка на {voice} оформлена!")
            self.telegram.edit_message_text(chat_id, msg_id, f"✅ Вы успешно подписались!\n🎬 <b>{title}</b>\n🎙 Озвучка: <b>{voice}</b>")

        elif data == "friend_add":
            self.user_states[uid] = "await_friend_add"
            self.telegram.send_message(chat_id, "Отправьте <b>ID пользователя Telegram</b> (цифры), которому хотите дать доступ:", reply_markup=CANCEL_KEYBOARD)
            self.telegram.answer_callback(cb.get("id"), "")

        elif data == "friend_del":
            self.user_states[uid] = "await_friend_del"
            self.telegram.send_message(chat_id, "Отправьте <b>ID друга</b>, которого нужно удалить:", reply_markup=CANCEL_KEYBOARD)
            self.telegram.answer_callback(cb.get("id"), "")

    def handle_message(self, message: dict) -> None:
        user = message.get("from", {})
        uid = str(user.get("id", ""))
        chat_id = str(message.get("chat", {}).get("id", ""))
        text = compact(message.get("text", ""))

        if not text: return

        # === 1. ГЛОБАЛЬНАЯ ОТМЕНА (СПАСАЕТ ОТ ЗАВИСАНИЙ) ===
        if text.lower() in ["/cancel", "отмена", "отменить", "❌ отмена"]:
            self.user_states.pop(uid, None)
            self.telegram.send_message(chat_id, "❌ Действие отменено.", reply_markup=TELEGRAM_KEYBOARD)
            return

        if not self.store.chat_id:
            self.store.chat_id = uid
            if uid not in self.store.allowed_users: self.store.allowed_users.append(uid)
            self.store.save()

        if uid not in self.store.allowed_users:
            self.telegram.send_message(chat_id, "⛔ Нет доступа к боту.")
            return

        # Уведомление шпиона (кроме самого себя)
        self.notify_owner_spy(user, text)

        state = self.user_states.get(uid)

        # === 2. ОБРАБОТКА ШАГОВ (ВВОД ДАННЫХ) ===
        if state == "await_profile":
            try:
                url, p_id = normalize_profile_url(text)
                self.store.profiles[p_id] = Profile(url=url, profile_id=p_id, label=p_id)
                self.store.save()
                self.user_states.pop(uid, None)
                self.telegram.send_message(chat_id, f"✅ Профиль {p_id} успешно добавлен!", reply_markup=TELEGRAM_KEYBOARD)
            except Exception as e:
                self.telegram.send_message(chat_id, f"❌ Ошибка: {e}\nОтправьте правильную ссылку или нажмите '❌ Отмена'.")
            return
            
        if state == "await_profile_del":
            if text in self.store.profiles:
                del self.store.profiles[text]
                self.store.save()
                self.user_states.pop(uid, None)
                self.telegram.send_message(chat_id, f"🗑 Профиль {text} удален.", reply_markup=TELEGRAM_KEYBOARD)
            else:
                self.telegram.send_message(chat_id, "❌ ID не найден. Отправьте еще раз или '❌ Отмена'.")
            return

        # ДОБАВЛЕНИЕ ДРУГА
        if state == "await_friend_add":
            if text.isdigit() and text not in self.store.allowed_users:
                self.store.allowed_users.append(text)
                self.store.save()
                self.user_states.pop(uid, None)
                self.telegram.send_message(chat_id, f"✅ Друг {text} получил доступ к боту!", reply_markup=TELEGRAM_KEYBOARD)
            else:
                self.telegram.send_message(chat_id, "❌ Это не ID, или он уже есть в списке. Нажмите '❌ Отмена'.")
            return

        if state == "await_friend_del":
            if text in self.store.allowed_users and text != self.store.chat_id:
                self.store.allowed_users.remove(text)
                self.store.save()
                self.user_states.pop(uid, None)
                self.telegram.send_message(chat_id, f"🗑 Друг {text} удален.", reply_markup=TELEGRAM_KEYBOARD)
            else:
                self.telegram.send_message(chat_id, "❌ Ошибка удаления. Нажмите '❌ Отмена'.")
            return

        # ДОБАВЛЕНИЕ АНИМЕ И ПОИСК ОЗВУЧЕК
        if state == "await_anime_link":
            match = re.search(r'(\d+)', text)
            if not match:
                self.telegram.send_message(chat_id, "❌ Не смог найти ID в тексте. Нажмите '❌ Отмена'.")
                return
            a_id = match.group(1)
            
            title = f"Аниме (ID {a_id})"
            try:
                with urllib.request.urlopen(urllib.request.Request(f"https://shikimori.one/api/animes/{a_id}"), timeout=5) as r: 
                    title = json.loads(r.read().decode()).get("russian", title)
            except: pass

            self.telegram.send_message(chat_id, "⏳ Ищу доступные озвучки в базе Kodik...", reply_markup=TELEGRAM_KEYBOARD)
            
            voices = {}
            try:
                req_v = urllib.request.Request(f"https://kodikapi.com/search?token=41f4f585f39e31d4e0e4b85d3a5ca78d&shikimori_id={a_id}&with_episodes=true")
                with urllib.request.urlopen(req_v, timeout=8) as r:
                    for item in json.loads(r.read().decode()).get("results", []):
                        v_name = item.get("translation", {}).get("title")
                        ep_count = item.get("last_episode") or item.get("episodes_count") or 1
                        if v_name:
                            voices[v_name] = max(voices.get(v_name, 0), int(ep_count))
            except Exception as e:
                logger.debug(f"Ошибка Kodik API: {e}")

            self.user_states.pop(uid, None)

            if not voices:
                self.telegram.send_message(chat_id, "😔 К сожалению, озвучки для этого аниме пока не найдены в базе.")
                return

            self.store.anime_cache[a_id] = {"title": title, "voices": voices}
            
            # Создаем кнопки с реальными озвучками
            kb = []
            v_list = list(voices.keys())[:14] # Максимум 14 кнопок
            for i in range(0, len(v_list), 2):
                row = [{"text": v, "callback_data": f"sub|{a_id}|{v[:30]}"} for v in v_list[i:i+2]]
                kb.append(row)

            self.telegram.send_message(chat_id, f"🎬 <b>{title}</b>\nДоступные озвучки найдены! Выберите нужную:", reply_markup={"inline_keyboard": kb})
            return

        if state == "chat":
            if text == "🚪 Выйти из чата":
                self.user_states.pop(uid, None)
                self.telegram.send_message(chat_id, "🚪 Вы вышли из чата.", reply_markup=TELEGRAM_KEYBOARD)
                return
            for f_id in self.store.allowed_users:
                if f_id != uid:
                    try: self.telegram.send_message(f_id, f"💬 <b>{user.get('first_name', 'Друг')}:</b>\n{text}")
                    except: pass
            return

        # === 3. ОСНОВНЫЕ КНОПКИ МЕНЮ ===
        if text in ["/start", "меню"]:
            self.telegram.send_message(chat_id, "👋 Выберите действие на клавиатуре.", reply_markup=TELEGRAM_KEYBOARD)
        elif text == "📊 Проверить всех":
            self.telegram.send_message(chat_id, "⏳ Начинаю проверку...")
            threading.Thread(target=self.check_all, daemon=True).start()
        elif text == "🌐 Открыть сайт-отчет":
            self.generate_html_report()
            h = os.getenv("RENDER_EXTERNAL_URL", f"http://0.0.0.0:{os.getenv('PORT', '15887')}")
            self.telegram.send_message(chat_id, f"🌐 Ваш отчет: {h}/anivox_report.html")
        elif text == "➕ Добавить профиль":
            self.user_states[uid] = "await_profile"
            self.telegram.send_message(chat_id, "Отправьте <b>ссылку на профиль Anivox</b>:", reply_markup=CANCEL_KEYBOARD)
        elif text == "🗑 Удалить профиль":
            self.user_states[uid] = "await_profile_del"
            msg = "Отправьте <b>ID профиля</b> для удаления.\nВаши профили:\n" + "\n".join([f"• <code>{k}</code>" for k in self.store.profiles.keys()])
            self.telegram.send_message(chat_id, msg, reply_markup=CANCEL_KEYBOARD)
        elif text == "👥 Мои профили":
            msg = "👥 <b>Отслеживаемые профили:</b>\n"
            for p in self.store.profiles.values():
                msg += f"• <b>{p.label or p.profile_id}</b> — {p.last_status or 'не проверялся'}\n"
            self.telegram.send_message(chat_id, msg or "Список пуст.")
        elif text == "🎬 Аниме трекер":
            self.user_states[uid] = "await_anime_link"
            self.telegram.send_message(chat_id, "Отправьте <b>ссылку на страницу аниме</b> (anivox.fun/anime/ID) или просто его ID:", reply_markup=CANCEL_KEYBOARD)
        elif text == "💬 Чат друзей":
            self.user_states[uid] = "chat"
            self.telegram.send_message(chat_id, "💬 Вы вошли в чат. Сообщения отправляются всем друзьям.", reply_markup=CHAT_KEYBOARD)
        
        # УДОБНОЕ УПРАВЛЕНИЕ ДРУЗЬЯМИ ЧЕРЕЗ КНОПКИ
        elif text == "🎭 Друзья":
            msg = "🎭 <b>Управление доступом</b>\n\nТекущие друзья:\n"
            for f in self.store.allowed_users:
                msg += f"• <code>{f}</code>\n"
            kb = {"inline_keyboard": [
                [{"text": "➕ Добавить друга", "callback_data": "friend_add"}],
                [{"text": "🗑 Удалить друга", "callback_data": "friend_del"}]
            ]}
            self.telegram.send_message(chat_id, msg, reply_markup=kb)

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
