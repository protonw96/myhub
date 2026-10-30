import base64
import html
import json
import os
import random
import re
import secrets
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
# =====================================================================

SESSION_FILE = "session.json"
CLAIMED_FILE = "claimed_games.json"
CHECK_INTERVAL = 10 * 3600  # Проверка новых раздач каждые 10 часов
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

bot_status = {
    "start_time": time.time(),
    "last_ping_time": "Еще не выполнялся",
    "last_ping_url": "Ожидание...",
    "last_ping_code": "—",
    "last_check": "Еще не выполнялась",
    "steam_ok": False,
    "claimed_count": 0,
}

# Временное хранилище для шагов входа
pending_auth = {}


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

    @property
    def session_id(self):
        return self.data["session_id"]

    def is_configured(self):
        return bool(self.data.get("refresh_token") and self.data.get("steamid"))

    def atomic_login(self, username, password, guard_code):
        """
        Мгновенная авторизация (Логин + Пароль + 5-значный код Guard за один раз).
        Предотвращает появление красного экрана гео-блокировки в мобильном приложении Steam.
        """
        try:
            # 1. Получаем RSA-ключ
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

            # 2. Начинаем сессию как обычный веб-клиент ПК (platform_type=2 не конфликтует с телефоном)
            begin_url = "https://api.steampowered.com/IAuthenticationService/BeginAuthSessionViaCredentials/v1/"
            begin_payload = {
                "account_name": username,
                "encrypted_password": encrypted_pw,
                "encryption_timestamp": timestamp,
                "remember_login": "true",
                "platform_type": "2",  # WebBrowser (снижает риск гео-блокировки)
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
                return {
                    "ok": False,
                    "error": "Неверный логин или пароль (либо временный лимит попыток).",
                }

            confirmations = b_res.get("allowed_confirmations", [])
            conf_types = [c.get("confirmation_type") for c in confirmations]
            # 3 = Мобильный код Steam Guard, 2 = Код с почты
            code_type = 3 if 3 in conf_types else (2 if 2 in conf_types else 3)

            # 3. МГНОВЕННО (без паузы) отправляем 5-значный код Steam Guard
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

            # 4. Сразу забираем токены
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
                "error": (
                    "Steam отклонил код или заблокировал IP сервера Render (США). "
                    "Используйте кнопку «📥 Вставить готовую сессию» (через Termux со своего IP)."
                ),
            }
        except Exception as e:
            return {"ok": False, "error": f"Ошибка сети: {e}"}

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
        except Exception as e:
            print(f"[!] Ошибка обновления токена: {e}")
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
            else:
                xml_resp = requests.get(
                    f"https://steamcommunity.com/profiles/{steamid}/?xml=1",
                    cookies=cookies,
                    timeout=15,
                )
                xml_match = re.search(
                    r"<steamID><!\[CDATA\[(.*?)\]\]></steamID>", xml_resp.text
                )
                if xml_match:
                    persona_name = xml_match.group(1)
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
        if r.status_code == 200:
            try:
                return r.json().get("success") == 1
            except Exception:
                return True
        return False

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
            api_url = f"https://store.steampowered.com/api/appdetails?appids={app_id}&cc=us&l=en"
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


steam = SteamClient()
bot = telebot.TeleBot(TG_BOT_TOKEN)


def is_admin(user_id):
    saved_admin = steam.data.get("admin_id")
    if not saved_admin:
        steam.data["admin_id"] = user_id
        save_session(steam.data)
        return True
    return int(saved_admin) == int(user_id)


# ----------------- КЛАВИАТУРЫ TELEGRAM -----------------
def get_main_keyboard():
    kb = InlineKeyboardMarkup(row_width=1)
    if not steam.is_configured():
        kb.add(
            InlineKeyboardButton(
                "🔑 Войти в Steam (Безопасный вход по коду)", callback_data="login_steam"
            ),
            InlineKeyboardButton(
                "📥 Вставить готовую сессию (Обход гео-блока 100%)",
                callback_data="import_b64",
            ),
        )
    else:
        kb.add(
            InlineKeyboardButton(
                "📝 1. Изменить «О себе» в Steam (Тест)", callback_data="change_bio"
            ),
            InlineKeyboardButton(
                "🎮 2. Добавить игру из Free-to-Play (Тест)", callback_data="test_f2p"
            ),
            InlineKeyboardButton(
                "🔍 Проверить раздачи прямо сейчас", callback_data="check_now"
            ),
            InlineKeyboardButton(
                "💓 Проверить работоспособность и пинг", callback_data="health_check"
            ),
            InlineKeyboardButton(
                "🌐 Указать ссылку Render для самопинга", callback_data="set_render_url"
            ),
            InlineKeyboardButton(
                "💾 Получить код сессии (SESSION_BASE64)", callback_data="get_b64"
            ),
            InlineKeyboardButton(
                "📥 Вставить готовую сессию (SESSION_BASE64)", callback_data="import_b64"
            ),
            InlineKeyboardButton("🔄 Сменить аккаунт Steam", callback_data="login_steam"),
        )
    return kb


# ----------------- ОБРАБОТЧИКИ TELEGRAM -----------------
@bot.message_handler(commands=["start", "menu"])
def cmd_start(message):
    if not is_admin(message.from_user.id):
        bot.send_message(message.chat.id, "⛔ Доступ запрещен.")
        return

    if not steam.is_configured():
        text = (
            "👋 **Добро пожаловать в Steam AutoClaim Bot!**\n\n"
            "Бот оснащен защитой от гео-блокировки Steam:\n"
            "• Нажмите **«🔑 Войти в Steam»** — бот соберет логин, пароль и 5-значный код Guard и отправит их одним пакетом, чтобы телефон не блокировал вход из США.\n"
            "• Или нажмите **«📥 Вставить готовую сессию»**, если сгенерировали токен локально через Termux."
        )
    else:
        ok = steam.check_health()
        status_text = "✅ Активна" if ok else "❌ Требуется повторный вход"
        text = (
            f"🤖 **Панель управления Steam AutoClaim**\n\n"
            f"👤 Аккаунт Steam: `{steam.data.get('account_name')}`\n"
            f"🔐 Сессия: {status_text}\n"
            f"⏱ Поиск новых раздач: каждые 10 часов\n"
            f"📡 Самопинг Render: каждые 3–5 минут\n\n"
            f"Выберите действие:"
        )
    bot.send_message(
        message.chat.id, text, parse_mode="Markdown", reply_markup=get_main_keyboard()
    )


@bot.callback_query_handler(func=lambda call: True)
def handle_callbacks(call):
    if not is_admin(call.from_user.id):
        return

    chat_id = call.message.chat.id

    if call.data == "login_steam":
        msg = bot.send_message(
            chat_id,
            "🔑 **Шаг 1 из 3.** Отправьте ваш **логин Steam** (имя аккаунта):",
            parse_mode="Markdown",
        )
        bot.register_next_step_handler(msg, step_get_username)

    elif call.data == "import_b64":
        msg = bot.send_message(
            chat_id,
            "📥 Отправьте сюда строку `SESSION_BASE64`, полученную при запуске бота на телефоне (в Termux) или ПК:",
            parse_mode="Markdown",
        )
        bot.register_next_step_handler(msg, step_import_b64)

    elif call.data == "change_bio":
        msg = bot.send_message(
            chat_id,
            "✏️ Напишите текст, который нужно установить в графу **«О себе»** в вашем профиле Steam:",
            parse_mode="Markdown",
        )
        bot.register_next_step_handler(msg, step_change_bio)

    elif call.data == "test_f2p":
        kb = InlineKeyboardMarkup(row_width=1)
        kb.add(
            InlineKeyboardButton("The Sims 4 (AppID: 1222670)", callback_data="f2p_1222670"),
            InlineKeyboardButton("PUBG: BATTLEGROUNDS (AppID: 578080)", callback_data="f2p_578080"),
            InlineKeyboardButton("Brawlhalla (AppID: 291550)", callback_data="f2p_291550"),
            InlineKeyboardButton("Dota 2 (AppID: 570)", callback_data="f2p_570"),
            InlineKeyboardButton("🔢 Ввести свой AppID игры", callback_data="f2p_custom"),
            InlineKeyboardButton("⬅️ Назад в меню", callback_data="back_main"),
        )
        bot.edit_message_text(
            "🎮 Выберите бесплатную игру (Free-to-Play) для тестового добавления в библиотеку:",
            chat_id,
            call.message.message_id,
            reply_markup=kb,
        )

    elif call.data.startswith("f2p_"):
        target = call.data.replace("f2p_", "")
        if target == "custom":
            msg = bot.send_message(
                chat_id,
                "🔢 Отправьте числовой **AppID** бесплатной игры из Steam (например `440` для Team Fortress 2):",
                parse_mode="Markdown",
            )
            bot.register_next_step_handler(msg, step_custom_f2p)
        else:
            bot.answer_callback_query(call.id, "Добавляю игру...")
            add_f2p_game(chat_id, target)

    elif call.data == "check_now":
        bot.answer_callback_query(call.id, "Проверяю раздачи...")
        count = check_giveaways()
        if count == 0:
            bot.send_message(
                chat_id,
                "ℹ️ Новых временно бесплатных игр сейчас нет (или все уже добавлены).",
                reply_markup=get_main_keyboard(),
            )

    elif call.data == "health_check":
        ok = steam.check_health()
        uptime_min = int((time.time() - bot_status["start_time"]) // 60)
        text = (
            f"💓 **Проверка работоспособности скрипта**\n\n"
            f"🟢 Время работы: `{uptime_min} мин.`\n"
            f"🔐 Сессия Steam: `{'Активна (OK)' if ok else 'Ошибка сессии'}`\n"
            f"🌐 Адрес самопинга: `{bot_status['last_ping_url']}`\n"
            f"📡 Последний пинг: `{bot_status['last_ping_time']}` (`{bot_status['last_ping_code']}`)\n"
            f"🔍 Последняя проверка игр: `{bot_status['last_check']}`\n"
            f"🎁 Добавлено игр за сеанс: `{bot_status['claimed_count']}`"
        )
        bot.send_message(
            chat_id, text, parse_mode="Markdown", reply_markup=get_main_keyboard()
        )

    elif call.data == "set_render_url":
        msg = bot.send_message(
            chat_id,
            "🌐 Отправьте ссылку на ваш проект на Render (например `https://my-steam-bot.onrender.com`):",
            parse_mode="Markdown",
        )
        bot.register_next_step_handler(msg, step_set_render_url)

    elif call.data == "get_b64":
        b64 = export_session_b64(steam.data)
        bot.send_message(
            chat_id,
            "💾 **Ваша строка сессии (`SESSION_BASE64`):**\n\n"
            f"`{b64}`\n\n"
            "Её можно вставить в переменную окружения `SESSION_BASE64` на Render или отправить боту через кнопку «📥 Вставить готовую сессию».",
            parse_mode="Markdown",
            reply_markup=get_main_keyboard(),
        )

    elif call.data == "back_main":
        cmd_start(call.message)


# ----------------- ОТЛОЖЕННЫЙ МГНОВЕННЫЙ ВХОД (ОБХОД ГЕО-БЛОКА) -----------------
def step_get_username(message):
    username = message.text.strip()
    pending_auth[message.chat.id] = {"username": username}
    msg = bot.send_message(
        message.chat.id,
        f"🔒 **Шаг 2 из 3.** Отправьте **пароль** от аккаунта `{username}`.\n"
        f"*(Бот сразу удалит сообщение с паролем и НЕ будет отправлять запрос в Steam до ввода кода, чтобы приложение не заблокировало вход)*:",
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
        bot.send_message(chat_id, "⚠️ Начните вход заново.", reply_markup=get_main_keyboard())
        return

    pending_auth[chat_id]["password"] = password

    msg = bot.send_message(
        chat_id,
        "🛡 **Шаг 3 из 3. Внимательно выполните инструкцию:**\n\n"
        "1. Откройте приложение **Steam** на телефоне.\n"
        "2. Нажмите на **значок щита (Steam Guard)** внизу экрана.\n"
        "3. Нажмите **«Показать код Steam Guard»** (5 букв/цифр).\n"
        "4. Отправьте этот **5-значный код** сюда в чат.\n\n"
        "⚠️ *ВАЖНО: Если после отправки кода в приложении всплывет окно «Попытка входа из Boardman, US» — НЕ НАЖИМАЙТЕ на него, бот сам подтвердит вход вашим 5-значным кодом за долю секунды!*",
        parse_mode="Markdown",
    )
    bot.register_next_step_handler(msg, step_get_guard_code)


def step_get_guard_code(message):
    chat_id = message.chat.id
    guard_code = message.text.strip()
    auth_data = pending_auth.get(chat_id)

    if not auth_data or "password" not in auth_data:
        bot.send_message(chat_id, "⚠️ Сессия ввода истекла, начните заново.", reply_markup=get_main_keyboard())
        return

    bot.send_message(chat_id, "⚡ Выполняю мгновенный пакетный вход в Steam...")
    res = steam.atomic_login(
        auth_data["username"], auth_data["password"], guard_code
    )
    pending_auth.pop(chat_id, None)

    if res.get("ok"):
        b64 = export_session_b64(steam.data)
        bot.send_message(
            chat_id,
            f"✅ **Успешный вход в аккаунт `{steam.data.get('account_name')}`!**\n\n"
            f"👇 **Проверьте работу бота:**\n"
            f"1. Нажмите **«1. Изменить О себе в Steam»** и отправьте любой текст.\n"
            f"2. Нажмите **«2. Добавить игру из Free-to-Play»**, чтобы проверить добавление игр.\n\n"
            f"💾 Ваш код сессии `SESSION_BASE64` (сохранен):\n`{b64}`",
            parse_mode="Markdown",
            reply_markup=get_main_keyboard(),
        )
    else:
        bot.send_message(
            chat_id,
            f"❌ **Не удалось войти:** {res.get('error')}",
            parse_mode="Markdown",
            reply_markup=get_main_keyboard(),
        )


def step_import_b64(message):
    chat_id = message.chat.id
    raw_b64 = message.text.strip()
    try:
        decoded = json.loads(base64.b64decode(raw_b64).decode("utf-8"))
        if "refresh_token" not in decoded or "steamid" not in decoded:
            raise ValueError("В строке нет refresh_token или steamid")
        decoded["admin_id"] = message.from_user.id
        steam.data = decoded
        save_session(steam.data)
        ok = steam.check_health()
        bot.send_message(
            chat_id,
            f"✅ **Сессия успешно импортирована!**\n"
            f"👤 Аккаунт: `{steam.data.get('account_name')}`\n"
            f"🔐 Проверка связи со Steam: `{'Успешно (OK)' if ok else 'Ошибка'}`",
            parse_mode="Markdown",
            reply_markup=get_main_keyboard(),
        )
    except Exception as e:
        bot.send_message(
            chat_id,
            f"❌ Некорректная строка SESSION_BASE64 ({e}).",
            reply_markup=get_main_keyboard(),
        )


# ----------------- ДЕЙСТВИЯ ТЕСТИРОВАНИЯ -----------------
def step_change_bio(message):
    new_bio = message.text.strip()
    bot.send_message(message.chat.id, "⏳ Меняю описание «О себе» в Steam...")
    if steam.update_bio(new_bio):
        profile_url = f"https://steamcommunity.com/profiles/{steam.data['steamid']}"
        bot.send_message(
            message.chat.id,
            f"✅ **Графа «О себе» успешно изменена!**\n\n"
            f"Текст: `{new_bio}`\n"
            f"🔗 [Открыть профиль Steam]({profile_url})",
            parse_mode="Markdown",
            reply_markup=get_main_keyboard(),
        )
    else:
        bot.send_message(
            message.chat.id,
            "❌ Ошибка при обновлении профиля. Проверьте статус сессии.",
            reply_markup=get_main_keyboard(),
        )


def step_custom_f2p(message):
    app_id = re.sub(r"\D", "", message.text.strip())
    if not app_id:
        bot.send_message(message.chat.id, "❌ Введите только цифры AppID.", reply_markup=get_main_keyboard())
        return
    add_f2p_game(message.chat.id, app_id)


def add_f2p_game(chat_id, app_id):
    bot.send_message(chat_id, f"⏳ Добавляю игру (AppID: `{app_id}`)...", parse_mode="Markdown")
    sub_id = steam.get_subid_from_appid(app_id)
    if not sub_id:
        bot.send_message(
            chat_id,
            f"❌ Не найден бесплатный пакет для AppID `{app_id}`.",
            parse_mode="Markdown",
            reply_markup=get_main_keyboard(),
        )
        return

    if steam.add_free_license(sub_id):
        bot_status["claimed_count"] += 1
        bot.send_message(
            chat_id,
            f"✅ **Игра успешно добавлена в библиотеку Steam!**\n\n"
            f"📦 AppID: `{app_id}` | SubID: `{sub_id}`\n"
            f"🔗 https://store.steampowered.com/app/{app_id}/",
            parse_mode="Markdown",
            reply_markup=get_main_keyboard(),
        )
    else:
        bot.send_message(
            chat_id,
            f"❌ Ошибка при активации пакета `{sub_id}`.",
            parse_mode="Markdown",
            reply_markup=get_main_keyboard(),
        )


def step_set_render_url(message):
    url = message.text.strip().rstrip("/")
    if not url.startswith("http"):
        url = "https://" + url
    steam.data["render_url"] = url
    save_session(steam.data)
    bot.send_message(
        message.chat.id,
        f"✅ Ссылка для самопинга установлена: `{url}/ping`",
        parse_mode="Markdown",
        reply_markup=get_main_keyboard(),
    )


# ----------------- АВТОПОИСК РАЗДАЧ (КАЖДЫЕ 10 ЧАСОВ) -----------------
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
                            f"🎁 **Автоматически добавлена платная игра по раздаче!**\n\n"
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
        time.sleep(CHECK_INTERVAL)


# ----------------- ВЕБ-СЕРВЕР И САМОПИНГ (3-5 МИНУТ) -----------------
app = Flask(__name__)


@app.route("/")
@app.route("/ping")
def ping_endpoint():
    return jsonify({
        "status": "alive",
        "steam_configured": steam.is_configured(),
        "last_ping": bot_status["last_ping_time"],
        "last_check": bot_status["last_check"],
    }), 200


def keep_alive_pinger():
    while True:
        time.sleep(random.randint(180, 300))
        base_url = (
            steam.data.get("render_url")
            or os.environ.get("RENDER_EXTERNAL_URL")
            or f"http://127.0.0.1:{os.environ.get('PORT', 10000)}"
        )
        ping_url = f"{base_url.rstrip('/')}/ping"
        bot_status["last_ping_url"] = ping_url

        try:
            r = requests.get(ping_url, headers={"User-Agent": USER_AGENT}, timeout=15)
            bot_status["last_ping_time"] = time.strftime("%H:%M:%S")
            bot_status["last_ping_code"] = f"HTTP {r.status_code}"
        except Exception as e:
            bot_status["last_ping_time"] = time.strftime("%H:%M:%S")
            bot_status["last_ping_code"] = f"Ошибка ({e})"


if __name__ == "__main__":
    threading.Thread(target=giveaway_loop, daemon=True).start()
    threading.Thread(target=keep_alive_pinger, daemon=True).start()
    threading.Thread(
        target=lambda: bot.infinity_polling(skip_pending=True), daemon=True
    ).start()

    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
