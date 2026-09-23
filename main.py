#!/usr/bin/env python3
"""AniVox Monitor v4.6.0 (Render 24/7, Owner Spy, Friend Chat & Anime Episode Tracker)."""

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
VERSION = "4.6.0"
CONFIG_PATH = Path(__file__).with_name("anivox_monitor.json")
LOG_PATH = Path(__file__).with_name("anivox_monitor.log")
REPORT_PATH = Path(__file__).with_name("anivox_report.html")

PROFILE_RE = re.compile(
    r"^https?://(?:www\.)?anivox\.fun/profile/([0-9]+)(?:[/?#].*)?$",
    re.IGNORECASE,
)
ALLOWED_INTERVALS = (1, 5, 10, 15, 20, 25, 30)
DEFAULT_INTERVAL = 10
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Linux; Android 10; Mobile) AppleWebKit/537.36 "
    "Chrome/124.0 Mobile Safari/537.36 AniVoxMonitor/4.6"
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)]
)
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
    "one_time_keyboard": False,
}

CHAT_KEYBOARD = {
    "keyboard": [
        ["🚪 Выйти из чата"]
    ],
    "resize_keyboard": True,
    "one_time_keyboard": False,
}

def almaty_tz() -> timezone:
    return timezone(timedelta(hours=5), name="ALMT")

def almaty_now() -> str:
    return datetime.now(almaty_tz()).isoformat(timespec="seconds")

def compact(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()

def mask_secret(value: str) -> str:
    return "не задан" if not value else f"{value[:4]}…{value[-4:]}"

def save_json(path: Path, data: dict[str, Any]) -> None:
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temp, path)
    try:
        path.chmod(0o600)
    except OSError:
        pass

def load_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("Не удалось прочитать настройки: %s", exc)
        return {}

def as_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.lower() in {"1", "true", "yes", "да", "д"}
    return default

def normalize_profile_url(value: str) -> tuple[str, str]:
    candidate = value.strip()
    if not candidate.startswith(("http://", "https://")):
        candidate = "https://" + candidate
    parsed = urllib.parse.urlparse(candidate)
    normalized = urllib.parse.urlunparse(
        ("https", parsed.netloc.lower(), parsed.path.rstrip("/"), "", "", "")
    )
    match = PROFILE_RE.match(normalized)
    if not match:
        raise ValueError("Нужна ссылка вида https://anivox.fun/profile/27788")
    return normalized, match.group(1)

def fetch_anime_info(title: str) -> dict[str, Any]:
    """Запрашивает жанры и ID аниме через открытый API Shikimori."""
    if not title or title.lower() in ["неизвестно", "тайтл"]:
        return {"genres": [], "id": ""}
    try:
        time.sleep(1)
        search_url = f"https://shikimori.one/api/animes?search={urllib.parse.quote(title)}&limit=1"
        req = urllib.request.Request(search_url, headers={"User-Agent": DEFAULT_USER_AGENT})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            if not data:
                return {"genres": [], "id": ""}
            anime_id = str(data[0]["id"])
            
        detail_url = f"https://shikimori.one/api/animes/{anime_id}"
        req2 = urllib.request.Request(detail_url, headers={"User-Agent": DEFAULT_USER_AGENT})
        with urllib.request.urlopen(req2, timeout=10) as resp2:
            detail_data = json.loads(resp2.read().decode("utf-8"))
            genres = [g.get("russian") or g.get("name") for g in detail_data.get("genres", [])]
            return {"genres": genres, "id": anime_id}
    except Exception as e:
        logger.warning(f"Ошибка Shikimori API для '{title}': {e}")
        return {"genres": [], "id": ""}

def background_fetch_info(title: str, anime_cache: dict, store_path: Path) -> None:
    if title in anime_cache and isinstance(anime_cache[title], dict) and anime_cache[title].get("id"):
        return
    info = fetch_anime_info(title)
    anime_cache[title] = info
    raw = load_json(store_path)
    raw["anime_cache"] = anime_cache
    save_json(store_path, raw)

def get_anime_data(title: str, cache: dict) -> tuple[str, str]:
    cached = cache.get(title)
    genres_str = "-"
    link = "https://anivox.fun"
    if isinstance(cached, dict):
        genres_str = ", ".join(cached.get("genres", [])) or "-"
        if cached.get("id"):
            link = f"https://anivox.fun/anime/{cached['id']}"
    elif isinstance(cached, list):
        genres_str = ", ".join(cached) or "-"
    return genres_str, link

# --- ПОИСК СЕРИЙ И ОЗВУЧЕК (Kodik API & AniLibria) ---
def fetch_anime_release_data(title: str) -> dict[str, int]:
    """Возвращает словарь: {'Озвучка': номер_последней_серии}."""
    results: dict[str, int] = {}
    title_clean = title.strip()
    if not title_clean:
        return results

    # 1. Запрос к Kodik
    try:
        url = f"https://kodikapi.com/search?token=41f4f585f39e31d4e0e4b85d3a5ca78d&title={urllib.parse.quote(title_clean)}&with_episodes=true"
        req = urllib.request.Request(url, headers={"User-Agent": DEFAULT_USER_AGENT})
        with urllib.request.urlopen(req, timeout=8) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            for item in data.get("results", []):
                trans = item.get("translation", {}).get("title") or "Оригинал"
                last_ep = item.get("last_episode") or item.get("episodes_count") or 1
                try:
                    last_ep_int = int(last_ep)
                    if trans not in results or last_ep_int > results[trans]:
                        results[trans] = last_ep_int
                except (ValueError, TypeError):
                    continue
    except Exception as e:
        logger.debug(f"Ошибка проверки Kodik для '{title}': {e}")

    # 2. Запасной запрос к AniLibria
    try:
        al_url = f"https://api.anilibria.tv/v3/title/search?search={urllib.parse.quote(title_clean)}"
        req = urllib.request.Request(al_url, headers={"User-Agent": DEFAULT_USER_AGENT})
        with urllib.request.urlopen(req, timeout=8) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            if data and isinstance(data, list):
                last_ep = data[0].get("player", {}).get("episodes", {}).get("last", 0)
                if last_ep:
                    results["AniLibria"] = max(results.get("AniLibria", 0), int(last_ep))
    except Exception as e:
        logger.debug(f"Ошибка проверки AniLibria для '{title}': {e}")

    return results

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
    auto_cleanup_days: int = 15
    notify_on_change: bool = True
    notify_on_online: bool = True
    notify_on_offline: bool = True
    notify_on_title: bool = True
    notify_on_errors: bool = False
    request_timeout: int = 20
    min_request_gap_seconds: int = 5

class Store:
    def __init__(self, path: Path) -> None:
        raw = load_json(path)
        self.path = path
        # Считываем токен из файла или системных переменных Render
        self.token = str(raw.get("token", "")).strip() or os.getenv("BOT_TOKEN", "").strip()
        self.chat_id = str(raw.get("chat_id", "")).strip() or os.getenv("CHAT_ID", "").strip()
        
        self.allowed_users: list[str] = [str(u) for u in raw.get("allowed_users", [])]
        if self.chat_id and self.chat_id not in self.allowed_users:
            self.allowed_users.append(self.chat_id)
            
        self.anime_cache: dict[str, Any] = raw.get("anime_cache", {})
        # Подписки на аниме-серии: { "title": {"title": str, "voice": str, "last_ep": int, "users": [str]} }
        self.anime_subs: dict[str, dict[str, Any]] = raw.get("anime_subs", {})

        raw_settings = raw.get("settings", {})
        raw_settings = raw_settings if isinstance(raw_settings, dict) else {}
        interval = int(raw_settings.get("interval_minutes", DEFAULT_INTERVAL))
        if interval not in ALLOWED_INTERVALS:
            interval = DEFAULT_INTERVAL
            
        self.settings = Settings(
            interval_minutes=interval,
            auto_cleanup_days=int(raw_settings.get("auto_cleanup_days", 15)),
            notify_on_change=as_bool(raw_settings.get("notify_on_change"), True),
            notify_on_online=as_bool(raw_settings.get("notify_on_online"), True),
            notify_on_offline=as_bool(raw_settings.get("notify_on_offline"), True),
            notify_on_title=as_bool(raw_settings.get("notify_on_title"), True),
            notify_on_errors=as_bool(raw_settings.get("notify_on_errors"), False),
            request_timeout=max(5, min(90, int(raw_settings.get("request_timeout", 20)))),
            min_request_gap_seconds=max(
                1, min(300, int(raw_settings.get("min_request_gap_seconds", 5)))
            ),
        )
        
        self.profiles: dict[str, Profile] = {}
        raw_profiles = raw.get("profiles", [])
        if isinstance(raw_profiles, list):
            for item in raw_profiles:
                if not isinstance(item, dict):
                    continue
                try:
                    profile = Profile(
                        url=str(item["url"]),
                        profile_id=str(item["profile_id"]),
                        label=str(item.get("label", "")),
                        last_signature=str(item.get("last_signature", "")),
                        last_check=str(item.get("last_check", "")),
                        last_ok=as_bool(item.get("last_ok"), False),
                        last_error=str(item.get("last_error", "")),
                        last_status=str(item.get("last_status", "")),
                    )
                    self.profiles[profile.profile_id] = profile
                except (KeyError, TypeError):
                    continue
                
        self.history: dict[str, list[dict[str, Any]]] = {}
        raw_history = raw.get("history", {})
        if isinstance(raw_history, dict):
            for profile_id, entries in raw_history.items():
                if isinstance(entries, list):
                    cleaned = []
                    for item in entries:
                        if isinstance(item, dict):
                            cleaned.append(item)
                    self.history[str(profile_id)] = cleaned[-500:]

    def save(self) -> None:
        save_json(
            self.path,
            {
                "token": self.token,
                "chat_id": self.chat_id,
                "allowed_users": self.allowed_users,
                "anime_cache": self.anime_cache,
                "anime_subs": self.anime_subs,
                "settings": asdict(self.settings),
                "profiles": [asdict(p) for p in self.profiles.values()],
                "history": self.history,
            },
        )

    def add_history(self, profile_id: str, result: "FetchResult", changes: list[str]) -> None:
        if not result.ok or not changes:
            return
        entries = self.history.setdefault(profile_id, [])
        if entries:
            last = entries[-1]
            if (last.get("presence") == result.presence and 
                last.get("nickname") == result.nickname and 
                last.get("level") == result.level and
                last.get("title") == result.title):
                return

        if result.title:
            cached = self.anime_cache.get(result.title)
            if not cached or (isinstance(cached, dict) and not cached.get("id")):
                threading.Thread(target=background_fetch_info, args=(result.title, self.anime_cache, self.path), daemon=True).start()

        entries.append({
            "checked_at": result.checked_at,
            "ok": result.ok,
            "nickname": result.nickname,
            "level": result.level,
            "presence": result.presence,
            "title": result.title,
            "signature": result.signature,
            "changes": changes,
            "error": result.error,
        })
        self.history[profile_id] = entries[-500:]

class TelegramError(RuntimeError):
    pass

class TelegramClient:
    def __init__(self, token: str, timeout: int = 30) -> None:
        self.token = token.strip()
        self.timeout = timeout
        self.base_url = f"https://api.telegram.org/bot{self.token}/"
        self.offset = 0

    def request(self, method: str, payload: Optional[dict[str, Any]] = None) -> Any:
        body = urllib.parse.urlencode(payload or {}).encode()
        request = urllib.request.Request(
            self.base_url + method,
            data=body,
            headers={"User-Agent": f"{APP_NAME}/{VERSION}"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                result = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise TelegramError(f"HTTP {exc.code}: {detail[:300]}") from exc
        except Exception as exc:
            raise TelegramError(f"Network error: {exc}") from exc
            
        if not result.get("ok"):
            raise TelegramError(str(result.get("description", "ошибка Telegram")))
        return result.get("result")

    def send_message(self, chat_id: str, text: str, reply_markup: Optional[dict[str, Any]] = None) -> None:
        self.request(
            "sendMessage",
            {
                "chat_id": chat_id,
                "text": text[:4096],
                "parse_mode": "HTML",
                "disable_web_page_preview": "true",
                "reply_markup": json.dumps(reply_markup or TELEGRAM_KEYBOARD, ensure_ascii=False),
            },
        )

    def send_document(self, chat_id: str, path: Path, caption: str = "") -> None:
        boundary = f"----AniVoxMonitor{os.urandom(8).hex()}"
        file_bytes = path.read_bytes()
        parts: list[bytes] = []

        def field(name: str, value: str) -> None:
            parts.extend([
                f"--{boundary}\r\n".encode(),
                f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode(),
                value.encode("utf-8"),
                b"\r\n",
            ])

        field("chat_id", chat_id)
        if caption:
            field("caption", caption[:1024])
            
        parts.extend([
            f"--{boundary}\r\n".encode(),
            f'Content-Disposition: form-data; name="document"; filename="{path.name}"\r\n'.encode(),
            b"Content-Type: text/html\r\n\r\n" if path.name.endswith(".html") else b"Content-Type: application/json\r\n\r\n",
            file_bytes,
            b"\r\n",
            f"--{boundary}--\r\n".encode(),
        ])
        request = urllib.request.Request(
            self.base_url + "sendDocument",
            data=b"".join(parts),
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}", "User-Agent": f"{APP_NAME}/{VERSION}"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as resp:
                res = json.loads(resp.read().decode("utf-8"))
                if not res.get("ok"):
                    raise TelegramError(res.get("description"))
        except Exception as exc:
            raise TelegramError(f"Ошибка отправки файла: {exc}") from exc

    def get_updates(self, timeout: int = 5) -> list[dict[str, Any]]:
        result = self.request(
            "getUpdates",
            {
                "offset": str(self.offset),
                "timeout": str(max(0, min(25, timeout))),
                "allowed_updates": json.dumps(["message", "callback_query"]),
            },
        )
        updates = result if isinstance(result, list) else []
        for item in updates:
            if isinstance(item, dict) and isinstance(item.get("update_id"), int):
                self.offset = item["update_id"] + 1
        return updates

class FetchResult:
    def __init__(
        self,
        ok: bool,
        profile_id: str,
        url: str,
        nickname: str = "неизвестно",
        level: str = "неизвестно",
        presence: str = "неизвестно",
        title: str = "",
        error: str = "",
        source: str = "AniVox WebSocket /api",
        last_seen_timestamp: float = 0,
    ) -> None:
        self.ok = ok
        self.profile_id = profile_id
        self.url = url
        self.nickname = nickname or "неизвестно"
        self.level = level or "неизвестно"
        self.presence = presence or "неизвестно"
        self.title = title
        self.error = error
        self.source = source
        self.last_seen_timestamp = last_seen_timestamp
        self.checked_at = almaty_now()

    @property
    def signature(self) -> str:
        if self.presence.startswith("Смотрит:"):
            state = "watching:" + self.title.lower()
        elif self.presence.startswith("Онлайн"):
            state = "online"
        else:
            state = "offline"
        return "|".join((self.nickname.lower(), self.level, state, self.title.lower())).lower()

def recv_exact(sock: socket.socket, length: int) -> bytes:
    chunks: list[bytes] = []
    remaining = length
    while remaining:
        chunk = sock.recv(remaining)
        if not chunk:
            raise ConnectionError("AniVox WebSocket закрыл соединение")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)

def ws_send_text(sock: socket.socket, payload: dict[str, Any]) -> None:
    data = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    mask = os.urandom(4)
    masked = bytes(byte ^ mask[i % 4] for i, byte in enumerate(data))
    length = len(data)
    if length < 126:
        header = bytes((0x81, 0x80 | length))
    elif length < 65536:
        header = bytes((0x81, 0x80 | 126)) + struct.pack("!H", length)
    else:
        header = bytes((0x81, 0x80 | 127)) + struct.pack("!Q", length)
    sock.sendall(header + mask + masked)

def ws_read_message(sock: socket.socket) -> Optional[str]:
    fragments: list[bytes] = []
    while True:
        header = recv_exact(sock, 2)
        opcode = header[0] & 0x0F
        masked = bool(header[1] & 0x80)
        length = header[1] & 0x7F
        if length == 126:
            length = struct.unpack("!H", recv_exact(sock, 2))[0]
        elif length == 127:
            length = struct.unpack("!Q", recv_exact(sock, 8))[0]
        mask = recv_exact(sock, 4) if masked else b""
        data = recv_exact(sock, length)
        if masked:
            data = bytes(byte ^ mask[i % 4] for i, byte in enumerate(data))
        if opcode == 8:
            return None
        if opcode == 9:
            sock.sendall(bytes((0x8A, len(data))) + data)
            continue
        if opcode == 10:
            continue
        if opcode in (1, 0):
            fragments.append(data)
            if header[0] & 0x80:
                return b"".join(fragments).decode("utf-8", errors="replace")

def anivox_websocket_profile(profile_id: str, timeout: int = 20) -> dict[str, Any]:
    host = "anivox.fun"
    raw_socket = socket.create_connection((host, 443), timeout=timeout)
    context = ssl.create_default_context()
    sock = context.wrap_socket(raw_socket, server_hostname=host)
    sock.settimeout(timeout)
    key = base64.b64encode(os.urandom(16)).decode("ascii")
    handshake = (
        f"GET /api HTTP/1.1\r\n"
        f"Host: {host}\r\n"
        "Upgrade: websocket\r\n"
        "Connection: Upgrade\r\n"
        f"Sec-WebSocket-Key: {key}\r\n"
        "Sec-WebSocket-Version: 13\r\n"
        f"Origin: https://{host}\r\n"
        f"User-Agent: {DEFAULT_USER_AGENT}\r\n\r\n"
    ).encode("ascii")
    try:
        sock.sendall(handshake)
        response = b""
        while b"\r\n\r\n" not in response:
            response += sock.recv(4096)
            if len(response) > 65536:
                raise ConnectionError("слишком длинный handshake")
        header_text = response.decode("latin1", errors="replace")
        if " 101 " not in header_text.splitlines()[0]:
            raise ConnectionError(f"AniVox WS отказ: {header_text.splitlines()[0]}")
        ws_send_text(sock, {"type": "get_profile", "id": int(profile_id)})
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            message = ws_read_message(sock)
            if not message:
                continue
            try:
                data = json.loads(message)
                if data.get("type") == "get_profile":
                    return data
            except json.JSONDecodeError:
                continue
        raise TimeoutError("AniVox не ответил get_profile")
    finally:
        try:
            sock.close()
        except OSError:
            pass

def format_last_seen(timestamp: float) -> str:
    if timestamp <= 0:
        return "Был в сети: время неизвестно"
    dt = datetime.fromtimestamp(timestamp, tz=almaty_tz())
    age = max(0, int(time.time() - timestamp))
    if age < 60:
        relative = "только что"
    elif age < 3600:
        relative = f"{age // 60} мин. назад"
    elif age < 86400:
        relative = f"{age // 3600} ч. назад"
    else:
        relative = f"{age // 86400} дн. назад"
    return f"Был в сети: {relative} ({dt.strftime('%H:%M')})"

def result_from_ws(data: dict[str, Any], profile_id: str, url: str) -> FetchResult:
    if not isinstance(data, dict):
        return FetchResult(False, profile_id, url, error="Некорректный ответ AniVox")
    container = data.get("data")
    user = container.get("user") if isinstance(container, dict) else None
    if not isinstance(user, dict):
        return FetchResult(False, profile_id, url, error="Нет объекта data.user")
    online_value = user.get("online", 0)
    try:
        online_timestamp = float(online_value or 0)
    except (TypeError, ValueError):
        online_timestamp = 0
    online_status = container.get("online_status", "")
    if online_timestamp == 0:
        title = compact(online_status.get("text", "") if isinstance(online_status, dict) else online_status)
        title = re.sub(r"^(?:смотрит|watching)\s*[:：-]?\s*", "", title, flags=re.IGNORECASE).strip()
        presence = f"Смотрит: {title}" if title else "Онлайн"
    else:
        title = ""
        presence = format_last_seen(online_timestamp)
    return FetchResult(
        True, profile_id, url,
        nickname=compact(user.get("username")) or "неизвестно",
        level=str(user.get("level", "неизвестно")),
        presence=presence,
        title=title,
        last_seen_timestamp=online_timestamp,
    )

def fetch_profile(profile: Profile, timeout: int = 20) -> FetchResult:
    try:
        data = anivox_websocket_profile(profile.profile_id, timeout)
        return result_from_ws(data, profile.profile_id, profile.url)
    except Exception as exc:
        return FetchResult(False, profile.profile_id, profile.url, error=str(exc))

def format_profile(result: FetchResult, cache: dict, label: str = "") -> str:
    name = html.escape(result.nickname + (f" ({label})" if label else ""))
    lines = [
        f"<b>👤 Профиль:</b> {name}",
        f"<b>⭐ Лвл:</b> {html.escape(str(result.level))}",
    ]
    if result.presence.startswith("Смотрит:"):
        escaped_title = html.escape(result.title)
        _, link = get_anime_data(result.title, cache)
        lines.append(f"🍿 <b>Смотрит:</b> <a href='{link}'>{escaped_title}</a>")
    else:
        status_ico = "🟢" if result.presence.startswith("Онлайн") else "🔴"
        lines.append(f"{status_ico} <b>Статус:</b> {html.escape(result.presence)}")
    try:
        dt = datetime.fromisoformat(result.checked_at).strftime('%H:%M')
    except Exception:
        dt = result.checked_at
    lines.extend([
        f"<i>⏱ Проверено: {dt} (Алматы)</i>",
        f"<a href='{result.url}'>🌐 Открыть профиль</a>"
    ])
    return "\n".join(lines)

def changed_fields(old: str, result: FetchResult) -> list[str]:
    if not old:
        return ["новый профиль"]
    previous = (old.split("|") + [""] * 4)[:4]
    current = (result.signature.split("|") + [""] * 4)[:4]
    changes = []
    if previous[0] != current[0]: changes.append("ник")
    if previous[1] != current[1]: changes.append("уровень")
    if previous[2] != current[2]: changes.append("статус")
    if previous[2].startswith("watching:") and current[2].startswith("watching:") and previous[3] != current[3]:
        changes.append("тайтл")
    return changes

# --- WEB СЕРВЕР ДЛЯ RENDER (Слушает PORT и отдает отчет) ---
class QuietHTTPRequestHandler(http.server.SimpleHTTPRequestHandler):
    def do_GET(self):
        # На Render любой заход на корень / отдает anivox_report.html
        if self.path in ("/", "/index.html"):
            self.path = "/anivox_report.html"
        return super().do_GET()

    def log_message(self, format, *args):
        pass

def run_web_server():
    port = int(os.getenv("PORT", 15887))
    work_dir = os.path.dirname(os.path.abspath(CONFIG_PATH))
    os.chdir(work_dir)
    try:
        socketserver.TCPServer.allow_reuse_address = True
        with socketserver.TCPServer(("0.0.0.0", port), QuietHTTPRequestHandler) as httpd:
            logger.info(f"Web-сервер запущен: порт {port} (0.0.0.0)")
            httpd.serve_forever()
    except Exception as e:
        logger.error(f"Ошибка запуска Web-сервера: {e}")

# --- КЛАСС МОНИТОРА ---
class Monitor:
    def __init__(self, store: Store, telegram: TelegramClient) -> None:
        self.store = store
        self.telegram = telegram
        self.stop_event = threading.Event()
        self.paused = False
        self.check_lock = threading.RLock()
        self.user_states: dict[str, str] = {} # Состояния (чат, добавление аниме и т.д.)

    def stop(self, *_args: Any) -> None:
        self.stop_event.set()

    # ФУНКЦИЯ ШПИОНА: Уведомляет владельца о ЛЮБЫХ действиях других пользователей
    def notify_owner_spy(self, user_info: dict, action_desc: str) -> None:
        if not self.store.chat_id:
            return
        uid = str(user_info.get("id", ""))
        # Не спамим владельцу о его собственных действиях
        if uid == str(self.store.chat_id):
            return
            
        first_name = user_info.get("first_name", "Без имени")
        username = f"@{user_info.get('username')}" if user_info.get("username") else "нет юзернейма"
        text = (
            f"👁 <b>[Шпион] Действие пользователя:</b>\n"
            f"👤 <b>Кто:</b> {html.escape(first_name)} ({username})\n"
            f"🆔 <b>ID:</b> <code>{uid}</code>\n"
            f"⚡ <b>Сделал:</b> {html.escape(action_desc)}"
        )
        try:
            self.telegram.send_message(self.store.chat_id, text)
        except Exception:
            pass

    def check_one(self, profile: Profile, announce: bool) -> FetchResult:
        result = fetch_profile(profile, self.store.settings.request_timeout)
        with self.check_lock:
            profile.last_check = result.checked_at
            old_signature = profile.last_signature
            changes = changed_fields(old_signature, result)
            profile.last_ok = result.ok
            profile.last_error = result.error
            self.store.add_history(profile.profile_id, result, changes)
            
            if result.ok:
                if announce and old_signature and changes:
                    msg_text = "<b>🔔 Изменение:</b> " + ", ".join(changes) + "\n\n" + format_profile(result, self.store.anime_cache, profile.label)
                    for uid in self.store.allowed_users:
                        try:
                            self.telegram.send_message(uid, msg_text)
                        except TelegramError as exc:
                            logger.warning("Notification failed for %s: %s", uid, exc)
                profile.last_signature = result.signature
                profile.last_status = result.presence
            self.store.save()
            return result

    def check_all(self, announce: bool = True) -> list[FetchResult]:
        results = []
        for profile in list(self.store.profiles.values()):
            if self.stop_event.is_set():
                break
            results.append(self.check_one(profile, announce))
        self.generate_html_report()
        return results

    # ПРОВЕРКА ВЫХОДА СЕРИЙ АНИМЕ В ОЗВУЧКЕ
    def check_anime_updates(self) -> None:
        if not self.store.anime_subs:
            return
        updated = False
        for title, sub_info in list(self.store.anime_subs.items()):
            target_voice = sub_info.get("voice", "").strip().lower()
            last_known_ep = int(sub_info.get("last_ep", 0))
            
            data = fetch_anime_release_data(title)
            if not data:
                continue
                
            for voice_name, ep_num in data.items():
                is_match = (not target_voice or target_voice == "любая" or target_voice in voice_name.lower())
                if is_match and ep_num > last_known_ep:
                    sub_info["last_ep"] = ep_num
                    updated = True
                    alert_text = (
                        f"🎉 <b>Вышла новая серия аниме!</b>\n\n"
                        f"🎬 <b>Тайтл:</b> {html.escape(title)}\n"
                        f"🎞 <b>Серия:</b> {ep_num}\n"
                        f"🎙 <b>Озвучка:</b> {html.escape(voice_name)}\n"
                        f"⏱ <i>Обнаружено: {datetime.now(almaty_tz()).strftime('%H:%M')} (Алматы)</i>"
                    )
                    for uid in sub_info.get("users", self.store.allowed_users):
                        try:
                            self.telegram.send_message(uid, alert_text)
                        except Exception:
                            pass
        if updated:
            self.store.save()
            self.generate_html_report()

    # ГЕНЕРАТОР ПОЛНОГО HTML-САЙТА (ТА САМАЯ ФУНКЦИЯ, КОТОРАЯ БЫЛА ОБОРВАНА)
    def generate_html_report(self) -> Path:
        """Создает стильный anivox_report.html с темной темой и автообновлением."""
        now_str = datetime.now(almaty_tz()).strftime("%d.%m.%Y %H:%M:%S")
        profiles_html = []
        
        for p in self.store.profiles.values():
            status = p.last_status or "Не проверялся"
            is_online = "онлайн" in status.lower() or "смотрит" in status.lower()
            badge_class = "badge-online" if is_online else "badge-offline"
            history = self.store.history.get(p.profile_id, [])
            last_entry = history[-1] if history else {}
            title = last_entry.get("title", "")
            
            hist_rows = ""
            for h in history[-5:][::-1]:
                hist_rows += f"<li><span class='time'>{h.get('checked_at','')[11:16]}</span> — {html.escape(h.get('presence',''))}</li>"
                
            profiles_html.append(f"""
            <div class="card">
                <div class="card-header">
                    <span class="name">{html.escape(p.label or p.profile_id)}</span>
                    <span class="badge {badge_class}">{html.escape(status)}</span>
                </div>
                <div class="card-body">
                    <p><b>ID профиля:</b> <a href="{p.url}" target="_blank">{p.profile_id}</a></p>
                    {f'<p class="watching">🍿 Смотрит: <b>{html.escape(title)}</b></p>' if title else ''}
                    <div class="history">
                        <b>Последние события:</b>
                        <ul>{hist_rows or '<li>История пуста</li>'}</ul>
                    </div>
                </div>
            </div>
            """)

        anime_html = []
        for a_title, a_data in self.store.anime_subs.items():
            anime_html.append(f"""
            <div class="anime-badge">
                🎬 <b>{html.escape(a_title)}</b> — Серия: <span class="ep-num">{a_data.get('last_ep', '?')}</span> 
                (Озвучка: <i>{html.escape(a_data.get('voice', 'Любая'))}</i>)
            </div>
            """)

        html_content = f"""<!DOCTYPE html>
<html lang="ru">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <meta http-equiv="refresh" content="30">
    <title>{APP_NAME} — Отчет</title>
    <style>
        :root {{ --bg: #0f172a; --card: #1e293b; --text: #f8fafc; --accent: #38bdf8; --green: #22c55e; --red: #ef4444; }}
        body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; background: var(--bg); color: var(--text); margin: 0; padding: 20px; }}
        .container {{ max-width: 900px; margin: 0 auto; }}
        header {{ text-align: center; margin-bottom: 25px; border-bottom: 1px solid #334155; padding-bottom: 15px; }}
        h1 {{ color: var(--accent); margin: 0 0 5px 0; font-size: 24px; }}
        .updated {{ font-size: 13px; color: #94a3b8; }}
        .grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(280px, 1fr)); gap: 15px; }}
        .card {{ background: var(--card); border-radius: 12px; padding: 15px; border: 1px solid #334155; box-shadow: 0 4px 6px rgba(0,0,0,0.2); }}
        .card-header {{ display: flex; justify-content: space-between; align-items: center; margin-bottom: 10px; }}
        .name {{ font-weight: bold; font-size: 17px; }}
        .badge {{ padding: 4px 10px; border-radius: 20px; font-size: 12px; font-weight: bold; }}
        .badge-online {{ background: rgba(34, 197, 94, 0.2); color: var(--green); border: 1px solid var(--green); }}
        .badge-offline {{ background: rgba(239, 68, 68, 0.2); color: var(--red); border: 1px solid var(--red); }}
        .watching {{ color: #fde047; margin: 8px 0; }}
        .history {{ margin-top: 10px; font-size: 13px; }}
        .history ul {{ margin: 5px 0; padding-left: 20px; color: #cbd5e1; }}
        .time {{ color: #94a3b8; font-weight: bold; }}
        a {{ color: var(--accent); text-decoration: none; }}
        .section-title {{ margin: 30px 0 15px 0; color: var(--accent); font-size: 19px; }}
        .anime-badge {{ background: var(--card); padding: 10px 15px; border-radius: 8px; margin-bottom: 8px; border: 1px solid #334155; }}
        .ep-num {{ color: var(--green); font-weight: bold; }}
    </style>
</head>
<body>
    <div class="container">
        <header>
            <h1>⚡ AniVox Live Monitor</h1>
            <div class="updated">Обновлено: {now_str} (Алматы) • Автообновление каждые 30 сек</div>
        </header>

        <div class="section-title">👥 Отслеживаемые пользователи</div>
        <div class="grid">
            {''.join(profiles_html) if profiles_html else '<p>Нет добавленных профилей.</p>'}
        </div>

        <div class="section-title">🎬 Отслеживание серий аниме</div>
        <div class="anime-list">
            {''.join(anime_html) if anime_html else '<p>Тайтлы пока не добавлены (/add_anime).</p>'}
        </div>
    </div>
</body>
</html>"""
        REPORT_PATH.write_text(html_content, encoding="utf-8")
        return REPORT_PATH

    def export_history(self, chat_id: str) -> str:
        path = CONFIG_PATH.with_name("anivox_history_export.json")
        payload = {
            "exported_at": almaty_now(),
            "profiles": [asdict(p) for p in self.store.profiles.values()],
            "history": self.store.history,
        }
        save_json(path, payload)
        try:
            self.telegram.send_document(chat_id, path, "История AniVox (JSON)")
            return "Экспорт истории отправлен файлом."
        except Exception as exc:
            return f"Не удалось отправить экспорт: {exc}"

    # ОБРАБОТЧИК СООБЩЕНИЙ ТЕЛЕГРАМ
    def handle_message(self, message: dict[str, Any]) -> None:
        user = message.get("from", {})
        uid = str(user.get("id", ""))
        chat_id = str(message.get("chat", {}).get("id", ""))
        text = compact(message.get("text", ""))

        if not text:
            return

        # Привязка первого запустившего как владельца
        if not self.store.chat_id:
            self.store.chat_id = uid
            if uid not in self.store.allowed_users:
                self.store.allowed_users.append(uid)
            self.store.save()
            self.telegram.send_message(chat_id, f"👑 Вы назначены владельцем бота! (ID: {uid})")

        # ПРОВЕРКА ДОСТУПА
        if uid not in self.store.allowed_users:
            # ШПИОН: оповещаем владельца о попытке доступа
            self.notify_owner_spy(user, f"Попытка доступа чужого: {text}")
            self.telegram.send_message(chat_id, "⛔ У вас нет доступа к боту. Обратитесь к владельцу.")
            return

        # ШПИОН: логируем любое действие для владельца
        self.notify_owner_spy(user, f"Команда/Кнопка: {text}")

        # РЕЖИМ ЧАТА С ДРУЗЬЯМИ
        if self.user_states.get(uid) == "chat":
            if text in ["🚪 Выйти из чата", "/exit"]:
                self.user_states.pop(uid, None)
                self.telegram.send_message(chat_id, "🚪 Вы вышли из чата друзей.", reply_markup=TELEGRAM_KEYBOARD)
                return
            # Пересылаем сообщение всем друзьям
            sender_name = user.get("first_name", "Друг")
            broadcast_text = f"💬 <b>[Чат друзей] {html.escape(sender_name)}:</b>\n{html.escape(text)}"
            for friend_id in self.store.allowed_users:
                if friend_id != uid:
                    try:
                        self.telegram.send_message(friend_id, broadcast_text)
                    except Exception:
                        pass
            return

        # ОБРАБОТКА ШАГОВЫХ СОСТОЯНИЙ
        state = self.user_states.get(uid)
        if state == "await_profile":
            try:
                norm_url, p_id = normalize_profile_url(text)
                self.store.profiles[p_id] = Profile(url=norm_url, profile_id=p_id, label=p_id)
                self.store.save()
                self.user_states.pop(uid, None)
                self.telegram.send_message(chat_id, f"✅ Профиль {p_id} успешно добавлен!", reply_markup=TELEGRAM_KEYBOARD)
                self.check_all(announce=False)
            except Exception as e:
                self.telegram.send_message(chat_id, f"❌ Ошибка ссылки: {e}. Попробуйте еще раз или /cancel.")
            return

        elif state == "await_anime":
            # Формат: Название | Озвучка (или просто Название)
            parts = [p.strip() for p in text.split("|")]
            title = parts[0]
            voice = parts[1] if len(parts) > 1 else "Любая"
            self.store.anime_subs[title] = {"title": title, "voice": voice, "last_ep": 0, "users": [uid]}
            self.store.save()
            self.user_states.pop(uid, None)
            self.telegram.send_message(chat_id, f"✅ Тайтл <b>{html.escape(title)}</b> добавлен в трекер!\nОзвучка: <i>{html.escape(voice)}</i>", reply_markup=TELEGRAM_KEYBOARD)
            threading.Thread(target=self.check_anime_updates, daemon=True).start()
            return

        # ОСНОВНОЕ МЕНЮ И КНОПКИ
        if text in ["/start", "меню"]:
            self.telegram.send_message(
                chat_id,
                f"👋 <b>Привет! Я AniVox Monitor v{VERSION}</b>\n\n"
                "• Мониторинг профилей AniVox 24/7\n"
                "• Отслеживание выхода новых серий в озвучке\n"
                "• Общий чат для друзей\n"
                "• Онлайн веб-отчет",
                reply_markup=TELEGRAM_KEYBOARD
            )

        elif text in ["📊 Проверить всех", "/check"]:
            self.telegram.send_message(chat_id, "⏳ Начинаю проверку профилей...")
            threading.Thread(target=self.check_all, args=(True,), daemon=True).start()

        elif text in ["🌐 Открыть сайт-отчет", "/site"]:
            self.generate_html_report()
            host_render = os.getenv("RENDER_EXTERNAL_URL", "")
            port = os.getenv("PORT", "15887")
            link = f"{host_render}/anivox_report.html" if host_render else f"http://0.0.0.0:{port}/anivox_report.html"
            self.telegram.send_message(chat_id, f"🌐 <b>Ваш веб-отчет доступен по ссылке:</b>\n{link}")

        elif text in ["👥 Мои профили", "🎭 Друзья"]:
            if not self.store.profiles:
                self.telegram.send_message(chat_id, "Список профилей пуст. Нажмите «➕ Добавить».")
            else:
                lines = ["<b>👥 Отслеживаемые профили:</b>\n"]
                for p in self.store.profiles.values():
                    lines.append(f"• <b>{html.escape(p.label or p.profile_id)}</b> — {html.escape(p.last_status or 'не проверялся')} (<a href='{p.url}'>ссылка</a>)")
                self.telegram.send_message(chat_id, "\n".join(lines))

        elif text in ["➕ Добавить", "/add"]:
            self.user_states[uid] = "await_profile"
            self.telegram.send_message(chat_id, "Отправьте ссылку на профиль AniVox (например, https://anivox.fun/profile/27788):")

        elif text in ["🗑 Удалить", "/del"]:
            if not self.store.profiles:
                self.telegram.send_message(chat_id, "Список пуст.")
            else:
                msg = "Чтобы удалить профиль, напишите <code>/remove ID</code>\nДоступные ID:\n" + ", ".join(self.store.profiles.keys())
                self.telegram.send_message(chat_id, msg)

        elif text.startswith("/remove "):
            p_id = text.split(maxsplit=1)[1].strip()
            if p_id in self.store.profiles:
                del self.store.profiles[p_id]
                self.store.save()
                self.telegram.send_message(chat_id, f"✅ Профиль {p_id} удален.")
            else:
                self.telegram.send_message(chat_id, "❌ Профиль с таким ID не найден.")

        # РАЗДЕЛ ТРЕКЕРА АНИМЕ
        elif text in ["🎬 Аниме трекер", "/anime"]:
            sub_list = []
            for t, d in self.store.anime_subs.items():
                sub_list.append(f"• <b>{html.escape(t)}</b> | Серия: <b>{d.get('last_ep', 0)}</b> | Озвучка: <i>{html.escape(d.get('voice', 'Любая'))}</i>")
            msg = (
                "🎬 <b>Трекер выхода серий и озвучек:</b>\n\n"
                + ("\n".join(sub_list) if sub_list else "Список пуст.\n")
                + "\n\nКоманды:\n"
                "➕ <code>/add_anime Название | Озвучка</code> — добавить тайтл\n"
                "🗑 <code>/del_anime Название</code> — удалить тайтл"
            )
            self.telegram.send_message(chat_id, msg)

        elif text.startswith("/add_anime "):
            raw_arg = text[11:].strip()
            parts = [p.strip() for p in raw_arg.split("|")]
            t_name = parts[0]
            v_name = parts[1] if len(parts) > 1 else "Любая"
            self.store.anime_subs[t_name] = {"title": t_name, "voice": v_name, "last_ep": 0, "users": [uid]}
            self.store.save()
            self.telegram.send_message(chat_id, f"✅ Аниме <b>{html.escape(t_name)}</b> добавлено в мониторинг (Озвучка: {html.escape(v_name)})!")
            threading.Thread(target=self.check_anime_updates, daemon=True).start()

        elif text.startswith("/del_anime "):
            t_name = text[11:].strip()
            if t_name in self.store.anime_subs:
                del self.store.anime_subs[t_name]
                self.store.save()
                self.telegram.send_message(chat_id, f"✅ Аниме <b>{html.escape(t_name)}</b> удалено из трекера.")
            else:
                self.telegram.send_message(chat_id, "❌ Тайтл не найден.")

        # РАЗДЕЛ ЧАТА ДРУЗЕЙ
        elif text in ["💬 Чат друзей", "/chat"]:
            self.user_states[uid] = "chat"
            self.telegram.send_message(
                chat_id,
                "💬 <b>Вы вошли в чат друзей!</b>\n"
                "Все ваши последующие сообщения будут отправляться всем добавленным друзьям.\n"
                "Нажмите «🚪 Выйти из чата», когда закончите.",
                reply_markup=CHAT_KEYBOARD
            )

        elif text in ["📈 История", "/history"]:
            self.telegram.send_message(chat_id, self.export_history(chat_id))

        elif text in ["⚙️ Настройки", "🔕 Уведомления"]:
            s = self.store.settings
            s.notify_on_change = not s.notify_on_change
            self.store.save()
            st = "ВКЛЮЧЕНЫ 🔔" if s.notify_on_change else "ВЫКЛЮЧЕНЫ 🔕"
            self.telegram.send_message(chat_id, f"Уведомления об изменениях: <b>{st}</b>")

        elif text in ["⏸ Пауза"]:
            self.paused = True
            self.telegram.send_message(chat_id, "⏸ Мониторинг поставлен на паузу.")

        elif text in ["▶️ Продолжить"]:
            self.paused = False
            self.telegram.send_message(chat_id, "▶️ Мониторинг возобновлен.")

        elif text in ["/cancel", "отмена"]:
            self.user_states.pop(uid, None)
            self.telegram.send_message(chat_id, "Действие отменено.", reply_markup=TELEGRAM_KEYBOARD)

    # ФОНОВЫЙ ЦИКЛ ОПРОСА TELEGRAM И ПРОВЕРОК
    def run(self) -> None:
        logger.info("Запуск Telegram Polling и планировщика...")
        last_check_time = 0.0
        last_anime_check = 0.0
        
        # Первичная генерация страницы
        self.generate_html_report()

        while not self.stop_event.is_set():
            now = time.time()
            # 1. Фоновая проверка профилей AniVox по таймеру
            if not self.paused and (now - last_check_time >= self.store.settings.interval_minutes * 60):
                last_check_time = now
                threading.Thread(target=self.check_all, args=(True,), daemon=True).start()

            # 2. Фоновая проверка выхода новых серий раз в 10 минут
            if now - last_anime_check >= 600:
                last_anime_check = now
                threading.Thread(target=self.check_anime_updates, daemon=True).start()

            # 3. Прием команд из Telegram
            try:
                updates = self.telegram.get_updates(timeout=2)
                for update in updates:
                    if "message" in update:
                        self.handle_message(update["message"])
            except Exception as e:
                logger.debug("Telegram Polling exception: %s", e)
                time.sleep(1)

            time.sleep(0.5)

def main():
    store = Store(CONFIG_PATH)
    if not store.token:
        logger.error("ОШИБКА: Токен бота не задан в конфиге или BOT_TOKEN!")
        sys.exit(1)

    # 1. Запуск веб-сервера в отдельном потоке (для Render)
    web_thread = threading.Thread(target=run_web_server, daemon=True)
    web_thread.start()

    # 2. Запуск логики бота
    telegram = TelegramClient(store.token)
    monitor = Monitor(store, telegram)

    def sig_handler(sig, frame):
        monitor.stop()
        sys.exit(0)

    signal.signal(signal.SIGINT, sig_handler)
    signal.signal(signal.SIGTERM, sig_handler)

    monitor.run()

if __name__ == "__main__":
    main()
