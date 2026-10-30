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
# ЕДИНСТВЕННАЯ НАСТРОЙКА: ВСТАВЬТЕ СЮДА ТОКЕН ВАШЕГО TELEGRAM-БОТА
# =====================================================================
TG_BOT_TOKEN = os.environ.get("TG_BOT_TOKEN", "8721976140:AAEHfoi-6aZ5S9Y0alYz_d0hKz_yUz26Ee0")
# =====================================================================

SESSION_FILE = "session.json"
CLAIMED_FILE = "claimed_games.json"
CHECK_INTERVAL = 10 * 3600  # Проверка раздач каждые 10 часов
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

# Глобальное состояние для отчета о работоспособности
bot_status = {
    "start_time": time.time(),
    "last_ping_time": "Еще не выполнялся",
    "last_ping_url": "Ожидание...",
    "last_ping_code": "—",
    "last_check": "Еще не выполнялась",
    "steam_ok": False,
    "claimed_count": 0,
}

# Временное хранилище данных при пошаговом входе через Telegram
pending_auth = {}


# ----------------- РАБОТА С СЕССИЕЙ И БАЗОЙ ИГР -----------------
def load_session():
    """Загружает сессию из переменной окружения SESSION_BASE64 или из файла session.json."""
    env_b64 = os.environ.get("SESSION_BASE64")
    if env_b64:
        try:
            raw = base64.b64decode(env_b64.strip()).decode("utf-8")
            data = json.loads(raw)
            with open(SESSION_FILE, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
            return data
        except Exception as e:
            print(f"[!] Ошибка декодирования SESSION_BASE64: {e}")

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

    def start_tg_login(self, username, password):
        """Шаг 1: Шифрование пароля RSA и отправка запроса на вход в Steam."""
        rsa_url = "https://api.steampowered.com/IAuthenticationService/GetPasswordRSAPublicKey/v1/"
        r = requests.get(rsa_url, params={"account_name": username}, timeout=15).json()
        resp = r.get("response", {})
        if not resp.get("publickey_mod"):
            return {"ok": False, "error": "Не удалось получить RSA-ключ от Steam."}

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
            "platform_type": "3",  # MobileApp для долгоживущего токена
            "website_id": "Mobile",
            "persistence": "1",
            "device_friendly_name": "SteamTelegramBot",
        }
        b_res = requests.post(begin_url, data=begin_payload, timeout=15).json().get("response", {})

        client_id = b_res.get("client_id")
        request_id = b_res.get("request_id")
        steamid = b_res.get("steamid")

        if not client_id or not steamid:
            return {
                "ok": False,
                "error": "Неверный логин или пароль Steam (либо временная блокировка попыток входа).",
            }

        confirmations = b_res.get("allowed_confirmations", [])
        conf_types = [c.get("confirmation_type") for c in confirmations]
        # Тип 3 = код из мобильного приложения, Тип 2 = код с почты
        code_type = 3 if 3 in conf_types else (2 if 2 in conf_types else None)

        return {
            "ok": True,
            "username": username,
            "client_id": client_id,
            "request_id": request_id,
            "steamid": str(steamid),
            "code_type": code_type,
        }

    def finish_tg_login(self, auth_state, guard_code=None):
        """Шаг 2: Отправка кода Steam Guard и получение вечных токенов."""
        client_id = auth_state["client_id"]
        request_id = auth_state["request_id"]
        steamid = auth_state["steamid"]
        code_type = auth_state["code_type"]

        # Если пользователь прислал 5-значный код (а не просто подтвердил пуш в приложении)
        if guard_code and len(guard_code) == 5 and code_type:
            guard_url = "https://api.steampowered.com/IAuthenticationService/UpdateAuthSessionWithSteamGuardCode/v1/"
            requests.post(
                guard_url,
                data={
                    "client_id": client_id,
                    "steamid": steamid,
                    "code": guard_code.upper(),
                    "code_type": code_type,
                },
                timeout=15,
            )

        poll_url = "https://api.steampowered.com/IAuthenticationService/PollAuthSessionStatus/v1/"
        for _ in range(6):
            p_res = requests.post(
                poll_url,
                data={"client_id": client_id, "request_id": request_id},
                timeout=15,
            ).json().get("response", {})

            if p_res.get("refresh_token") and p_res.get("access_token"):
                self.data.update({
                    "steamid": steamid,
                    "account_name": auth_state["username"],
                    "refresh_token": p_res["refresh_token"],
                    "access_token": p_res["access_token"],
                    "session_id": secrets.token_hex(12),
                })
                save_session(self.data)
                bot_status["steam_ok"] = True
                return True
            time.sleep(2)

        return False

    def is_token_expired(self):
        token = self.data.get("access_token", "")
        if not token:
            return True
        try:
            payload_part = token.split(".")[1]
            padding = "=" * ((4 - len(payload_part) % 4) % 4)
            decoded = json.loads(base64.urlsafe_b64decode(payload_part + padding).decode("utf-8"))
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
        """Меняет графу 'О себе' в профиле Steam."""
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
                xml_match = re.search(r"<steamID><!\[CDATA\[(.*?)\]\]></steamID>", xml_resp.text)
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

        r = requests.post(post_url, data=payload, cookies=cookies, headers=headers, timeout=15)
        if r.status_code == 200:
            try:
                return r.json().get("success") == 1
            except Exception:
                return True
        return False

    def get_subid_from_appid(self, app_id):
        """Ищет бесплатный пакет (SubID) игры по её AppID."""
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
        """Активирует бесплатную игру (F2P или временную 100% скидку) в библиотеку."""
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
            data={"action": "add_to_cart", "sessionid": self.session_id, "subid": str(sub_id)},
            cookies=cookies,
            headers=headers,
            timeout=15,
        )

        return r1.status_code == 200 or r2.status_code == 200


steam = SteamClient()
bot = telebot.TeleBot(TG_BOT_TOKEN)


# ----------------- ПРОВЕРКА ВЛАДЕЛЬЦА БОТА -----------------
def is_admin(user_id):
    """Привязывает бота к первому пользователю, который написал /start."""
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
        kb.add(InlineKeyboardButton("🔑 Войти в аккаунт Steam", callback_data="login_steam"))
    else:
        kb.add(
            InlineKeyboardButton("📝 1. Изменить «О себе» в Steam (Тест)", callback_data="change_bio"),
            InlineKeyboardButton("🎮 2. Добавить игру из Free-to-Play (Тест)", callback_data="test_f2p"),
            InlineKeyboardButton("🔍 Проверить раздачи прямо сейчас", callback_data="check_now"),
            InlineKeyboardButton("💓 Проверить работоспособность и пинг", callback_data="health_check"),
            InlineKeyboardButton("🌐 Указать ссылку Render для самопинга", callback_data="set_render_url"),
            InlineKeyboardButton("💾 Получить код сессии (SESSION_BASE64)", callback_data="get_b64"),
            InlineKeyboardButton("🔄 Сменить аккаунт Steam", callback_data="login_steam"),
        )
    return kb


# ----------------- ОБРАБОТЧИКИ TELEGRAM -----------------
@bot.message_handler(commands=["start", "menu"])
def cmd_start(message):
    if not is_admin(message.from_user.id):
        bot.send_message(message.chat.id, "⛔ Доступ запрещен. Бот привязан к другому владельцу.")
        return

    if not steam.is_configured():
        text = (
            "👋 **Добро пожаловать в Steam AutoClaim Bot!**\n\n"
            "Бот еще не привязан к аккаунту Steam.\n"
            "Нажмите кнопку ниже, чтобы ввести логин, пароль и код Steam Guard прямо здесь в чате."
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
            f"Выберите нужное действие:"
        )
    bot.send_message(message.chat.id, text, parse_mode="Markdown", reply_markup=get_main_keyboard())


@bot.callback_query_handler(func=lambda call: True)
def handle_callbacks(call):
    if not is_admin(call.from_user.id):
        return

    chat_id = call.message.chat.id

    # --- ВХОД В STEAM ЧЕРЕЗ ТГ ---
    if call.data == "login_steam":
        msg = bot.send_message(
            chat_id,
            "🔑 **Шаг 1 из 3.** Отправьте ваш **логин Steam** (имя аккаунта, которое используете для входа):",
            parse_mode="Markdown",
        )
        bot.register_next_step_handler(msg, step_get_username)

    # --- ТЕСТ 1: ИЗМЕНИТЬ "О СЕБЕ" ---
    elif call.data == "change_bio":
        msg = bot.send_message(
            chat_id,
            "✏️ Напишите любой текст, который нужно поставить в графу **«О себе»** в вашем профиле Steam:",
            parse_mode="Markdown",
        )
        bot.register_next_step_handler(msg, step_change_bio)

    # --- ТЕСТ 2: ДОБАВИТЬ ИГРУ ИЗ FREE TO PLAY ---
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
            "🎮 Выберите любую игру из списка Free-to-Play, чтобы бот прямо сейчас добавил её в вашу библиотеку Steam:",
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
            bot.answer_callback_query(call.id, "Добавляю игру в библиотеку...")
            add_f2p_game(chat_id, target)

    # --- ПРОВЕРКА РАЗДАЧ ---
    elif call.data == "check_now":
        bot.answer_callback_query(call.id, "Проверяю раздачи...")
        count = check_giveaways()
        if count == 0:
            bot.send_message(
                chat_id,
                "ℹ️ На данный момент новых платных игр со 100% скидкой нет (или все текущие уже добавлены).",
                reply_markup=get_main_keyboard(),
            )

    # --- ПРОВЕРКА РАБОТОСПОСОБНОСТИ ---
    elif call.data == "health_check":
        ok = steam.check_health()
        uptime_min = int((time.time() - bot_status["start_time"]) // 60)
        text = (
            f"💓 **Проверка работоспособности скрипта**\n\n"
            f"🟢 Время непрерывной работы: `{uptime_min} мин.`\n"
            f"🔐 Авторизация Steam: `{'Работает (OK)' if ok else 'Ошибка сессии'}`\n"
            f"🌐 Адрес самопинга: `{bot_status['last_ping_url']}`\n"
            f"📡 Последний пинг: `{bot_status['last_ping_time']}` (Ответ: `{bot_status['last_ping_code']}`)\n"
            f"🔍 Последняя проверка раздач: `{bot_status['last_check']}`\n"
            f"🎁 Добавлено игр за сеанс: `{bot_status['claimed_count']}`"
        )
        bot.send_message(chat_id, text, parse_mode="Markdown", reply_markup=get_main_keyboard())

    # --- НАСТРОЙКА ССЫЛКИ RENDER ---
    elif call.data == "set_render_url":
        msg = bot.send_message(
            chat_id,
            "🌐 Отправьте ссылку на ваш проект на Render (например `https://my-steam-bot.onrender.com`):",
            parse_mode="Markdown",
        )
        bot.register_next_step_handler(msg, step_set_render_url)

    # --- ЭКСПОРТ СЕССИИ ---
    elif call.data == "get_b64":
        b64 = export_session_b64(steam.data)
        bot.send_message(
            chat_id,
            "💾 **Сохранение сессии для Render.com**\n\n"
            "Чтобы сессия не слетала даже при полном пересоздании сервера на Render, добавьте в настройки Render (вкладка **Environment**) переменную:\n"
            "Имя: `SESSION_BASE64`\n"
            f"Значение:\n`{b64}`",
            parse_mode="Markdown",
            reply_markup=get_main_keyboard(),
        )

    elif call.data == "back_main":
        cmd_start(call.message)


# ----------------- ПОШАГОВАЯ АВТОРИЗАЦИЯ STEAM В ТГ -----------------
def step_get_username(message):
    username = message.text.strip()
    pending_auth[message.chat.id] = {"username": username}
    msg = bot.send_message(
        message.chat.id,
        f"🔒 **Шаг 2 из 3.** Теперь отправьте **пароль** от аккаунта `{username}`.\n"
        f"*(Для безопасности бот сразу удалит сообщение с паролем из истории чата)*:",
        parse_mode="Markdown",
    )
    bot.register_next_step_handler(msg, step_get_password)


def step_get_password(message):
    chat_id = message.chat.id
    password = message.text.strip()
    username = pending_auth.get(chat_id, {}).get("username", "")

    # Удаляем сообщение с паролем из чата для безопасности
    try:
        bot.delete_message(chat_id, message.message_id)
    except Exception:
        pass

    wait_msg = bot.send_message(chat_id, "⏳ Отправляю запрос авторизации в Steam...")
    res = steam.start_tg_login(username, password)

    if not res.get("ok"):
        bot.edit_message_text(
            f"❌ **Ошибка входа:** {res.get('error')}\nПопробуйте снова через меню.",
            chat_id,
            wait_msg.message_id,
            parse_mode="Markdown",
            reply_markup=get_main_keyboard(),
        )
        return

    pending_auth[chat_id] = res
    msg = bot.send_message(
        chat_id,
        "📲 **Шаг 3 из 3. Запрос в Steam отправлен!**\n\n"
        "• Введите **5-значный код Steam Guard** (из мобильного приложения или с почты).\n"
        "• *ИЛИ* если в мобильном приложении Steam всплыло окно подтверждения, нажмите **«Подтвердить»** в приложении, а сюда отправьте слово `ок`:",
        parse_mode="Markdown",
    )
    bot.register_next_step_handler(msg, step_get_guard_code)


def step_get_guard_code(message):
    chat_id = message.chat.id
    code = message.text.strip()
    auth_state = pending_auth.get(chat_id)

    if not auth_state:
        bot.send_message(chat_id, "⚠️ Сессия ввода истекла, начните вход заново.", reply_markup=get_main_keyboard())
        return

    bot.send_message(chat_id, "⏳ Проверяю подтверждение и сохраняю сессию...")
    if steam.finish_tg_login(auth_state, guard_code=code):
        pending_auth.pop(chat_id, None)
        bot.send_message(
            chat_id,
            f"✅ **Успешный вход в аккаунт `{steam.data.get('account_name')}`!**\n"
            f"Сессия сохранена.\n\n"
            f"👇 **Что сделать сейчас для проверки:**\n"
            f"1. Нажмите кнопку **«1. Изменить О себе в Steam»** и напишите любой текст — если он появился в вашем профиле, значит бот работает.\n"
            f"2. Нажмите **«2. Добавить игру из Free-to-Play»**, чтобы убедиться, что бот умеет добавлять игры в вашу библиотеку.",
            parse_mode="Markdown",
            reply_markup=get_main_keyboard(),
        )
    else:
        bot.send_message(
            chat_id,
            "❌ Неверный код Steam Guard или время ожидания подтверждения истекло. Попробуйте войти еще раз.",
            reply_markup=get_main_keyboard(),
        )


# ----------------- ДЕЙСТВИЯ ПРОВЕРКИ -----------------
def step_change_bio(message):
    new_bio = message.text.strip()
    bot.send_message(message.chat.id, "⏳ Меняю описание в профиле Steam...")
    if steam.update_bio(new_bio):
        profile_url = f"https://steamcommunity.com/profiles/{steam.data['steamid']}"
        bot.send_message(
            message.chat.id,
            f"✅ **Готово! Графа «О себе» успешно изменена!**\n\n"
            f"Новый текст: `{new_bio}`\n"
            f"🔗 [Проверить в профиле Steam]({profile_url})\n\n"
            f"Теперь нажмите **«2. Добавить игру из Free-to-Play»** для проверки добавления игр!",
            parse_mode="Markdown",
            reply_markup=get_main_keyboard(),
        )
    else:
        bot.send_message(
            message.chat.id,
            "❌ Ошибка при изменении описания профиля. Проверьте статус сессии.",
            reply_markup=get_main_keyboard(),
        )


def step_custom_f2p(message):
    app_id = re.sub(r"\D", "", message.text.strip())
    if not app_id:
        bot.send_message(message.chat.id, "❌ Нужно ввести только цифры AppID.", reply_markup=get_main_keyboard())
        return
    add_f2p_game(message.chat.id, app_id)


def add_f2p_game(chat_id, app_id):
    bot.send_message(chat_id, f"⏳ Получаю лицензию для игры (AppID: `{app_id}`)...", parse_mode="Markdown")
    sub_id = steam.get_subid_from_appid(app_id)
    if not sub_id:
        bot.send_message(
            chat_id,
            f"❌ Не удалось найти бесплатный пакет для AppID `{app_id}`.",
            parse_mode="Markdown",
            reply_markup=get_main_keyboard(),
        )
        return

    if steam.add_free_license(sub_id):
        bot_status["claimed_count"] += 1
        bot.send_message(
            chat_id,
            f"✅ **Проверка пройдена! Игра добавлена в вашу библиотеку Steam!**\n\n"
            f"📦 AppID: `{app_id}` | SubID: `{sub_id}`\n"
            f"🔗 https://store.steampowered.com/app/{app_id}/\n\n"
            f"Бот полностью исправен и теперь каждые 10 часов будет автоматически забирать временно бесплатные игры.",
            parse_mode="Markdown",
            reply_markup=get_main_keyboard(),
        )
    else:
        bot.send_message(
            chat_id,
            f"❌ Не удалось активировать лицензию `{sub_id}`.",
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
        f"✅ Ссылка для самопинга сохранена: `{url}/ping`\n"
        f"Бот будет пинговать её каждые 3–5 минут.",
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
    except Exception as e:
        print(f"[!] Ошибка обращения к API раздач: {e}")
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
                            f"🎁 **Найдена и добавлена временно бесплатная игра!**\n\n"
                            f"🎮 **{title}**\n"
                            f"📦 AppID: `{app_id}` (SubID: `{sub_id}`)\n"
                            f"🔗 https://store.steampowered.com/app/{app_id}/",
                            parse_mode="Markdown",
                        )
                    time.sleep(3)
            except Exception as e:
                print(f"[!] Ошибка добавления {title}: {e}")

    return added


def giveaway_loop():
    while True:
        try:
            if steam.is_configured():
                check_giveaways()
        except Exception as e:
            print(f"[!] Ошибка в цикле проверки игр: {e}")
        time.sleep(CHECK_INTERVAL)


# ----------------- ВЕБ-СЕРВЕР И САМОПИНГ (КАЖДЫЕ 3-5 МИНУТ) -----------------
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
    """Пингует внешний URL Render каждые 3-5 минут, не давая контейнеру уснуть."""
    while True:
        # Случайная пауза от 3 до 5 минут (180 - 300 секунд)
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
            print(f"[Self-Ping] Успешно: {ping_url} -> {r.status_code}")
        except Exception as e:
            bot_status["last_ping_time"] = time.strftime("%H:%M:%S")
            bot_status["last_ping_code"] = f"Ошибка ({e})"
            print(f"[Self-Ping] Ошибка: {e}")


if __name__ == "__main__":
    # 1. Запускаем цикл проверки раздач (каждые 10 часов)
    threading.Thread(target=giveaway_loop, daemon=True).start()

    # 2. Запускаем цикл самопинга (каждые 3-5 минут)
    threading.Thread(target=keep_alive_pinger, daemon=True).start()

    # 3. Запускаем опрос Telegram-бота
    threading.Thread(target=lambda: bot.infinity_polling(skip_pending=True), daemon=True).start()

    # 4. Запускаем веб-сервер Flask на порту Render
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
