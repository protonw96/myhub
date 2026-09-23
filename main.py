#!/usr/bin/env python3
"""AniVox Monitor v4.5.0 (Anti-Freeze Architecture & Direct Anime ID Links)."""

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
VERSION = "4.5.0"
CONFIG_PATH = Path(__file__).with_name("anivox_monitor.json")
LOG_PATH = Path(__file__).with_name("anivox_monitor.log")
PROFILE_RE = re.compile(
    r"^https?://(?:www\.)?anivox\.fun/profile/([0-9]+)(?:[/?#].*)?$",
    re.IGNORECASE,
)
ALLOWED_INTERVALS = (1, 5, 10, 15, 20, 25, 30)
DEFAULT_INTERVAL = 10
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Linux; Android 10; Mobile) AppleWebKit/537.36 "
    "Chrome/124.0 Mobile Safari/537.36 AniVoxMonitor/4.5"
)

logger = logging.getLogger(APP_NAME)

TELEGRAM_KEYBOARD = {
    "keyboard": [
        ["📊 Проверить всех", "🌐 Открыть сайт-отчет"],
        ["👥 Мои профили", "➕ Добавить", "🗑 Удалить"],
        ["📈 История", "🎭 Друзья"],
        ["⚙️ Настройки", "🔕 Уведомления"],
        ["⏸ Пауза", "▶️ Продолжить"]
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
        raise ValueError(
            "Нужна ссылка вида https://anivox.fun/profile/27788"
        )
    return normalized, match.group(1)

def parse_bool(value: Any, default: bool = False) -> bool:
    return as_bool(value, default)

def fetch_anime_info(title: str) -> dict[str, Any]:
    """Запрашивает жанры и ID аниме через открытый API Shikimori (без зависаний)."""
    if not title or title.lower() in ["неизвестно", "тайтл"]:
        return {"genres": [], "id": ""}
    try:
        time.sleep(1) # Защита от бана по IP от Shikimori
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
    """Фоновая функция для поиска жанров и ID без лагов бота."""
    if title in anime_cache and isinstance(anime_cache[title], dict) and anime_cache[title].get("id"):
        return
        
    info = fetch_anime_info(title)
    anime_cache[title] = info
    raw = load_json(store_path)
    raw["anime_cache"] = anime_cache
    save_json(store_path, raw)

def get_anime_data(title: str, cache: dict) -> tuple[str, str]:
    """Возвращает (строка_жанров, прямая_ссылка_anivox_через_id)"""
    cached = cache.get(title)
    genres_str = "-"
    link = "https://anivox.fun" # Резервная ссылка, если ничего не найдено
    
    if isinstance(cached, dict):
        genres_str = ", ".join(cached.get("genres", [])) or "-"
        if cached.get("id"):
            link = f"https://anivox.fun/anime/{cached['id']}"
    elif isinstance(cached, list):
        genres_str = ", ".join(cached) or "-"
        
    return genres_str, link

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
        self.token = str(raw.get("token", "")).strip()
        self.chat_id = str(raw.get("chat_id", "")).strip()
        
        self.allowed_users: list[str] = raw.get("allowed_users", [])
        if self.chat_id and self.chat_id not in self.allowed_users:
            self.allowed_users.append(self.chat_id)
            
        self.anime_cache: dict[str, Any] = raw.get("anime_cache", {})

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
                except (KeyError, TypeError):
                    continue
                self.profiles[profile.profile_id] = profile
                
        self.history: dict[str, list[dict[str, Any]]] = {}
        raw_history = raw.get("history", {})
        if isinstance(raw_history, dict):
            for profile_id, entries in raw_history.items():
                if isinstance(entries, list):
                    cleaned_entries = []
                    for item in entries:
                        if not isinstance(item, dict):
                            continue
                        if not cleaned_entries:
                            cleaned_entries.append(item)
                        else:
                            last_item = cleaned_entries[-1]
                            if (last_item.get("presence") != item.get("presence") or
                                last_item.get("nickname") != item.get("nickname") or
                                last_item.get("level") != item.get("level") or
                                last_item.get("title") != item.get("title")):
                                cleaned_entries.append(item)
                    self.history[str(profile_id)] = cleaned_entries[-500:]

    def save(self) -> None:
        save_json(
            self.path,
            {
                "token": self.token,
                "chat_id": self.chat_id,
                "allowed_users": self.allowed_users,
                "anime_cache": self.anime_cache,
                "settings": asdict(self.settings),
                "profiles": [asdict(p) for p in self.profiles.values()],
                "history": self.history,
            },
        )

    def add_history(
        self,
        profile_id: str,
        result: "FetchResult",
        changes: list[str],
    ) -> None:
        if not result.ok:
            return
            
        if not changes:
            return

        entries = self.history.setdefault(profile_id, [])
        if entries:
            last_entry = entries[-1]
            if (last_entry.get("presence") == result.presence and 
                last_entry.get("nickname") == result.nickname and 
                last_entry.get("level") == result.level and
                last_entry.get("title") == result.title):
                return

        # Запускаем фоновый поиск ID, если тайтла нет в кэше
        if result.title:
            cached = self.anime_cache.get(result.title)
            if not cached or (isinstance(cached, dict) and not cached.get("id")) or isinstance(cached, list):
                if not isinstance(cached, dict):
                    self.anime_cache[result.title] = {"genres": [], "id": ""}
                threading.Thread(target=background_fetch_info, args=(result.title, self.anime_cache, self.path), daemon=True).start()

        entries.append(
            {
                "checked_at": result.checked_at,
                "ok": result.ok,
                "nickname": result.nickname,
                "level": result.level,
                "presence": result.presence,
                "title": result.title,
                "signature": result.signature,
                "changes": changes,
                "error": result.error,
            }
        )
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
        except (urllib.error.URLError, TimeoutError, OSError, ConnectionError) as exc:
            raise TelegramError(f"Network error (Telegram): {exc}") from exc
            
        if not result.get("ok"):
            raise TelegramError(str(result.get("description", "ошибка Telegram")))
        return result.get("result")

    def get_me(self) -> dict[str, Any]:
        result = self.request("getMe")
        return result if isinstance(result, dict) else {}

    def send_message(
        self, chat_id: str, text: str, reply_markup: Optional[dict[str, Any]] = None
    ) -> None:
        self.request(
            "sendMessage",
            {
                "chat_id": chat_id,
                "text": text[:4096],
                "parse_mode": "HTML",
                "disable_web_page_preview": "true",
                "reply_markup": json.dumps(
                    reply_markup or TELEGRAM_KEYBOARD, ensure_ascii=False
                ),
            },
        )

    def edit_message_text(
        self, chat_id: str, text: str, message_id: int, reply_markup: Optional[dict[str, Any]] = None
    ) -> None:
        self.request(
            "editMessageText",
            {
                "chat_id": chat_id,
                "message_id": message_id,
                "text": text[:4096],
                "parse_mode": "HTML",
                "reply_markup": json.dumps(reply_markup, ensure_ascii=False) if reply_markup else ""
            },
        )

    def answer_callback_query(self, callback_query_id: str, text: str = "") -> None:
        try:
            self.request("answerCallbackQuery", {"callback_query_id": callback_query_id, "text": text})
        except Exception:
            pass

    def send_document(self, chat_id: str, path: Path, caption: str = "") -> None:
        boundary = f"----AniVoxMonitor{os.urandom(8).hex()}"
        file_bytes = path.read_bytes()
        parts: list[bytes] = []

        def field(name: str, value: str) -> None:
            parts.extend(
                [
                    f"--{boundary}\r\n".encode(),
                    f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode(),
                    value.encode("utf-8"),
                    b"\r\n",
                ]
            )

        field("chat_id", chat_id)
        if caption:
            field("caption", caption[:1024])
            
        content_type = b"Content-Type: text/html\r\n\r\n" if path.name.endswith(".html") else b"Content-Type: application/json\r\n\r\n"
        
        parts.extend(
            [
                f"--{boundary}\r\n".encode(),
                (
                    'Content-Disposition: form-data; name="document"; '
                    f'filename="{path.name}"\r\n'
                ).encode(),
                content_type,
                file_bytes,
                b"\r\n",
                f"--{boundary}--\r\n".encode(),
            ]
        )
        request = urllib.request.Request(
            self.base_url + "sendDocument",
            data=b"".join(parts),
            headers={
                "Content-Type": f"multipart/form-data; boundary={boundary}",
                "User-Agent": f"{APP_NAME}/{VERSION}",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                result = json.loads(response.read().decode("utf-8"))
        except (urllib.error.URLError, urllib.error.HTTPError, OSError) as exc:
            err_details = exc.read().decode() if isinstance(exc, urllib.error.HTTPError) else str(exc)
            raise TelegramError(f"не удалось отправить файл: {err_details}") from exc
        if not result.get("ok"):
            raise TelegramError(str(result.get("description", "ошибка отправки файла")))

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
        return "|".join(
            (self.nickname.lower(), self.level, state, self.title.lower())
        ).lower()

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
        if opcode == 9:  # ping
            pong = bytes((0x8A, len(data))) + data
            sock.sendall(pong)
            continue
        if opcode == 10:
            continue
        if opcode in (1, 0):
            fragments.append(data)
            if header[0] & 0x80:
                return b"".join(fragments).decode("utf-8", errors="replace")

def anivox_websocket_profile(
    profile_id: str, timeout: int = 20
) -> dict[str, Any]:
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
                raise ConnectionError("слишком длинный WebSocket handshake")
        header_text = response.decode("latin1", errors="replace")
        status_line = header_text.splitlines()[0] if header_text else ""
        if " 101 " not in status_line:
            raise ConnectionError(f"AniVox WebSocket: {status_line}")
        ws_send_text(sock, {"type": "get_profile", "id": int(profile_id)})
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            message = ws_read_message(sock)
            if not message:
                continue
            try:
                data = json.loads(message)
            except json.JSONDecodeError:
                continue
            if data.get("type") == "get_profile":
                return data
        raise TimeoutError("AniVox не прислал ответ get_profile")
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
    return f"Был в сети: {relative} ({dt.strftime('%H:%M Алматы')})"

def result_from_ws(data: dict[str, Any], profile_id: str, url: str) -> FetchResult:
    if not isinstance(data, dict):
        return FetchResult(
            False, profile_id, url,
            error="AniVox ответил некорректным форматом данных",
        )
    container = data.get("data")
    user = container.get("user") if isinstance(container, dict) else None
    if not isinstance(user, dict):
        return FetchResult(
            False, profile_id, url,
            error="AniVox ответил без объекта data.user",
        )
    online_value = user.get("online", 0)
    try:
        online_timestamp = float(online_value or 0)
    except (TypeError, ValueError):
        online_timestamp = 0
    online_status = container.get("online_status", "")
    if online_timestamp == 0:
        title = ""
        if isinstance(online_status, dict):
            title = compact(online_status.get("text", ""))
        else:
            title = compact(online_status)
        title = re.sub(
            r"^(?:смотрит|watching)\s*[:：-]?\s*",
            "",
            title,
            flags=re.IGNORECASE,
        ).strip()
        presence = f"Смотрит: {title}" if title else "Онлайн"
    else:
        title = ""
        presence = format_last_seen(online_timestamp)
    return FetchResult(
        True,
        profile_id,
        url,
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
    except (ConnectionError, OSError, TimeoutError, ValueError, json.JSONDecodeError) as exc:
        return FetchResult(
            False,
            profile.profile_id,
            profile.url,
            error=f"не удалось получить данные через AniVox WebSocket: {exc}",
        )

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
    except:
        dt = result.checked_at
        
    lines.extend([
        f"<i>⏱ Проверено: {dt} (Алматы)</i>",
        f"<a href='{result.url}'>🌐 Открыть профиль</a>"
    ])
    return "\n".join(lines)

def calc_stats(entries: list[dict], anime_cache: dict) -> tuple[float, float, float, dict, dict]:
    w_time = 0.0
    on_time = 0.0
    off_time = 0.0
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
                
                cached = anime_cache.get(t)
                g_list = cached.get("genres", []) if isinstance(cached, dict) else (cached if isinstance(cached, list) else [])
                for genre in g_list:
                    genres_time[genre] = genres_time.get(genre, 0) + dur_hours
                    
            elif "Онлайн" in pres:
                on_time += dur_hours
            else:
                off_time += dur_hours
        except (ValueError, TypeError):
            continue
            
    return round(w_time, 1), round(on_time, 1), round(off_time, 1), titles, genres_time

def changed_fields(old: str, result: FetchResult) -> list[str]:
    if not old:
        return ["новый профиль"]
    previous = old.split("|")
    current = result.signature.split("|")
    previous += [""] * (4 - len(previous))
    current += [""] * (4 - len(current))
    changes: list[str] = []
    if previous[0] != current[0]:
        changes.append("ник")
    if previous[1] != current[1]:
        changes.append("уровень")
    old_state, new_state = previous[2], current[2]
    if old_state != new_state:
        changes.append("статус")
    if (
        old_state.startswith("watching:")
        and new_state.startswith("watching:")
        and previous[3] != current[3]
    ):
        changes.append("тайтл")
    return changes

class QuietHTTPRequestHandler(http.server.SimpleHTTPRequestHandler):
    """Скрывает назойливые логи локального сервера."""
    def log_message(self, format, *args):
        pass

def run_web_server():
    """Запускает веб-сервер в директории со скриптом."""
    port = int(os.getenv("PORT", 15887))
    work_dir = os.path.dirname(os.path.abspath(CONFIG_PATH))
    os.chdir(work_dir)
    try:
        socketserver.TCPServer.allow_reuse_address = True
        with socketserver.TCPServer(("", port), QuietHTTPRequestHandler) as httpd:
            logger.info(f"Web-сервер запущен на порту {port}")
            httpd.serve_forever()
    except Exception as e:
        logger.error(f"Ошибка запуска Web-сервера: {e}")

class Monitor:
    def __init__(self, store: Store, telegram: TelegramClient) -> None:
        self.store = store
        self.telegram = telegram
        self.stop_event = threading.Event()
        self.paused = False
        self.check_lock = threading.RLock()
        self.check_requested = threading.Event()

    def stop(self, *_args: Any) -> None:
        self.stop_event.set()

    def allowed_notification(self, old: str, result: FetchResult) -> bool:
        settings = self.store.settings
        fields = changed_fields(old, result)
        if not old or not settings.notify_on_change:
            return bool(not old and settings.notify_on_change)
        if "статус" in fields:
            online = result.presence.startswith(("Онлайн", "Смотрит"))
            if online and not settings.notify_on_online:
                fields.remove("статус")
            if not online and not settings.notify_on_offline:
                fields.remove("статус")
        if "тайтл" in fields and not settings.notify_on_title:
            fields.remove("тайтл")
        return bool(fields)

    def check_one(self, profile: Profile, announce: bool) -> FetchResult:
        # АРХИТЕКТУРА АНТИ-ФРИЗ: Сетевой запрос выполняется ВНЕ блокировки
        result = fetch_profile(profile, self.store.settings.request_timeout)
        
        # Только обновление внутренних данных защищено потоковым замком
        with self.check_lock:
            profile.last_check = result.checked_at
            old_signature = profile.last_signature
            changes = changed_fields(old_signature, result)
            profile.last_ok = result.ok
            profile.last_error = result.error
            self.store.add_history(profile.profile_id, result, changes)
            
            if result.ok:
                if announce and self.allowed_notification(old_signature, result):
                    prefix = ""
                    if old_signature:
                        prefix = "<b>🔔 Изменение:</b> " + ", ".join(changes) + "\n\n"
                    msg_text = prefix + format_profile(result, self.store.anime_cache, profile.label)
                    
                    for uid in self.store.allowed_users:
                        try:
                            self.telegram.send_message(uid, msg_text)
                        except TelegramError as exc:
                            logger.warning("Telegram notification failed for %s: %s", uid, exc)
                            
                profile.last_signature = result.signature
                profile.last_status = result.presence
                
            elif announce and self.store.settings.notify_on_errors:
                err_msg = f"⚠️ <b>Ошибка проверки {html.escape(profile.url)}</b>\n{html.escape(result.error)}"
                for uid in self.store.allowed_users:
                    try:
                        self.telegram.send_message(uid, err_msg)
                    except TelegramError as exc:
                        logger.warning("Telegram error notification failed for %s: %s", uid, exc)
            self.store.save()
            return result

    def check_all(self, announce: bool = True) -> list[FetchResult]:
        results = []
        for profile in list(self.store.profiles.values()):
            if self.stop_event.is_set():
                break
            results.append(self.check_one(profile, announce))
        return results

    def history_text(self, args: str) -> str:
        parts = args.split()
        profile_id = parts[0] if parts and parts[0].isdigit() else ""
        try:
            limit = int(parts[1] if profile_id and len(parts) > 1 else parts[0]) if parts else 15
        except ValueError:
            limit = 15
        limit = max(1, min(30, limit))
        profile_ids = [profile_id] if profile_id else list(self.store.profiles)
        blocks: list[str] = []
        
        p_idx = 1
        for current_id in profile_ids:
            profile = self.store.profiles.get(current_id)
            entries = self.store.history.get(current_id, [])
            
            title = profile.label if profile and profile.label else current_id
            
            try:
                last_chk_str = datetime.fromisoformat(profile.last_check).strftime("%d.%m %H:%M:%S") if profile.last_check else "никогда"
            except:
                last_chk_str = profile.last_check if profile.last_check else "никогда"
                
            rows = [
                f"👤 <b>{p_idx}. {html.escape(title)}</b> (ID {current_id})",
                f"🔄 <i>Автопроверка: {html.escape(last_chk_str)}</i>"
            ]
            
            if not entries:
                rows.append("<i>История пуста</i>")
            else:
                hist_idx = 1
                for entry in entries[-limit:][::-1]:
                    status = entry.get("presence", "неизвестно")
                    try:
                        dt_str = datetime.fromisoformat(entry.get("checked_at", "")).strftime("%d.%m %H:%M")
                    except:
                        dt_str = entry.get("checked_at", "?")
                        
                    rows.append(f"<b>{hist_idx}.</b> {dt_str} — {html.escape(status)}")
                    hist_idx += 1
                    
            blocks.append("\n".join(rows))
            p_idx += 1
            
        return (
            "\n\n".join(blocks)[:4000]
            if blocks
            else "История пока пуста. Записываются только изменения."
        )

    def summary_text(self) -> str:
        if not self.store.profiles:
            return "Профилей нет. Владелец должен добавить профиль через /add."
        counts = {"онлайн": 0, "смотрит": 0, "оффлайн": 0, "ошибка": 0}
        rows = ["🧾 <b>Сводка по пользователям:</b>"]
        
        idx = 1
        for profile in self.store.profiles.values():
            status = profile.last_status or "не проверялся"
            status_lower = status.lower()
            if not profile.last_ok and profile.last_check:
                counts["ошибка"] += 1
            elif status_lower.startswith("смотрит"):
                counts["смотрит"] += 1
            elif status_lower.startswith("онлайн"):
                counts["онлайн"] += 1
            elif profile.last_check:
                counts["оффлайн"] += 1
            history_count = len(self.store.history.get(profile.profile_id, []))
            
            try:
                dt_str = datetime.fromisoformat(profile.last_check).strftime("%d.%m %H:%M") if profile.last_check else "нет"
            except:
                dt_str = profile.last_check
                
            rows.append(
                f"<b>{idx}.</b> {html.escape(profile.label or profile.profile_id)}: {html.escape(status)}\n"
                f"  изменений: {history_count}, последнее: {dt_str}"
            )
            idx += 1
            
        rows.append(
            "\nИтого: "
            f"🟢 онлайн {counts['онлайн']} | "
            f"🍿 смотрит {counts['смотрит']} | "
            f"🔴 оффлайн {counts['оффлайн']} | "
            f"⚠️ ошибки {counts['ошибка']}"
        )
        return "\n".join(rows)[:4000]

    def search_text(self, query: str) -> str:
        needle = query.strip().lower()
        if not needle:
            return "Пример: /find текст или нажмите 🔍 Поиск."
        found: list[str] = []
        for profile in self.store.profiles.values():
            entries = self.store.history.get(profile.profile_id, [])
            latest = entries[-1] if entries else {}
            haystack = " ".join(
                [
                    profile.profile_id,
                    profile.label,
                    str(latest.get("nickname", "")),
                    str(latest.get("title", "")),
                ]
            ).lower()
            if needle in haystack:
                found.append(
                    f"• {html.escape(latest.get('nickname', profile.label or profile.profile_id))} "
                    f"(ID {profile.profile_id}) — {html.escape(latest.get('presence', 'не проверялся'))}"
                )
        return "\n".join(["Найдено:"] + found) if found else "Ничего не найдено."

    def probe_text(self) -> str:
        if self.store.profiles:
            profile = next(iter(self.store.profiles.values()))
        else:
            profile = Profile("https://anivox.fun/profile/27788", "27788")
        started = time.monotonic()
        result = fetch_profile(profile, self.store.settings.request_timeout)
        elapsed = time.monotonic() - started
        if result.ok:
            return (
                f"✅ AniVox доступен, WebSocket /api работает.\n"
                f"Ответ за {elapsed:.1f} сек.\n{format_profile(result, self.store.anime_cache)}"
            )
        return f"❌ AniVox не ответил за {elapsed:.1f} сек.\n{html.escape(result.error)}"

    def export_history(self, chat_id: str) -> str:
        path = CONFIG_PATH.with_name("anivox_history_export.json")
        payload = {
            "exported_at": almaty_now(),
            "profiles": [asdict(profile) for profile in self.store.profiles.values()],
            "history": self.store.history,
        }
        save_json(path, payload)
        try:
            self.telegram.send_document(chat_id, path, "История AniVox (JSON)")
            return "Экспорт истории отправлен файлом."
        except TelegramError as exc:
            return f"Не удалось отправить экспорт: {exc}"

    def gen
