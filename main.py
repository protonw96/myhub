import base64
import html
import json
import os
import random
import re
import secrets
import sys
import threading
import time
from flask import Flask, jsonify
import requests
import rsa
import telebot
from telebot.types import InlineKeyboardButton, InlineKeyboardMarkup

# =====================================================================
# ВСТАВЬТЕ СЮДА ТОКЕН ВАШЕГО TELEGRAM-БОТА ОТ @BotFather
# =====================================================================
TG_BOT_TOKEN = os.environ.get("TG_BOT_TOKEN", "8721976140:AAEHfoi-6aZ5S9Y0alYz_d0hKz_yUz26Ee0")

# Ваша ссылка Render уже встроена по умолчанию:
DEFAULT_RENDER_URL = "https://myhub-yhxu.onrender.com"
# =====================================================================

SESSION_FILE = "session.json"
CLAIMED_FILE = "claimed_games.json"
CHECK_GIVEAWAYS_INTERVAL = 10 * 3600  # Проверка бесплатных раздач каждые 10 часов
CHECK_DISCOUNTS_INTERVAL = 2 * 3600   # Проверка скидок по списку желаемого каждые 2 часа

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

bot_status = {
    "start_time": time.time(),
    "last_ping_ts": 0,
    "last_ping_time": "Еще не выполнялся",
    "last_ping_url": f"{DEFAULT_RENDER_URL}/ping",
    "last_ping_code": "—",
    "ping_count": 0,
    "ping_history": [],  # Хранит последние 10 пингов (тихий лог)
    "last_check": "Еще не выполнялась",
    "last_discount_check": "Еще не выполнялась",
    "steam_ok": False,
    "claimed_count": 0,
}

pending_auth = {}
library_cache = {}  # Кэш списка игр для удаления


# ----------------- РАБОТА С СЕССИЕЙ И БАЗОЙ -----------------
def load_session():
    env_b64 = os.environ.get("SESSION_BASE64")
    if env_b64:
        try:
            raw = base64.b64decode(env_b64.strip()).decode("utf-8")
            data = json.loads(raw)
            with open(SESSION_FILE, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
            return data
        except Exception as e:
            print(f"[!] Ошибка чтения SESSION_BASE64: {e}")

    if os.path.exists(SESSION_FILE):
        try:
            with open(SESSION_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def save_session(data):
    with open(SESSION_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def export_session_b64(data):
    raw = json.dumps(data, ensure_ascii=False)
    return base64.b64encode(raw.encode("utf-8")).decode("utf-8")


def load_claimed():
    if os.path.exists(CLAIMED_FILE):
        try:
            with open(CLAIMED_FILE, "r", encoding="utf-8") as f:
                return set(json.load(f))
        except Exception:
            return set()
    return set()


def save_claimed(claimed_set):
    with open(CLAIMED_FILE, "w", encoding="utf-8") as f:
        json.dump(list(claimed_set), f, indent=2, ensure_ascii=False)


# ----------------- КЛИЕНТ STEAM -----------------
class SteamClient:
    def __init__(self):
        self.data = load_session()
        if "session_id" not in self.data:
            self.data["session_id"] = secrets.token_hex(12)
        if "render_url" not in self.data:
            self.data["render_url"] = DEFAULT_RENDER_URL
        if "watchlist" not in self.data:
            self.data["watchlist"] = {}  # {appid: {"name": str, "last_discount": int}}

    @property
    def session_id(self):
        return self.data["session_id"]

    def is_configured(self):
        return bool(self.data.get("refresh_token") and self.data.get("steamid"))

    def atomic_login(self, username, password, guard_code):
        """Мгновенный пакетный вход для защиты от гео-блокировки."""
        try:
            rsa_url = "https://api.steampowered.com/IAuthenticationService/GetPasswordRSAPublicKey/v1/"
            r = requests.get(
                rsa_url,
                params={"account_name": username},
                headers={"User-Agent": USER_AGENT},
                timeout=15,
            ).json()
            resp = r.get("response", {})
            if not resp.get("publickey_mod"):
                return {"ok": False, "error": "Не удалось получить RSA-ключ Steam."}

            mod = int(resp["publickey_mod"], 16)
            exp = int(resp["publickey_exp"], 16)
            timestamp = resp["timestamp"]

            pub_key = rsa.PublicKey(mod, exp)
            encrypted_pw = base64.b64encode(
                rsa.encrypt(password.encode("utf-8"), pub_key)
            ).decode("utf-8")

            begin_url = "https://api.steampowered.com/IAuthenticationService/BeginAuthSessionViaCredentials/v1/"
            begin_payload = {
                "account_name": username,
                "encrypted_password": encrypted_pw,
                "encryption_timestamp": timestamp,
                "remember_login": "true",
                "platform_type": "2",
                "website_id": "Store",
                "persistence": "1",
                "device_friendly_name": "Chrome (Windows)",
            }
            b_res = (
                requests.post(
                    begin_url,
                    data=begin_payload,
                    headers={"User-Agent": USER_AGENT},
                    timeout=15,
                )
                .json()
                .get("response", {})
            )

            client_id = b_res.get("client_id")
            request_id = b_res.get("request_id")
            steamid = b_res.get("steamid")

            if not client_id or not steamid:
                return {"ok": False, "error": "Неверный логин или пароль."}

            confirmations = b_res.get("allowed_confirmations", [])
            conf_types = [c.get("confirmation_type") for c in confirmations]
            code_type = 3 if 3 in conf_types else (2 if 2 in conf_types else 3)

            guard_url = "https://api.steampowered.com/IAuthenticationService/UpdateAuthSessionWithSteamGuardCode/v1/"
            requests.post(
                guard_url,
                data={
                    "client_id": client_id,
                    "steamid": steamid,
                    "code": guard_code.strip().upper(),
                    "code_type": code_type,
                },
                headers={"User-Agent": USER_AGENT},
                timeout=15,
            )

            poll_url = "https://api.steampowered.com/IAuthenticationService/PollAuthSessionStatus/v1/"
            for _ in range(5):
                p_res = (
                    requests.post(
                        poll_url,
                        data={"client_id": client_id, "request_id": request_id},
                        headers={"User-Agent": USER_AGENT},
                        timeout=15,
                    )
                    .json()
                    .get("response", {})
                )

                if p_res.get("refresh_token") and p_res.get("access_token"):
                    self.data.update({
                        "steamid": str(steamid),
                        "account_name": username,
                        "refresh_token": p_res["refresh_token"],
                        "access_token": p_res["access_token"],
                        "session_id": secrets.token_hex(12),
                    })
                    save_session(self.data)
                    bot_status["steam_ok"] = True
                    return {"ok": True}
                time.sleep(1)

            return {
                "ok": False,
                "error": "Код отклонен или IP заблокирован. Используйте импорт SESSION_BASE64.",
            }
        except Exception as e:
            return {"ok": False, "error": f"Ошибка: {e}"}

    def is_token_expired(self):
        token = self.data.get("access_token", "")
        if not token:
            return True
        try:
            payload_part = token.split(".")[1]
            padding = "=" * ((4 - len(payload_part) % 4) % 4)
            decoded = json.loads(
                base64.urlsafe_b64decode(payload_part + padding).decode("utf-8")
            )
            return time.time() > (decoded.get("exp", 0) - 3600)
        except Exception:
            return True

    def refresh_access_token(self):
        if not self.is_configured():
            return False
        url = "https://api.steampowered.com/IAuthenticationService/GenerateAccessTokenForApp/v1/"
        payload = {
            "refresh_token": self.data["refresh_token"],
            "steamid": self.data["steamid"],
            "renewal_type": 0,
        }
        try:
            r = requests.post(url, data=payload, timeout=15).json().get("response", {})
            if r.get("access_token"):
                self.data["access_token"] = r["access_token"]
                save_session(self.data)
                return True
        except Exception:
            pass
        return False

    def get_cookies(self):
        if self.is_token_expired():
            self.refresh_access_token()
        return {
            "sessionid": self.session_id,
            "steamLoginSecure": f"{self.data.get('steamid', '')}%7C%7C{self.data.get('access_token', '')}",
            "birthtime": "283993201",
            "lastagecheckage": "1-0-1979",
        }

    def check_health(self):
        if not self.is_configured():
            bot_status["steam_ok"] = False
            return False
        try:
            r = requests.get(
                "https://store.steampowered.com/account/",
                cookies=self.get_cookies(),
                headers={"User-Agent": USER_AGENT},
                allow_redirects=False,
                timeout=15,
            )
            ok = r.status_code == 200
            bot_status["steam_ok"] = ok
            return ok
        except Exception:
            bot_status["steam_ok"] = False
            return False

    def update_bio(self, new_summary):
        steamid = self.data["steamid"]
        cookies = self.get_cookies()
        headers = {
            "User-Agent": USER_AGENT,
            "Referer": f"https://steamcommunity.com/profiles/{steamid}/edit/info",
        }

        persona_name = self.data.get("account_name", "Player")
        real_name = ""
        custom_url = ""

        try:
            info_resp = requests.get(
                f"https://steamcommunity.com/profiles/{steamid}/edit/info",
                cookies=cookies,
                headers=headers,
                timeout=15,
            )
            match = re.search(r'data-profile-edit="([^"]+)"', info_resp.text)
            if match:
                profile_json = json.loads(html.unescape(match.group(1)))
                persona_name = profile_json.get("strPersonaName", persona_name)
                real_name = profile_json.get("strRealName", "")
                custom_url = profile_json.get("strCustomURL", "")
        except Exception:
            pass

        post_url = f"https://steamcommunity.com/profiles/{steamid}/edit/"
        payload = {
            "sessionID": self.session_id,
            "type": "profileSave",
            "personaName": persona_name,
            "real_name": real_name,
            "customURL": custom_url,
            "summary": new_summary,
            "json": "1",
        }

        r = requests.post(
            post_url, data=payload, cookies=cookies, headers=headers, timeout=15
        )
        return r.status_code == 200

    def get_subid_from_appid(self, app_id):
        try:
            page = requests.get(
                f"https://store.steampowered.com/app/{app_id}/",
                cookies=self.get_cookies(),
                headers={"User-Agent": USER_AGENT},
                timeout=15,
            ).text
            match = re.search(r"addFreeLicense\(\s*(\d+)", page)
            if match:
                return match.group(1)
            sub_match = re.search(r'name="subid"\s+value="(\d+)"', page)
            if sub_match:
                return sub_match.group(1)
        except Exception:
            pass

        try:
            api_url = f"https://store.steampowered.com/api/appdetails?appids={app_id}&cc=kz&l=ru"
            data = requests.get(api_url, timeout=15).json().get(str(app_id), {})
            if data.get("success"):
                packages = data.get("data", {}).get("packages", [])
                if packages:
                    return str(packages[0])
        except Exception:
            pass
        return None

    def add_free_license(self, sub_id):
        cookies = self.get_cookies()
        headers = {
            "User-Agent": USER_AGENT,
            "Referer": "https://store.steampowered.com/",
            "X-Requested-With": "XMLHttpRequest",
        }

        url_f2p = f"https://store.steampowered.com/freelicense/addfreelicense/{sub_id}"
        r1 = requests.post(
            url_f2p,
            data={"sessionid": self.session_id, "ajax": "true"},
            cookies=cookies,
            headers=headers,
            timeout=15,
        )

        url_checkout = "https://store.steampowered.com/checkout/addfreelicense"
        r2 = requests.post(
            url_checkout,
            data={
                "action": "add_to_cart",
                "sessionid": self.session_id,
                "subid": str(sub_id),
            },
            cookies=cookies,
            headers=headers,
            timeout=15,
        )
        return r1.status_code == 200 or r2.status_code == 200

    def get_removable_licenses(self):
        """Парсит список игр в библиотеке, которые можно удалить (Free / Раздачи)."""
        url = "https://store.steampowered.com/account/licenses/?l=russian"
        try:
            r = requests.get(
                url,
                cookies=self.get_cookies(),
                headers={"User-Agent": USER_AGENT},
                timeout=15,
            )
            if r.status_code != 200:
                return []

            items = []
            rows = re.findall(r"<tr[^>]*>(.*?)</tr>", r.text, re.DOTALL)
            for row in rows:
                m = re.search(r"RemoveFreeLicense\(\s*(\d+)\s*,\s*'([^']*)'", row)
                if m:
                    pkg_id = m.group(1)
                    raw_arg = m.group(2)
                    title = ""
                    try:
                        title = base64.b64decode(raw_arg).decode("utf-8")
                    except Exception:
                        title = raw_arg

                    if not title or not title.isprintable():
                        tds = re.findall(r"<td[^>]*>(.*?)</td>", row, re.DOTALL)
                        if len(tds) >= 2:
                            clean_td = re.sub(r"<div.*?</div>", "", tds[1], flags=re.DOTALL)
                            clean_td = re.sub(r"<[^>]+>", "", clean_td).strip()
                            title = html.unescape(clean_td)

                    title = title.strip() or f"Package {pkg_id}"
                    items.append({"packageid": pkg_id, "name": title[:45]})
            return items
        except Exception as e:
            print(f"[!] Ошибка загрузки лицензий: {e}")
            return []

    def remove_license(self, package_id):
        """Удаляет игру (бесплатную лицензию) из библиотеки Steam."""
        url = "https://store.steampowered.com/account/removelicense"
        headers = {
            "User-Agent": USER_AGENT,
            "Referer": "https://store.steampowered.com/account/licenses/",
            "X-Requested-With": "XMLHttpRequest",
        }
        payload = {
            "sessionid": self.session_id,
            "packageid": str(package_id),
        }
        try:
            r = requests.post(
                url,
                data=payload,
                cookies=self.get_cookies(),
                headers=headers,
                timeout=15,
            )
            if r.status_code == 200:
                return r.json().get("success") == 1
        except Exception as e:
            print(f"[!] Ошибка удаления пакета {package_id}: {e}")
        return False

    def get_game_price_info(self, app_id):
        """Получает название, цену в тенге (KZ) и процент скидки игры по AppID."""
        url = f"https://store.steampowered.com/api/appdetails?appids={app_id}&cc=kz&l=ru"
        try:
            r = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=15).json()
            app_data = r.get(str(app_id), {})
            if not app_data.get("success"):
                return None
            data = app_data.get("data", {})
            name = data.get("name", f"App {app_id}")
            is_free = data.get("is_free", False)
            price_ov = data.get("price_overview", {})

            if is_free:
                return {
                    "appid": str(app_id),
                    "name": name,
                    "is_free": True,
                    "discount": 100,
                    "final_formatted": "Бесплатно",
                    "initial_formatted": "0₸",
                }
            if not price_ov:
                return {
                    "appid": str(app_id),
                    "name": name,
                    "is_free": False,
                    "discount": 0,
                    "final_formatted": "Цена недоступна",
                    "initial_formatted": "—",
                }
            return {
                "appid": str(app_id),
                "name": name,
                "is_free": False,
                "discount": int(price_ov.get("discount_percent", 0)),
                "final_formatted": price_ov.get("final_formatted", ""),
                "initial_formatted": price_ov.get("initial_formatted", ""),
            }
        except Exception:
            return None


steam = SteamClient()
bot = telebot.TeleBot(TG_BOT_TOKEN)


def is_admin(user_id):
    saved_admin = steam.data.get("admin_id")
    if not saved_admin:
        steam.data["admin_id"] = user_id
        save_session(steam.data)
        return True
    return int(saved_admin) == int(user_id)


# ----------------- УДОБНОЕ КОМПАКТНОЕ МЕНЮ (2 КОЛОНКИ) -----------------
def get_main_keyboard():
    kb = InlineKeyboardMarkup(row_width=2)
    if not steam.is_configured():
        kb.add(
            InlineKeyboardButton("🔑 Войти в Steam", callback_data="login_steam"),
            InlineKeyboardButton("📥 Вставить сессию", callback_data="import_b64"),
        )
        kb.add(
            InlineKeyboardButton("📡 Статус и Пинг", callback_data="menu_ping"),
            InlineKeyboardButton("🔄 Обновить бота", callback_data="update_bot"),
        )
    else:
        kb.add(
            InlineKeyboardButton("📚 Библиотека", callback_data="menu_library"),
            InlineKeyboardButton("📉 Радар скидок", callback_data="menu_discounts"),
            InlineKeyboardButton("🎁 Раздачи", callback_data="menu_giveaways"),
            InlineKeyboardButton("👤 Профиль Steam", callback_data="menu_profile"),
            InlineKeyboardButton("📡 Статус и Пинг", callback_data="menu_ping"),
            InlineKeyboardButton("⚙️ Система / Обновить", callback_data="menu_system"),
        )
    return kb


def format_dashboard_text():
    if not steam.is_configured():
        return (
            "👋 **Панель управления Steam Bot**\n\n"
            "Бот еще не привязан к аккаунту Steam.\n"
            "Выберите способ авторизации ниже:"
        )
    ago = (
        f"{int(time.time() - bot_status['last_ping_ts'])} сек. назад"
        if bot_status["last_ping_ts"] > 0
        else "Ожидание первого пинга..."
    )
    watchlist_count = len(steam.data.get("watchlist", {}))
    return (
        f"🤖 **Главное меню Steam AutoClaim**\n\n"
        f"👤 Аккаунт: `{steam.data.get('account_name')}`\n"
        f"📉 Игр на радаре скидок: `{watchlist_count}`\n"
        f"🎁 Добавлено за сеанс: `{bot_status['claimed_count']}`\n"
        f"📡 Последний тихий пинг: `{ago}`\n\n"
        f"👇 Выберите нужный раздел:"
    )


def send_or_edit(chat_id, text, reply_markup, message_id=None):
    if message_id:
        try:
            bot.edit_message_text(
                text,
                chat_id,
                message_id,
                parse_mode="Markdown",
                reply_markup=reply_markup,
                disable_web_page_preview=True,
            )
            return
        except Exception:
            pass
    bot.send_message(
        chat_id,
        text,
        parse_mode="Markdown",
        reply_markup=reply_markup,
        disable_web_page_preview=True,
    )


# ----------------- СТАРТ И НАВИГАЦИЯ ПО ВКЛАДКАМ -----------------
@bot.message_handler(commands=["start", "menu"])
def cmd_start(message):
    if not is_admin(message.from_user.id):
        bot.send_message(message.chat.id, "⛔ Доступ запрещен.")
        return
    send_or_edit(message.chat.id, format_dashboard_text(), get_main_keyboard())


@bot.callback_query_handler(func=lambda call: True)
def handle_callbacks(call):
    if not is_admin(call.from_user.id):
        return

    chat_id = call.message.chat.id
    msg_id = call.message.message_id
    data = call.data

    # --- ГЛАВНОЕ МЕНЮ ---
    if data == "back_main":
        send_or_edit(chat_id, format_dashboard_text(), get_main_keyboard(), msg_id)

    # --- 1. МЕНЮ БИБЛИОТЕКИ (УДАЛЕНИЕ И ДОБАВЛЕНИЕ ИГР) ---
    elif data == "menu_library":
        kb = InlineKeyboardMarkup(row_width=1)
        kb.add(
            InlineKeyboardButton("🗑 Список игр для удаления", callback_data="lib_list_0"),
            InlineKeyboardButton("➕ Добавить игру по AppID / F2P", callback_data="test_f2p"),
            InlineKeyboardButton("⬅️ Назад в главное меню", callback_data="back_main"),
        )
        send_or_edit(
            chat_id,
            "📚 **Управление библиотекой Steam**\n\n"
            "• Вы можете посмотреть список всех бесплатных/акционных игр на аккаунте и удалить любую из них в один клик.\n"
            "• Либо добавить бесплатную игру по её `AppID`.",
            kb,
            msg_id,
        )

    elif data.startswith("lib_list_"):
        page = int(data.replace("lib_list_", ""))
        if page == 0 or chat_id not in library_cache:
            bot.answer_callback_query(call.id, "Загружаю библиотеку из Steam...")
            library_cache[chat_id] = steam.get_removable_licenses()

        items = library_cache.get(chat_id, [])
        per_page = 6
        total_pages = max(1, (len(items) + per_page - 1) // per_page)
        page = max(0, min(page, total_pages - 1))
        chunk = items[page * per_page : (page + 1) * per_page]

        kb = InlineKeyboardMarkup(row_width=1)
        for item in chunk:
            kb.add(
                InlineKeyboardButton(
                    f"❌ Удалить: {item['name']}",
                    callback_data=f"lib_del_{item['packageid']}_{page}",
                )
            )

        nav_btns = []
        if page > 0:
            nav_btns.append(InlineKeyboardButton("⬅️ Пред.", callback_data=f"lib_list_{page-1}"))
        nav_btns.append(InlineKeyboardButton("🔄 Обновить", callback_data="lib_list_0"))
        if page < total_pages - 1:
            nav_btns.append(InlineKeyboardButton("След. ➡️", callback_data=f"lib_list_{page+1}"))
        kb.row(*nav_btns)
        kb.add(InlineKeyboardButton("⬅️ Назад в Библиотеку", callback_data="menu_library"))

        text = (
            f"📚 **Игры, доступные для удаления ({len(items)} шт.)**\n"
            f"Страница `{page + 1}` из `{total_pages}`\n\n"
            f"Нажмите на игру, чтобы удалить её лицензию с аккаунта Steam:"
        )
        send_or_edit(chat_id, text, kb, msg_id)

    elif data.startswith("lib_del_"):
        parts = data.split("_")
        pkg_id = parts[2]
        page = int(parts[3]) if len(parts) > 3 else 0
        bot.answer_callback_query(call.id, f"Удаляю пакет {pkg_id}...")
        if steam.remove_license(pkg_id):
            if chat_id in library_cache:
                library_cache[chat_id] = [
                    x for x in library_cache[chat_id] if x["packageid"] != pkg_id
                ]
            bot.send_message(chat_id, f"✅ Лицензия `{pkg_id}` успешно удалена из библиотеки!", parse_mode="Markdown")
        else:
            bot.send_message(chat_id, f"❌ Не удалось удалить пакет `{pkg_id}`.", parse_mode="Markdown")
        call.data = f"lib_list_{page}"
        handle_callbacks(call)

    # --- 2. МЕНЮ РАДАРА СКИДОК ПО APPID ---
    elif data == "menu_discounts":
        watchlist = steam.data.get("watchlist", {})
        kb = InlineKeyboardMarkup(row_width=1)
        kb.add(
            InlineKeyboardButton("➕ Добавить игру по AppID в радар", callback_data="disc_add"),
            InlineKeyboardButton("🔍 Проверить скидки прямо сейчас", callback_data="disc_check_now"),
        )
        for appid, info in list(watchlist.items())[:8]:
            kb.add(
                InlineKeyboardButton(
                    f"🗑 Убрать: {info.get('name', appid)} ({appid})",
                    callback_data=f"disc_rem_{appid}",
                )
            )
        kb.add(InlineKeyboardButton("⬅️ Назад в главное меню", callback_data="back_main"))

        if watchlist:
            lines = [
                f"• **{v.get('name')}** (`{k}`) — посл. скидка: `{v.get('last_discount', 0)}%`"
                for k, v in watchlist.items()
            ]
            list_text = "\n".join(lines)
        else:
            list_text = "_Список пуст. Добавьте любую игру по её AppID!_"

        text = (
            f"📉 **Радар скидок Steam**\n"
            f"Последняя автопроверка: `{bot_status['last_discount_check']}`\n\n"
            f"**Отслеживаемые игры:**\n{list_text}\n\n"
            f"Бот проверяет цены каждые 2 часа и сразу пришлет уведомление, когда на игру появится скидка."
        )
        send_or_edit(chat_id, text, kb, msg_id)

    elif data == "disc_add":
        msg = bot.send_message(
            chat_id,
            "🔢 Отправьте **AppID** игры (или ссылку на игру в Steam), чтобы отслеживать скидки на неё:",
            parse_mode="Markdown",
        )
        bot.register_next_step_handler(msg, step_add_discount_watch)

    elif data.startswith("disc_rem_"):
        appid = data.replace("disc_rem_", "")
        steam.data.get("watchlist", {}).pop(appid, None)
        save_session(steam.data)
        bot.answer_callback_query(call.id, "Игра убрана из радара")
        call.data = "menu_discounts"
        handle_callbacks(call)

    elif data == "disc_check_now":
        bot.answer_callback_query(call.id, "Проверяю цены в Steam...")
        results = check_discounts(force_report=True)
        bot.send_message(
            chat_id,
            results or "ℹ️ Список отслеживания пуст.",
            parse_mode="Markdown",
            reply_markup=get_main_keyboard(),
        )

    # --- 3. МЕНЮ РАЗДАЧ ---
    elif data == "menu_giveaways":
        kb = InlineKeyboardMarkup(row_width=1)
        kb.add(
            InlineKeyboardButton("🔍 Проверить 100% раздачи сейчас", callback_data="check_now"),
            InlineKeyboardButton("⬅️ Назад в главное меню", callback_data="back_main"),
        )
        text = (
            f"🎁 **Автосбор платных игр со 100% скидкой**\n\n"
            f"• Интервал автопроверки: `каждые 10 часов`\n"
            f"• Последняя проверка: `{bot_status['last_check']}`\n"
            f"• Всего забрано за сеанс: `{bot_status['claimed_count']}`"
        )
        send_or_edit(chat_id, text, kb, msg_id)

    elif data == "check_now":
        bot.answer_callback_query(call.id, "Ищу активные раздачи...")
        count = check_giveaways()
        if count == 0:
            bot.send_message(
                chat_id,
                "ℹ️ Новых неактивированных раздач сейчас нет.",
                reply_markup=get_main_keyboard(),
            )

    # --- 4. МЕНЮ ПРОФИЛЯ STEAM ---
    elif data == "menu_profile":
        ok = steam.check_health()
        kb = InlineKeyboardMarkup(row_width=1)
        kb.add(
            InlineKeyboardButton("📝 Изменить графу «О себе»", callback_data="change_bio"),
            InlineKeyboardButton("🔄 Сменить аккаунт Steam", callback_data="login_steam"),
            InlineKeyboardButton("⬅️ Назад в главное меню", callback_data="back_main"),
        )
        text = (
            f"👤 **Профиль Steam**\n\n"
            f"• Логин: `{steam.data.get('account_name')}`\n"
            f"• SteamID: `{steam.data.get('steamid')}`\n"
            f"• Статус соединения: `{'Активно (OK)' if ok else 'Ошибка сессии'}`"
        )
        send_or_edit(chat_id, text, kb, msg_id)

    # --- 5. СТАТУС И ТИХИЙ ПИНГ (БЕЗ УВЕДОМЛЕНИЙ) ---
    elif data == "menu_ping":
        uptime_min = int((time.time() - bot_status["start_time"]) // 60)
        if bot_status["last_ping_ts"] > 0:
            sec_ago = int(time.time() - bot_status["last_ping_ts"])
            ago_str = f"{sec_ago // 60} мин. {sec_ago % 60} сек. назад"
        else:
            ago_str = "Еще не выполнялся"

        history_lines = "\n".join(bot_status["ping_history"][-8:]) or "_Пока нет записей_"

        kb = InlineKeyboardMarkup(row_width=2)
        kb.add(
            InlineKeyboardButton("🔄 Обновить статус", callback_data="menu_ping"),
            InlineKeyboardButton("⚡ Пингануть сейчас", callback_data="force_ping"),
        )
        kb.add(
            InlineKeyboardButton("🌐 Изменить URL Render", callback_data="set_render_url"),
            InlineKeyboardButton("⬅️ Назад в меню", callback_data="back_main"),
        )
        text = (
            f"📡 **Мониторинг тихого самопинга (Без уведомлений)**\n\n"
            f"🟢 Аптайм сервера: `{uptime_min} мин.`\n"
            f"🌐 URL: `{steam.data.get('render_url')}/ping`\n"
            f"⏱ Когда пинговался: **{ago_str}** (`{bot_status['last_ping_time']}`)\n"
            f"📊 Всего успешных пингов: `{bot_status['ping_count']}`\n\n"
            f"📜 **Последние пинги (тихий лог):**\n{history_lines}"
        )
        send_or_edit(chat_id, text, kb, msg_id)

    elif data == "force_ping":
        do_single_ping()
        bot.answer_callback_query(call.id, f"Пинг выполнен: {bot_status['last_ping_code']}")
        call.data = "menu_ping"
        handle_callbacks(call)

    # --- 6. СИСТЕМА И АВТООБНОВЛЕНИЕ БОТА ЧЕРЕЗ ТГ ---
    elif data == "menu_system":
        kb = InlineKeyboardMarkup(row_width=1)
        kb.add(
            InlineKeyboardButton("🔄 Обновить код бота (Скинуть код/файл)", callback_data="update_bot"),
            InlineKeyboardButton("💾 Получить SESSION_BASE64", callback_data="get_b64"),
            InlineKeyboardButton("📥 Вставить SESSION_BASE64", callback_data="import_b64"),
            InlineKeyboardButton("⬅️ Назад в главное меню", callback_data="back_main"),
        )
        text = (
            "⚙️ **Система и Автообновление**\n\n"
            "• Нажмите **«🔄 Обновить код бота»**, чтобы прислать новый файл `.py` или текст кода прямо в чат — бот сам заменит код и перезапустится.\n"
            "• Используйте `SESSION_BASE64` для резервного копирования всех настроек."
        )
        send_or_edit(chat_id, text, kb, msg_id)

    elif data == "update_bot":
        msg = bot.send_message(
            chat_id,
            "🔄 **Автообновление бота**\n\n"
            "Отправьте мне новый код одним из способов:\n"
            "1️⃣ **Файлом** (отправьте документ `bot.py`) — *рекомендуется для больших кодов*.\n"
            "2️⃣ **Текстовым сообщением** (просто вставьте Python-код в чат).\n\n"
            "*(Для отмены отправьте слово `отмена`)*:",
            parse_mode="Markdown",
        )
        bot.register_next_step_handler(msg, step_update_bot_code)

    # --- ОСТАЛЬНЫЕ КОЛБЭКИ (ВХОД, F2P, БИО, СЕССИЯ) ---
    elif data == "login_steam":
        msg = bot.send_message(
            chat_id,
            "🔑 **Шаг 1 из 3.** Отправьте ваш **логин Steam**:",
            parse_mode="Markdown",
        )
        bot.register_next_step_handler(msg, step_get_username)

    elif data == "import_b64":
        msg = bot.send_message(
            chat_id,
            "📥 Отправьте строку `SESSION_BASE64`:",
            parse_mode="Markdown",
        )
        bot.register_next_step_handler(msg, step_import_b64)

    elif data == "get_b64":
        b64 = export_session_b64(steam.data)
        bot.send_message(
            chat_id,
            f"💾 **Ваша строка `SESSION_BASE64`:**\n\n`{b64}`",
            parse_mode="Markdown",
            reply_markup=get_main_keyboard(),
        )

    elif data == "change_bio":
        msg = bot.send_message(
            chat_id,
            "✏️ Напишите новый текст для графы **«О себе»** в Steam:",
            parse_mode="Markdown",
        )
        bot.register_next_step_handler(msg, step_change_bio)

    elif data == "test_f2p":
        kb = InlineKeyboardMarkup(row_width=2)
        kb.add(
            InlineKeyboardButton("The Sims 4", callback_data="f2p_1222670"),
            InlineKeyboardButton("PUBG", callback_data="f2p_578080"),
            InlineKeyboardButton("Brawlhalla", callback_data="f2p_291550"),
            InlineKeyboardButton("Dota 2", callback_data="f2p_570"),
        )
        kb.add(
            InlineKeyboardButton("🔢 Ввести любой свой AppID", callback_data="f2p_custom"),
            InlineKeyboardButton("⬅️ Назад в Библиотеку", callback_data="menu_library"),
        )
        send_or_edit(
            chat_id,
            "🎮 **Добавление игры в библиотеку**\nВыберите игру из списка или введите свой `AppID`:",
            kb,
            msg_id,
        )

    elif data.startswith("f2p_"):
        target = data.replace("f2p_", "")
        if target == "custom":
            msg = bot.send_message(
                chat_id,
                "🔢 Отправьте числовой **AppID** бесплатной игры для добавления в библиотеку:",
                parse_mode="Markdown",
            )
            bot.register_next_step_handler(msg, step_custom_f2p)
        else:
            bot.answer_callback_query(call.id, "Добавляю игру...")
            add_f2p_game(chat_id, target)

    elif data == "set_render_url":
        msg = bot.send_message(
            chat_id,
            "🌐 Отправьте ссылку на ваш Render-сервис (например `https://myhub-yhxu.onrender.com`):",
            parse_mode="Markdown",
        )
        bot.register_next_step_handler(msg, step_set_render_url)


# ----------------- ОБРАБОТЧИК АВТООБНОВЛЕНИЯ БОТА -----------------
def step_update_bot_code(message):
    chat_id = message.chat.id
    if message.text and message.text.strip().lower() == "отмена":
        bot.send_message(chat_id, "❎ Обновление отменено.", reply_markup=get_main_keyboard())
        return

    new_code = None

    # Если прислали файл .py
    if message.document:
        try:
            file_info = bot.get_file(message.document.file_id)
            downloaded = bot.download_file(file_info.file_path)
            new_code = downloaded.decode("utf-8")
        except Exception as e:
            bot.send_message(chat_id, f"❌ Ошибка чтения файла: {e}", reply_markup=get_main_keyboard())
            return
    # Если прислали код текстом
    elif message.text:
        new_code = message.text.strip()
        # Убираем маркдаун-обертку ```python ... ``` если она есть
        new_code = re.sub(r"^```[a-zA-Z]*\n", "", new_code)
        new_code = re.sub(r"\n```$", "", new_code)

    if not new_code or len(new_code) < 50:
        bot.send_message(
            chat_id,
            "❌ Полученный код слишком короткий или пустой. Обновление отменено.",
            reply_markup=get_main_keyboard(),
        )
        return

    # Если в новом коде забыли вписать токен, автоматически подставляем текущий рабочий токен
    if "ВСТАВЬТЕ_СЮДА_ТОКЕН_БОТА" in new_code:
        new_code = new_code.replace("ВСТАВЬТЕ_СЮДА_ТОКЕН_БОТА", TG_BOT_TOKEN)

    # Проверяем синтаксис Python перед заменой файла, чтобы бот не сломался
    try:
        compile(new_code, "bot.py", "exec")
    except SyntaxError as e:
        bot.send_message(
            chat_id,
            f"❌ **Ошибка синтаксиса в новом коде!**\nСтрока {e.lineno}: `{e.msg}`\n\nСтарый бот продолжает работать.",
            parse_mode="Markdown",
            reply_markup=get_main_keyboard(),
        )
        return

    try:
        script_path = os.path.abspath(__file__)
        with open(script_path, "w", encoding="utf-8") as f:
            f.write(new_code)

        bot.send_message(
            chat_id,
            "✅ **Код успешно проверен и обновлен!**\n🔄 Перезапускаю бота (займет ~3 секунды)... Напишите `/start`.",
            parse_mode="Markdown",
        )
        time.sleep(1)
        os.execv(sys.executable, [sys.executable] + sys.argv)
    except Exception as e:
        bot.send_message(
            chat_id,
            f"❌ Не удалось перезапустить процесс: {e}",
            reply_markup=get_main_keyboard(),
        )


# ----------------- ОБРАБОТЧИКИ РАДАРА СКИДОК -----------------
def step_add_discount_watch(message):
    chat_id = message.chat.id
    text = message.text.strip()
    m = re.search(r"/app/(\d+)", text)
    app_id = m.group(1) if m else re.sub(r"\D", "", text)

    if not app_id:
        bot.send_message(chat_id, "❌ Не удалось распознать AppID.", reply_markup=get_main_keyboard())
        return

    bot.send_message(chat_id, f"⏳ Проверяю игру `{app_id}` в магазине Steam...", parse_mode="Markdown")
    info = steam.get_game_price_info(app_id)
    if not info:
        bot.send_message(
            chat_id,
            f"❌ Игра с AppID `{app_id}` не найдена в Steam.",
            parse_mode="Markdown",
            reply_markup=get_main_keyboard(),
        )
        return

    if "watchlist" not in steam.data:
        steam.data["watchlist"] = {}

    steam.data["watchlist"][app_id] = {
        "name": info["name"],
        "last_discount": info["discount"],
    }
    save_session(steam.data)

    sale_status = (
        f"🔥 **Сейчас уже действует скидка -{info['discount']}%!**\nЦена: `{info['final_formatted']}` (было `{info['initial_formatted']}`)"
        if info["discount"] > 0
        else f"💰 Текущая цена без скидки: `{info['final_formatted']}`"
    )

    bot.send_message(
        chat_id,
        f"✅ **Игра добавлена в радар скидок!**\n\n"
        f"🎮 **{info['name']}** (AppID: `{app_id}`)\n"
        f"{sale_status}\n\n"
        f"Бот автоматически пришлет уведомление, когда на неё появится новая скидка.",
        parse_mode="Markdown",
        reply_markup=get_main_keyboard(),
    )


def check_discounts(force_report=False):
    watchlist = steam.data.get("watchlist", {})
    if not watchlist:
        return ""

    bot_status["last_discount_check"] = time.strftime("%H:%M:%S")
    admin_id = steam.data.get("admin_id")
    report_lines = ["📊 **Отчет по ценам отслеживаемых игр:**\n"]

    for app_id, saved in list(watchlist.items()):
        info = steam.get_game_price_info(app_id)
        if not info:
            continue

        cur_disc = info["discount"]
        prev_disc = saved.get("last_discount", 0)

        if cur_disc > 0:
            report_lines.append(
                f"🔥 **{info['name']}** — скидка **-{cur_disc}%** (`{info['final_formatted']}` вместо `{info['initial_formatted']}`)"
            )
        else:
            report_lines.append(
                f"• **{info['name']}** — без скидки (`{info['final_formatted']}`)"
            )

        # Если появилась новая скидка (или скидка выросла), присылаем уведомление
        if cur_disc > 0 and cur_disc != prev_disc and admin_id:
            bot.send_message(
                admin_id,
                f"🔥 **СКИДКА В STEAM НА ОТСЛЕЖИВАЕМУЮ ИГРУ!**\n\n"
                f"🎮 **{info['name']}**\n"
                f"📉 Скидка: **-{cur_disc}%**\n"
                f"💰 Новая цена: **{info['final_formatted']}** (обычная: `{info['initial_formatted']}`)\n"
                f"🔗 https://store.steampowered.com/app/{app_id}/",
                parse_mode="Markdown",
            )

        steam.data["watchlist"][app_id]["last_discount"] = cur_disc
        steam.data["watchlist"][app_id]["name"] = info["name"]
        time.sleep(1.5)

    save_session(steam.data)
    return "\n".join(report_lines) if force_report else ""


def discounts_loop():
    while True:
        time.sleep(CHECK_DISCOUNTS_INTERVAL)
        try:
            check_discounts(force_report=False)
        except Exception:
            pass


# ----------------- ВХОД, ПРОФИЛЬ И F2P -----------------
def step_get_username(message):
    username = message.text.strip()
    pending_auth[message.chat.id] = {"username": username}
    msg = bot.send_message(
        message.chat.id,
        f"🔒 **Шаг 2 из 3.** Отправьте **пароль** от `{username}`:",
        parse_mode="Markdown",
    )
    bot.register_next_step_handler(msg, step_get_password)


def step_get_password(message):
    chat_id = message.chat.id
    password = message.text.strip()
    try:
        bot.delete_message(chat_id, message.message_id)
    except Exception:
        pass

    if chat_id not in pending_auth:
        bot.send_message(chat_id, "⚠️ Начните заново.", reply_markup=get_main_keyboard())
        return

    pending_auth[chat_id]["password"] = password
    msg = bot.send_message(
        chat_id,
        "🛡 **Шаг 3 из 3.** Откройте приложение Steam → вкладка **Steam Guard (щит)** → **«Показать код Steam Guard»** и отправьте **5-значный код** сюда:",
        parse_mode="Markdown",
    )
    bot.register_next_step_handler(msg, step_get_guard_code)


def step_get_guard_code(message):
    chat_id = message.chat.id
    guard_code = message.text.strip()
    auth_data = pending_auth.get(chat_id)
    if not auth_data or "password" not in auth_data:
        bot.send_message(chat_id, "⚠️ Начните вход заново.", reply_markup=get_main_keyboard())
        return

    bot.send_message(chat_id, "⚡ Выполняю пакетный вход...")
    res = steam.atomic_login(auth_data["username"], auth_data["password"], guard_code)
    pending_auth.pop(chat_id, None)

    if res.get("ok"):
        bot.send_message(
            chat_id,
            f"✅ **Успешный вход в `{steam.data.get('account_name')}`!**",
            parse_mode="Markdown",
            reply_markup=get_main_keyboard(),
        )
    else:
        bot.send_message(
            chat_id,
            f"❌ **Ошибка:** {res.get('error')}",
            parse_mode="Markdown",
            reply_markup=get_main_keyboard(),
        )


def step_import_b64(message):
    chat_id = message.chat.id
    try:
        decoded = json.loads(base64.b64decode(message.text.strip()).decode("utf-8"))
        decoded["admin_id"] = message.from_user.id
        if "render_url" not in decoded:
            decoded["render_url"] = DEFAULT_RENDER_URL
        steam.data = decoded
        save_session(steam.data)
        ok = steam.check_health()
        bot.send_message(
            chat_id,
            f"✅ **Сессия импортирована!** Статус: `{'OK' if ok else 'Ошибка'}`",
            parse_mode="Markdown",
            reply_markup=get_main_keyboard(),
        )
    except Exception as e:
        bot.send_message(chat_id, f"❌ Ошибка: {e}", reply_markup=get_main_keyboard())


def step_change_bio(message):
    new_bio = message.text.strip()
    if steam.update_bio(new_bio):
        bot.send_message(
            message.chat.id,
            f"✅ **Описание «О себе» обновлено:** `{new_bio}`",
            parse_mode="Markdown",
            reply_markup=get_main_keyboard(),
        )
    else:
        bot.send_message(message.chat.id, "❌ Ошибка обновления профиля.", reply_markup=get_main_keyboard())


def step_custom_f2p(message):
    app_id = re.sub(r"\D", "", message.text.strip())
    if not app_id:
        bot.send_message(message.chat.id, "❌ Введите только цифры AppID.", reply_markup=get_main_keyboard())
        return
    add_f2p_game(message.chat.id, app_id)


def add_f2p_game(chat_id, app_id):
    sub_id = steam.get_subid_from_appid(app_id)
    if not sub_id:
        bot.send_message(
            chat_id,
            f"❌ Бесплатный SubID для AppID `{app_id}` не найден.",
            parse_mode="Markdown",
            reply_markup=get_main_keyboard(),
        )
        return

    if steam.add_free_license(sub_id):
        bot_status["claimed_count"] += 1
        library_cache.pop(chat_id, None)  # Сбрасываем кэш библиотеки
        bot.send_message(
            chat_id,
            f"✅ **Игра добавлена в библиотеку!** (AppID: `{app_id}`, SubID: `{sub_id}`)",
            parse_mode="Markdown",
            reply_markup=get_main_keyboard(),
        )
    else:
        bot.send_message(chat_id, "❌ Ошибка активации лицензии.", reply_markup=get_main_keyboard())


def step_set_render_url(message):
    url = message.text.strip().rstrip("/")
    if not url.startswith("http"):
        url = "https://" + url
    steam.data["render_url"] = url
    save_session(steam.data)
    bot.send_message(
        message.chat.id,
        f"✅ URL самопинга сохранен: `{url}/ping`",
        parse_mode="Markdown",
        reply_markup=get_main_keyboard(),
    )


# ----------------- АВТОПОИСК 100% РАЗДАЧ -----------------
def check_giveaways():
    if not steam.is_configured():
        return 0

    claimed = load_claimed()
    bot_status["last_check"] = time.strftime("%Y-%m-%d %H:%M:%S")
    added = 0
    admin_id = steam.data.get("admin_id")

    try:
        url = "https://www.gamerpower.com/api/giveaways?platform=steam&type=game"
        resp = requests.get(url, timeout=15)
        if resp.status_code != 200:
            return 0
        giveaways = resp.json()
    except Exception:
        return 0

    for g in giveaways:
        gid = str(g.get("id"))
        title = g.get("title", "Steam Game")
        open_url = g.get("open_giveaway_url", "")

        if gid in claimed:
            continue

        if "store.steampowered.com/app/" in open_url:
            try:
                app_id = open_url.split("/app/")[1].split("/")[0]
                sub_id = steam.get_subid_from_appid(app_id)
                if sub_id and steam.add_free_license(sub_id):
                    claimed.add(gid)
                    save_claimed(claimed)
                    added += 1
                    bot_status["claimed_count"] += 1
                    if admin_id:
                        bot.send_message(
                            admin_id,
                            f"🎁 **Автоматически забрана игра по раздаче!**\n\n"
                            f"🎮 **{title}**\n"
                            f"📦 AppID: `{app_id}` (SubID: `{sub_id}`)\n"
                            f"🔗 https://store.steampowered.com/app/{app_id}/",
                            parse_mode="Markdown",
                        )
                    time.sleep(3)
            except Exception:
                pass

    return added


def giveaway_loop():
    while True:
        try:
            if steam.is_configured():
                check_giveaways()
        except Exception:
            pass
        time.sleep(CHECK_GIVEAWAYS_INTERVAL)


# ----------------- ТИХИЙ САМОПИНГ БЕЗ УВЕДОМЛЕНИЙ -----------------
app = Flask(__name__)


@app.route("/")
@app.route("/ping")
def ping_endpoint():
    return jsonify({
        "status": "alive",
        "steam_configured": steam.is_configured(),
        "last_ping": bot_status["last_ping_time"],
        "ping_count": bot_status["ping_count"],
    }), 200


def do_single_ping():
    """Выполняет тихий пинг без отправки уведомлений в Telegram и записывает время в лог."""
    base_url = (
        steam.data.get("render_url")
        or os.environ.get("RENDER_EXTERNAL_URL")
        or DEFAULT_RENDER_URL
    )
    ping_url = f"{base_url.rstrip('/')}/ping"
    bot_status["last_ping_url"] = ping_url
    t0 = time.time()

    try:
        r = requests.get(ping_url, headers={"User-Agent": USER_AGENT}, timeout=15)
        ms = int((time.time() - t0) * 1000)
        now_str = time.strftime("%H:%M:%S")
        bot_status["last_ping_ts"] = time.time()
        bot_status["last_ping_time"] = now_str
        bot_status["last_ping_code"] = f"HTTP {r.status_code} ({ms} мс)"
        bot_status["ping_count"] += 1
        bot_status["ping_history"].append(f"• `{now_str}` — ✅ HTTP {r.status_code} (`{ms} мс`)")
    except Exception as e:
        now_str = time.strftime("%H:%M:%S")
        bot_status["last_ping_ts"] = time.time()
        bot_status["last_ping_time"] = now_str
        bot_status["last_ping_code"] = "Ошибка"
        bot_status["ping_history"].append(f"• `{now_str}` — ❌ Ошибка сети")

    # Оставляем только последние 10 записей
    if len(bot_status["ping_history"]) > 10:
        bot_status["ping_history"] = bot_status["ping_history"][-10:]


def keep_alive_pinger():
    # Первый пинг через 15 секунд после старта
    time.sleep(15)
    do_single_ping()
    while True:
        time.sleep(random.randint(180, 300))  # Каждые 3-5 минут
        do_single_ping()


if __name__ == "__main__":
    threading.Thread(target=giveaway_loop, daemon=True).start()
    threading.Thread(target=discounts_loop, daemon=True).start()
    threading.Thread(target=keep_alive_pinger, daemon=True).start()
    threading.Thread(
        target=lambda: bot.infinity_polling(skip_pending=True), daemon=True
    ).start()

    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
