import base64
import html
import io
import json
import os
import random
import re
import secrets
import shutil
import subprocess
import sys
import threading
import time
from flask import Flask, jsonify, redirect, render_template_string, request, session, url_for
import requests
import rsa
import telebot
from telebot.types import InlineKeyboardButton, InlineKeyboardMarkup

TOK_FILE = "tok.json"
DEFAULT_RENDER_URL = "https://myhub-yhxu.onrender.com"


def load_tok_config():
    cfg = {
        "tg_bot_token": os.environ.get("TG_BOT_TOKEN", "ВСТАВЬТЕ_СЮДА_ТОКЕН_БОТА"),
        "render_url": os.environ.get("RENDER_EXTERNAL_URL", DEFAULT_RENDER_URL),
        "web_password": os.environ.get("WEB_PASSWORD", "admin123"),  # Пароль для входа на сайт
    }
    if os.path.exists(TOK_FILE):
        try:
            with open(TOK_FILE, "r", encoding="utf-8") as f:
                file_cfg = json.load(f)
                if file_cfg.get("tg_bot_token") and "ВСТАВЬТЕ" not in file_cfg.get("tg_bot_token", ""):
                    cfg["tg_bot_token"] = file_cfg["tg_bot_token"]
                if file_cfg.get("render_url"):
                    cfg["render_url"] = file_cfg["render_url"]
                if file_cfg.get("web_password"):
                    cfg["web_password"] = file_cfg["web_password"]
        except Exception:
            pass
    return cfg


tok_config = load_tok_config()
TG_BOT_TOKEN = tok_config["tg_bot_token"]

SESSION_FILE = "session.json"
CLAIMED_FILE = "claimed_games.json"
BACKUP_FILE = "bot.py.bak"
CHECK_GIVEAWAYS_INTERVAL = 10 * 3600
CHECK_DISCOUNTS_INTERVAL = 1800

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

bot_status = {
    "start_time": time.time(),
    "last_ping_ts": 0,
    "last_ping_time": "Еще не выполнялся",
    "last_ping_url": f"{tok_config['render_url']}/ping",
    "last_ping_code": "—",
    "ping_count": 0,
    "ping_history": [],
    "last_check": "Еще не выполнялась",
    "last_discount_check": "Еще не выполнялась",
    "steam_ok": False,
    "claimed_count": 0,
    "autobuy_tasks": set(),
}

pending_auth = {}
calc_state = {}
library_cache = {}
update_sessions = {}


def load_session():
    if os.path.exists(SESSION_FILE):
        try:
            with open(SESSION_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                if data:
                    return data
        except Exception:
            pass
    env_b64 = os.environ.get("SESSION_BASE64")
    if env_b64:
        try:
            raw = base64.b64decode(env_b64.strip()).decode("utf-8")
            data = json.loads(raw)
            with open(SESSION_FILE, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
            return data
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


class SteamClient:
    def __init__(self):
        self.data = load_session()
        if "session_id" not in self.data:
            self.data["session_id"] = secrets.token_hex(12)
        if "render_url" not in self.data:
            self.data["render_url"] = tok_config["render_url"]
        if "watchlist" not in self.data:
            self.data["watchlist"] = {}
        if "silent_mode" not in self.data:
            self.data["silent_mode"] = False

    @property
    def session_id(self):
        return self.data["session_id"]

    def is_configured(self):
        return bool(self.data.get("refresh_token") and self.data.get("steamid"))

    def atomic_login(self, username, password, guard_code):
        try:
            rsa_url = "https://api.steampowered.com/IAuthenticationService/GetPasswordRSAPublicKey/v1/"
            r = requests.get(rsa_url, params={"account_name": username}, headers={"User-Agent": USER_AGENT}, timeout=15).json()
            resp = r.get("response", {})
            if not resp.get("publickey_mod"):
                return {"ok": False, "error": "Не удалось получить RSA-ключ Steam."}
            mod = int(resp["publickey_mod"], 16)
            exp = int(resp["publickey_exp"], 16)
            timestamp = resp["timestamp"]
            pub_key = rsa.PublicKey(mod, exp)
            encrypted_pw = base64.b64encode(rsa.encrypt(password.encode("utf-8"), pub_key)).decode("utf-8")
            begin_url = "https://api.steampowered.com/IAuthenticationService/BeginAuthSessionViaCredentials/v1/"
            b_res = requests.post(begin_url, data={
                "account_name": username, "encrypted_password": encrypted_pw,
                "encryption_timestamp": timestamp, "remember_login": "true",
                "platform_type": "2", "website_id": "Store", "persistence": "1",
                "device_friendly_name": "Chrome (Windows)"
            }, headers={"User-Agent": USER_AGENT}, timeout=15).json().get("response", {})
            client_id = b_res.get("client_id")
            request_id = b_res.get("request_id")
            steamid = b_res.get("steamid")
            if not client_id or not steamid:
                return {"ok": False, "error": "Неверный логин или пароль."}
            confirmations = b_res.get("allowed_confirmations", [])
            conf_types = [c.get("confirmation_type") for c in confirmations]
            code_type = 3 if 3 in conf_types else (2 if 2 in conf_types else 3)
            requests.post("https://api.steampowered.com/IAuthenticationService/UpdateAuthSessionWithSteamGuardCode/v1/", data={
                "client_id": client_id, "steamid": steamid, "code": guard_code.strip().upper(), "code_type": code_type
            }, headers={"User-Agent": USER_AGENT}, timeout=15)
            poll_url = "https://api.steampowered.com/IAuthenticationService/PollAuthSessionStatus/v1/"
            for _ in range(5):
                p_res = requests.post(poll_url, data={"client_id": client_id, "request_id": request_id}, headers={"User-Agent": USER_AGENT}, timeout=15).json().get("response", {})
                if p_res.get("refresh_token") and p_res.get("access_token"):
                    self.data.update({
                        "steamid": str(steamid), "account_name": username,
                        "refresh_token": p_res["refresh_token"], "access_token": p_res["access_token"],
                        "session_id": secrets.token_hex(12)
                    })
                    save_session(self.data)
                    bot_status["steam_ok"] = True
                    return {"ok": True}
                time.sleep(1)
            return {"ok": False, "error": "Код отклонен или IP заблокирован."}
        except Exception as e:
            return {"ok": False, "error": f"Ошибка: {e}"}

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
        try:
            r = requests.post(url, data={"refresh_token": self.data["refresh_token"], "steamid": self.data["steamid"], "renewal_type": 0}, timeout=15).json().get("response", {})
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

    def get_wallet_balance(self):
        if not self.is_configured():
            return "Не авторизован"
        try:
            r = requests.get("https://store.steampowered.com/account/", cookies=self.get_cookies(), headers={"User-Agent": USER_AGENT}, allow_redirects=False, timeout=15)
            if r.status_code == 200:
                bot_status["steam_ok"] = True
                m = re.search(r'class="accountData price"[^>]*>(.*?)</div>', r.text, re.DOTALL)
                if not m:
                    m = re.search(r'id="header_wallet_balance"[^>]*>(.*?)</a>', r.text, re.DOTALL)
                if m:
                    return html.unescape(re.sub(r"<[^>]+>", "", m.group(1)).strip())
                return "0 ₸"
            bot_status["steam_ok"] = False
        except Exception:
            bot_status["steam_ok"] = False
        return "Неизвестно"

    def check_health(self):
        bal = self.get_wallet_balance()
        return bot_status["steam_ok"], bal

    def update_profile_field(self, summary=None, persona_name=None, real_name=None):
        steamid = self.data["steamid"]
        cookies = self.get_cookies()
        headers = {"User-Agent": USER_AGENT, "Referer": f"https://steamcommunity.com/profiles/{steamid}/edit/info"}
        cur_persona, cur_real, cur_url, cur_summary = self.data.get("account_name", "Player"), "", "", ""
        try:
            info_resp = requests.get(f"https://steamcommunity.com/profiles/{steamid}/edit/info", cookies=cookies, headers=headers, timeout=15)
            match = re.search(r'data-profile-edit="([^"]+)"', info_resp.text)
            if match:
                p_json = json.loads(html.unescape(match.group(1)))
                cur_persona = p_json.get("strPersonaName", cur_persona)
                cur_real = p_json.get("strRealName", "")
                cur_url = p_json.get("strCustomURL", "")
                cur_summary = p_json.get("strSummary", "")
        except Exception:
            pass
        r = requests.post(f"https://steamcommunity.com/profiles/{steamid}/edit/", data={
            "sessionID": self.session_id, "type": "profileSave",
            "personaName": persona_name if persona_name is not None else cur_persona,
            "real_name": real_name if real_name is not None else cur_real,
            "customURL": cur_url, "summary": summary if summary is not None else cur_summary, "json": "1"
        }, cookies=cookies, headers=headers, timeout=15)
        return r.status_code == 200

    def get_all_licenses(self):
        url = "https://store.steampowered.com/account/licenses/?l=russian"
        try:
            r = requests.get(url, cookies=self.get_cookies(), headers={"User-Agent": USER_AGENT}, timeout=15)
            if r.status_code != 200:
                return []
            items = []
            rows = re.findall(r"<tr[^>]*>(.*?)</tr>", r.text, re.DOTALL)
            for row in rows:
                tds = re.findall(r"<td[^>]*>(.*?)</td>", row, re.DOTALL)
                if len(tds) < 3:
                    continue
                date_str = re.sub(r"<[^>]+>", "", tds[0]).strip()
                acq_method = re.sub(r"<[^>]+>", "", tds[2]).strip()
                m = re.search(r"RemoveFreeLicense\(\s*(\d+)\s*,\s*'([^']*)'", tds[1])
                removable = bool(m)
                pkg_id = m.group(1) if m else "0"
                td_item_clean = re.sub(r"<div.*?</div>", "", tds[1], flags=re.DOTALL)
                title = html.unescape(re.sub(r"<[^>]+>", "", td_item_clean)).strip()
                if not title and m:
                    try:
                        title = base64.b64decode(m.group(2)).decode("utf-8").strip()
                    except Exception:
                        title = f"Package {pkg_id}"
                if title:
                    items.append({"packageid": pkg_id, "name": title[:42], "date": date_str, "method": acq_method, "removable": removable})
            return items
        except Exception:
            return []

    def get_purchase_history(self):
        url = "https://store.steampowered.com/account/history/?l=russian"
        try:
            r = requests.get(url, cookies=self.get_cookies(), headers={"User-Agent": USER_AGENT}, timeout=15)
            if r.status_code != 200:
                return []
            records = []
            rows = re.findall(r'<tr class="wallet_table_row[^>]*>(.*?)</tr>', r.text, re.DOTALL)
            for row in rows[:10]:
                date_m = re.search(r'class="wht_date"[^>]*>(.*?)</td>', row, re.DOTALL)
                items_m = re.search(r'class="wht_items"[^>]*>(.*?)</td>', row, re.DOTALL)
                total_m = re.search(r'class="wht_total"[^>]*>(.*?)</td>', row, re.DOTALL)
                date_s = html.unescape(re.sub(r"<[^>]+>", "", date_m.group(1)).strip()) if date_m else "—"
                item_s = html.unescape(re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", items_m
