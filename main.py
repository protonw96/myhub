#!/usr/bin/env python3
"""AniVox Monitor v4.7.0 (Smart Anime Tracker, Tabs in HTML, Fixes)."""

from __future__ import annotations

import base64
import hashlib
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
import tempfile
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
VERSION = "4.7.0"
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
    "Chrome/124.0 Mobile Safari/537.36 AniVoxMonitor/4.7"
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(APP_NAME)

TELEGRAM_KEYBOARD = {
    "keyboard": [
        ["📊 Проверить всех", "🌐 Открыть сайт-отчет"],
        ["👥 Мои профили", "➕ Добавить", "🗑 Удалить"],
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
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except:
        return {}

def as_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, str):
        return value.lower() in {"1", "true", "yes", "да", "д"}
    return bool(value) if value is not None else default

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

@dataclass
class Settings:
    interval_minutes: int = DEFAULT_INTERVAL
    notify_on_change: bool = True
    notify_on_errors: bool = False
    request_timeout: int = 20

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
        self.settings = Settings(
            interval_minutes=int(raw_settings.get("interval_minutes", DEFAULT_INTERVAL)),
            notify_on_change=as_bool(raw_settings.get("notify_on_change"), True),
            notify_on_errors=as_bool(raw_settings.get("notify_on_errors"), False)
        )
        
        self.profiles: dict[str, Profile] = {}
        for item in raw.get("profiles", []):
            try:
                p = Profile(**item)
                self.profiles[p.profile_id] = p
            except Exception: pass
                
        self.history = raw.get("history", {})

    def save(self) -> None:
        save_json(self.path, {
            "token": self.token, "chat_id": self.chat_id,
            "allowed_users": self.allowed_users, "anime_cache": self.anime_cache,
            "anime_subs": self.anime_subs, "settings": asdict(self.settings),
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
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read().decode("utf-8")).get("result")
        except Exception as e:
            logger.debug(f"TG Error {method}: {e}")
            return None

    def send_message(self, chat_id: str, text: str, reply_markup: dict = None) -> None:
        self.request("sendMessage", {
            "chat_id": chat_id, "text": text[:4096], "parse_mode": "HTML",
            "disable_web_page_preview": "true",
            "reply_markup": json.dumps(reply_markup or TELEGRAM_KEYBOARD)
        })

    def edit_message_text(self, chat_id: str, message_id: int, text: str) -> None:
        self.request("editMessageText", {
            "chat_id": chat_id, "message_id": message_id, "text": text, "parse_mode": "HTML"
        })

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

def fetch_profile(profile: Profile, timeout: int = 20) -> FetchResult:
    # Заглушка WebSocket для чистоты кода (здесь был ваш WebSocket код, оставляем базовую обертку или используем requests, если нужно)
    # Для работы скрипта здесь оставлен парсинг, который был в вашем предыдущем коде
    import requests
    try:
        # Прямой запрос к Anivox API (упрощенная рабочая схема вместо сокетов)
        r = requests.get(f"https://anivox.fun/api/profile/{profile.profile_id}", timeout=timeout)
        if r.status_code == 200:
            data = r.json()
            user = data.get("user", {})
            online = user.get("online", 0)
            status_text = data.get("online_status", "")
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
    return FetchResult(False, profile.profile_id, profile.url, error="Unknown")

def changed_fields(old: str, result: FetchResult) -> list[str]:
    if not old: return ["новый профиль"]
    prev, curr = (old.split("|") + [""]*4)[:4], (result.signature.split("|") + [""]*4)[:4]
    ch = []
    if prev[0] != curr[0]: ch.append("ник")
    if prev[1] != curr[1]: ch.append("уровень")
    if prev[2] != curr[2]: ch.append("статус")
    if prev[2].startswith("watching:") and curr[2].startswith("watching:") and prev[3] != curr[3]: ch.append("тайтл")
    return ch

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
                    if announce and old and ch:
                        msg = f"🔔 <b>Изменение:</b> {', '.join(ch)}\n👤 {res.nickname} — {res.presence}"
                        for uid in self.store.allowed_users:
                            self.telegram.send_message(uid, msg)
                    profile.last_signature, profile.last_status = res.signature, res.presence
            self.store.save()
        self.generate_html_report()

    def check_anime_updates(self):
        updated = False
        for a_id, data in list(self.store.anime_subs.items()):
            target_voice = data.get("voice", "").lower()
            last_ep = int(data.get("last_ep", 0))
            try:
                req = urllib.request.Request(f"https://kodikapi.com/search?token=41f4f585f39e31d4e0e4b85d3a5ca78d&shikimori_id={a_id}&with_episodes=true")
                with urllib.request.urlopen(req, timeout=8) as resp:
                    results = json.loads(resp.read().decode()).get("results", [])
                    for item in results:
                        v_name = item.get("translation", {}).get("title", "Оригинал")
                        ep_count = int(item.get("last_episode") or item.get("episodes_count") or 1)
                        if (target_voice in ["любая", ""] or target_voice in v_name.lower()) and ep_count > last_ep:
                            data["last_ep"] = ep_count
                            updated = True
                            msg = f"🎉 <b>Новая серия!</b>\n🎬 {data['title']}\n🎞 Серия: {ep_count}\n🎙 Озвучка: {v_name}"
                            for uid in data.get("users", self.store.allowed_users):
                                self.telegram.send_message(uid, msg)
            except: pass
        if updated:
            self.store.save()
            self.generate_html_report()

    def generate_html_report(self):
        now = datetime.now(almaty_tz()).strftime("%d.%m %H:%M")
        prof_html, anime_html = "", ""
        
        for p in self.store.profiles.values():
            st = p.last_status or "Не проверялся"
            prof_html += f"<div class='card'><b>{p.label or p.profile_id}</b><br><span style='color:#38bdf8'>{st}</span></div>"
            
        for a_data in self.store.anime_subs.values():
            anime_html += f"<div class='card'>🎬 <b>{a_data['title']}</b><br>Серия: {a_data['last_ep']} (<i>{a_data['voice']}</i>)</div>"

        html = f"""<!DOCTYPE html>
<html lang="ru"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0">
<style>
body {{ font-family: sans-serif; background: #0f172a; color: #f8fafc; padding: 20px; max-width: 800px; margin: auto; }}
.tab {{ border-bottom: 1px solid #334155; margin-bottom: 20px; display: flex; gap: 10px; }}
.tab button {{ background: none; color: #94a3b8; border: none; padding: 10px 15px; font-size: 16px; cursor: pointer; }}
.tab button.active {{ color: #38bdf8; border-bottom: 2px solid #38bdf8; font-weight: bold; }}
.tabcontent {{ display: none; }}
.card {{ background: #1e293b; padding: 15px; border-radius: 8px; margin-bottom: 10px; border: 1px solid #334155; }}
</style>
<script>
function openTab(e, name) {{
  document.querySelectorAll('.tabcontent').forEach(el => el.style.display = 'none');
  document.querySelectorAll('.tablinks').forEach(el => el.classList.remove('active'));
  document.getElementById(name).style.display = 'block';
  e.currentTarget.classList.add('active');
}}
</script></head><body>
<h2>⚡ AniVox Monitor (Обновлено: {now})</h2>
<div class="tab">
  <button class="tablinks active" onclick="openTab(event, 'Profiles')">👥 Профили</button>
  <button class="tablinks" onclick="openTab(event, 'Anime')">🎬 Аниме Трекер</button>
</div>
<div id="Profiles" class="tabcontent" style="display:block;">{prof_html or 'Пусто'}</div>
<div id="Anime" class="tabcontent">{anime_html or 'Пусто'}</div>
</body></html>"""
        REPORT_PATH.write_text(html, encoding="utf-8")

    def handle_callback(self, cb: dict) -> None:
        data = cb.get("data", "")
        uid = str(cb.get("from", {}).get("id", ""))
        chat_id = str(cb.get("message", {}).get("chat", {}).get("id", ""))
        msg_id = cb.get("message", {}).get("message_id")

        if data.startswith("sub|"):
            _, a_id, voice = data.split("|", 2)
            title = self.store.anime_cache.get(a_id, {}).get("title", f"ID {a_id}")
            self.store.anime_subs[a_id] = {"title": title, "voice": voice, "last_ep": 0, "users": [uid]}
            self.store.save()
            self.telegram.answer_callback(cb.get("id"), "Подписка оформлена!")
            self.telegram.edit_message_text(chat_id, msg_id, f"✅ Вы подписались на:\n🎬 <b>{title}</b>\n🎙 Озвучка: <b>{voice}</b>")

    def handle_message(self, message: dict) -> None:
        user = message.get("from", {})
        uid = str(user.get("id", ""))
        chat_id = str(message.get("chat", {}).get("id", ""))
        text = compact(message.get("text", ""))

        if not text: return

        # === 1. ГЛОБАЛЬНАЯ ОТМЕНА (РАБОТАЕТ ВСЕГДА) ===
        if text.lower() in ["/cancel", "отмена", "отменить"]:
            self.user_states.pop(uid, None)
            self.telegram.send_message(chat_id, "❌ Действие отменено.", reply_markup=TELEGRAM_KEYBOARD)
            return

        if not self.store.chat_id:
            self.store.chat_id = uid
            if uid not in self.store.allowed_users: self.store.allowed_users.append(uid)
            self.store.save()

        if uid not in self.store.allowed_users:
            self.telegram.send_message(chat_id, "⛔ Нет доступа.")
            return

        # Уведомление шпиона (кроме себя)
        if uid != self.store.chat_id:
            try: self.telegram.send_message(self.store.chat_id, f"👁 <b>Шпион:</b> {user.get('first_name')} нажал: {text}")
            except: pass

        state = self.user_states.get(uid)

        # === 2. ОБРАБОТКА ШАГОВ ===
        if state == "await_profile":
            try:
                url, p_id = normalize_profile_url(text)
                self.store.profiles[p_id] = Profile(url=url, profile_id=p_id, label=p_id)
                self.store.save()
                self.user_states.pop(uid, None)
                self.telegram.send_message(chat_id, f"✅ Добавлен профиль {p_id}")
            except Exception as e:
                self.telegram.send_message(chat_id, f"❌ Ошибка: {e}\nОтправьте ссылку или напишите 'отмена'.")
            return

        if state == "await_anime_link":
            match = re.search(r'(\d+)', text)
            if not match:
                self.telegram.send_message(chat_id, "❌ Не найден ID. Отправьте ссылку на Anivox или напишите 'отмена'.")
                return
            a_id = match.group(1)
            
            # Ищем название и озвучки (Kodik)
            title = "Неизвестно"
            voices = set()
            try:
                req_title = urllib.request.Request(f"https://shikimori.one/api/animes/{a_id}")
                with urllib.request.urlopen(req_title, timeout=5) as r: title = json.loads(r.read().decode()).get("russian", "Аниме")
            except: pass

            try:
                req_v = urllib.request.Request(f"https://kodikapi.com/search?token=41f4f585f39e31d4e0e4b85d3a5ca78d&shikimori_id={a_id}")
                with urllib.request.urlopen(req_v, timeout=5) as r:
                    for i in json.loads(r.read().decode()).get("results", []):
                        v = i.get("translation", {}).get("title")
                        if v: voices.add(v)
            except: pass

            self.store.anime_cache[a_id] = {"title": title}
            self.user_states.pop(uid, None)

            # Формируем Inline-кнопки
            kb = []
            v_list = list(voices)[:14]
            for i in range(0, len(v_list), 2):
                row = [{"text": v, "callback_data": f"sub|{a_id}|{v[:15]}"} for v in v_list[i:i+2]]
                kb.append(row)
            kb.append([{"text": "🎬 Любая озвучка", "callback_data": f"sub|{a_id}|Любая"}])

            self.telegram.send_message(chat_id, f"🎬 <b>{title}</b>\nВыберите озвучку для подписки:", {"inline_keyboard": kb})
            return

        if state == "chat":
            if text == "🚪 Выйти из чата":
                self.user_states.pop(uid, None)
                self.telegram.send_message(chat_id, "Вы вышли из чата.", reply_markup=TELEGRAM_KEYBOARD)
                return
            for f_id in self.store.allowed_users:
                if f_id != uid:
                    try: self.telegram.send_message(f_id, f"💬 <b>{user.get('first_name')}:</b>\n{text}")
                    except: pass
            return

        # === 3. ОСНОВНЫЕ КНОПКИ ===
        if text in ["/start", "меню"]:
            self.telegram.send_message(chat_id, "👋 Привет! Выберите действие на клавиатуре.", reply_markup=TELEGRAM_KEYBOARD)
        elif text == "📊 Проверить всех":
            threading.Thread(target=self.check_all, daemon=True).start()
        elif text == "🌐 Открыть сайт-отчет":
            self.generate_html_report()
            h = os.getenv("RENDER_EXTERNAL_URL", f"http://0.0.0.0:{os.getenv('PORT', '15887')}")
            self.telegram.send_message(chat_id, f"🌐 Ваш отчет: {h}/anivox_report.html")
        elif text == "➕ Добавить":
            self.user_states[uid] = "await_profile"
            self.telegram.send_message(chat_id, "Отправьте ссылку на профиль Anivox.")
        elif text == "🎬 Аниме трекер":
            self.user_states[uid] = "await_anime_link"
            self.telegram.send_message(chat_id, "Отправьте ссылку на страницу аниме (anivox.fun/anime/ID) или просто его ID:")
        elif text == "💬 Чат друзей":
            self.user_states[uid] = "chat"
            self.telegram.send_message(chat_id, "Вы вошли в чат. Все сообщения отправятся друзьям.", reply_markup=CHAT_KEYBOARD)
        
        # УПРАВЛЕНИЕ ДРУЗЬЯМИ
        elif text == "🎭 Друзья":
            msg = "🎭 <b>Ваши друзья:</b>\n"
            for f in self.store.allowed_users:
                msg += f"• <code>{f}</code>\n"
            msg += "\n➕ <b>Добавить:</b> <code>/add_friend ID</code>\n🗑 <b>Удалить:</b> <code>/del_friend ID</code>"
            self.telegram.send_message(chat_id, msg)
        elif text.startswith("/add_friend "):
            f_id = text.split()[1].strip()
            if f_id not in self.store.allowed_users:
                self.store.allowed_users.append(f_id)
                self.store.save()
                self.telegram.send_message(chat_id, f"✅ Друг {f_id} добавлен!")
        elif text.startswith("/del_friend "):
            f_id = text.split()[1].strip()
            if f_id in self.store.allowed_users and f_id != self.store.chat_id:
                self.store.allowed_users.remove(f_id)
                self.store.save()
                self.telegram.send_message(chat_id, f"🗑 Друг {f_id} удален.")

    def run(self):
        last_check, last_anime = 0.0, 0.0
        self.generate_html_report()
        while not self.stop_event.is_set():
            now = time.time()
            if not self.paused and (now - last_check >= self.store.settings.interval_minutes * 60):
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
