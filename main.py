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
from flask import (
    Flask,
    jsonify,
    redirect,
    render_template_string,
    request,
    send_file,
    session,
    url_for,
)
import requests
import rsa
import telebot
from telebot.types import InlineKeyboardButton, InlineKeyboardMarkup

# ----------------- ЗАГРУЗКА НАСТРОЕК ИЗ TOK.JSON -----------------
TOK_FILE = "tok.json"
DEFAULT_RENDER_URL = "https://myhub-yhxu.onrender.com"


def load_tok_config():
    cfg = {
        "tg_bot_token": os.environ.get("TG_BOT_TOKEN", "ВСТАВЬТЕ_СЮДА_ТОКЕН_БОТА"),
        "render_url": os.environ.get("RENDER_EXTERNAL_URL", DEFAULT_RENDER_URL),
        "web_password": os.environ.get("WEB_PASSWORD", "admin123"),
        "gh_token": os.environ.get("GH_TOKEN", ""),
        "gh_repo": os.environ.get("GH_REPO", ""),
        "gh_path": os.environ.get("GH_PATH", os.path.basename(os.path.abspath(__file__))),
        "gh_branch": os.environ.get("GH_BRANCH", "main"),
    }
    if os.path.exists(TOK_FILE):
        try:
            with open(TOK_FILE, "r", encoding="utf-8") as f:
                file_cfg = json.load(f)
                tok = file_cfg.get("tg_bot_token", "")
                if tok and "ВСТАВЬТЕ" not in tok:
                    cfg["tg_bot_token"] = tok
                for k in ("render_url", "web_password", "gh_token", "gh_repo", "gh_path", "gh_branch"):
                    if file_cfg.get(k):
                        cfg[k] = file_cfg[k]
        except Exception as e:
            print(f"[!] Ошибка чтения {TOK_FILE}: {e}")
    else:
        save_tok_config(cfg)
    return cfg


def save_tok_config(cfg):
    try:
        with open(TOK_FILE, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2, ensure_ascii=False)
    except Exception:
        pass


tok_config = load_tok_config()
TG_BOT_TOKEN = tok_config["tg_bot_token"]

SESSION_FILE = "session.json"
CLAIMED_FILE = "claimed_games.json"
CLAIMED_F2P_FILE = "claimed_f2p_apps.json"
BACKUP_FILE = "bot.py.bak"

CHECK_GIVEAWAYS_INTERVAL = 10 * 3600  # Проверка бесплатных раздач каждые 10 часов
CHECK_DISCOUNTS_INTERVAL = 900        # Проверка скидок и автопокупок каждые 15 минут
F2P_FARM_INTERVAL = 900               # Авто-сбор всех бесплатных F2P игр каждые 15 минут

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
    "ping_interval_min": 4,
    "last_check": "Еще не выполнялась",
    "last_discount_check": "Еще не выполнялась",
    "last_f2p_check": "Еще не выполнялась",
    "steam_ok": False,
    "claimed_count": 0,
    "f2p_added_count": 0,
    "f2p_auto_all_enabled": False,
    "f2p_current_page": 0,
    "autobuy_tasks": set(),
    "last_gh_push": "Еще не выполнялся",
    "web_msg": "",
}

pending_auth = {}
calc_state = {}
library_cache = {}
web_search_cache = []
web_stat_cache = ""

update_lock = threading.Lock()
update_sessions = {}
UPDATE_TRIGGER_CODES = {"1111", "0000", "обновить", "готово", "ok", "ок", "/apply", "update"}


# ----------------- СЕССИЯ И ЛОКАЛЬНЫЕ БАЗЫ -----------------
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
        except Exception as e:
            print(f"[!] Ошибка чтения SESSION_BASE64: {e}")
    return {}


def save_session(data):
    with open(SESSION_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def export_session_b64(data):
    raw = json.dumps(data, ensure_ascii=False)
    return base64.b64encode(raw.encode("utf-8")).decode("utf-8")


def load_json_set(filepath):
    if os.path.exists(filepath):
        try:
            with open(filepath, "r", encoding="utf-8") as f:
                return set(json.load(f))
        except Exception:
            return set()
    return set()


def save_json_set(filepath, data_set):
    with open(filepath, "w", encoding="utf-8") as f:
        json.dump(list(data_set), f, indent=2, ensure_ascii=False)


def strip_html(raw_text):
    if not raw_text:
        return ""
    clean = re.sub(r"<[^>]+>", " ", str(raw_text))
    return html.unescape(re.sub(r"\s+", " ", clean).strip())


# ----------------- ПРЯМАЯ ОТПРАВКА ОБНОВЛЕНИЙ В GITHUB -----------------
def push_code_to_github(new_code_str):
    """
    Отправляет обновленный файл напрямую в репозиторий GitHub через REST API.
    Возвращает (успех: bool, описание: str).
    """
    gh_token = (
        steam.data.get("gh_token")
        or tok_config.get("gh_token")
        or os.environ.get("GH_TOKEN", "")
    ).strip()
    gh_repo = (
        steam.data.get("gh_repo")
        or tok_config.get("gh_repo")
        or os.environ.get("GH_REPO", "")
    ).strip()
    gh_path = (
        steam.data.get("gh_path")
        or tok_config.get("gh_path")
        or os.path.basename(os.path.abspath(__file__))
    ).strip()
    gh_branch = (
        steam.data.get("gh_branch")
        or tok_config.get("gh_branch")
        or "main"
    ).strip()

    if not gh_token or not gh_repo or "ваш_логин" in gh_repo:
        return False, "GitHub токен или репозиторий еще не указаны (обновлено только локально)"

    gh_repo = gh_repo.replace("https://github.com/", "").strip("/")
    api_url = f"https://api.github.com/repos/{gh_repo}/contents/{gh_path}"
    headers = {
        "Authorization": f"token {gh_token}",
        "Accept": "application/vnd.github.v3+json",
        "User-Agent": "SteamUltimateBot-Updater",
    }

    try:
        # 1. Получаем текущий SHA файла в репозитории (обязательно для перезаписи в GitHub API)
        get_r = requests.get(
            api_url, params={"ref": gh_branch}, headers=headers, timeout=15
        )
        file_sha = None
        if get_r.status_code == 200:
            file_sha = get_r.json().get("sha")
        elif get_r.status_code == 404:
            # Если ветка master вместо main, пробуем без привязки к ветке
            get_r2 = requests.get(api_url, headers=headers, timeout=15)
            if get_r2.status_code == 200:
                file_sha = get_r2.json().get("sha")
                gh_branch = ""

        b64_content = base64.b64encode(new_code_str.encode("utf-8")).decode("utf-8")
        payload = {
            "message": f"Auto-update {gh_path} via Steam Bot ({time.strftime('%d.%m %H:%M')})",
            "content": b64_content,
        }
        if file_sha:
            payload["sha"] = file_sha
        if gh_branch:
            payload["branch"] = gh_branch

        put_r = requests.put(api_url, headers=headers, json=payload, timeout=25)
        if put_r.status_code in (200, 201):
            commit_sha = put_r.json().get("commit", {}).get("sha", "")[:7]
            bot_status["last_gh_push"] = f"OK ({commit_sha}) в {time.strftime('%H:%M:%S')}"
            return True, f"✅ Файл `{gh_path}` успешно отправлен в GitHub (`{gh_repo}`, коммит `{commit_sha}`)"
        else:
            err_msg = put_r.json().get("message", f"HTTP {put_r.status_code}")
            bot_status["last_gh_push"] = f"Ошибка: {err_msg}"
            return False, f"⚠️ Ошибка GitHub API ({put_r.status_code}): {err_msg}"
    except Exception as e:
        bot_status["last_gh_push"] = f"Ошибка: {e}"
        return False, f"⚠️ Ошибка соединения с GitHub: {e}"


# ----------------- КЛИЕНТ STEAM -----------------
class SteamClient:
    def __init__(self):
        self.data = load_session()
        if "session_id" not in self.data:
            self.data["session_id"] = secrets.token_hex(12)
        if "render_url" not in self.data:
            self.data["render_url"] = tok_config["render_url"]
        if "watchlist" not in self.data:
            self.data["watchlist"] = {}
        if "notes" not in self.data:
            self.data["notes"] = []
        if "silent_mode" not in self.data:
            self.data["silent_mode"] = False
        if "f2p_auto_all" in self.data:
            bot_status["f2p_auto_all_enabled"] = bool(self.data["f2p_auto_all"])

    @property
    def session_id(self):
        return self.data["session_id"]

    def is_configured(self):
        return bool(self.data.get("refresh_token") and self.data.get("steamid"))

    def atomic_login(self, username, password, guard_code):
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
            enc_pw = base64.b64encode(
                rsa.encrypt(password.encode("utf-8"), pub_key)
            ).decode("utf-8")

            begin_url = "https://api.steampowered.com/IAuthenticationService/BeginAuthSessionViaCredentials/v1/"
            begin_payload = {
                "account_name": username,
                "encrypted_password": enc_pw,
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

            confs = b_res.get("allowed_confirmations", [])
            conf_types = [c.get("confirmation_type") for c in confs]
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
                "error": "Код отклонен или IP заблокирован. Используйте SESSION_BASE64.",
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
        sid = self.data.get("steamid", "")
        tok = self.data.get("access_token", "")
        return {
            "sessionid": self.session_id,
            "steamLoginSecure": f"{sid}%7C%7C{tok}",
            "birthtime": "283993201",
            "lastagecheckage": "1-0-1979",
        }

    def get_wallet_balance(self):
        if not self.is_configured():
            return "Не авторизован"
        try:
            r = requests.get(
                "https://store.steampowered.com/account/",
                cookies=self.get_cookies(),
                headers={"User-Agent": USER_AGENT},
                allow_redirects=False,
                timeout=15,
            )
            if r.status_code == 200:
                bot_status["steam_ok"] = True
                m = re.search(
                    r'class="accountData price"[^>]*>(.*?)</div>', r.text, re.DOTALL
                )
                if not m:
                    m = re.search(
                        r'id="header_wallet_balance"[^>]*>(.*?)</a>', r.text, re.DOTALL
                    )
                if m:
                    return strip_html(m.group(1))
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
        edit_url = f"https://steamcommunity.com/profiles/{steamid}/edit/info"
        headers = {"User-Agent": USER_AGENT, "Referer": edit_url}

        cur_persona = self.data.get("account_name", "Player")
        cur_real, cur_url, cur_summary = "", "", ""
        try:
            info_resp = requests.get(edit_url, cookies=cookies, headers=headers, timeout=15)
            match = re.search(r'data-profile-edit="([^"]+)"', info_resp.text)
            if match:
                p_json = json.loads(html.unescape(match.group(1)))
                cur_persona = p_json.get("strPersonaName", cur_persona)
                cur_real = p_json.get("strRealName", "")
                cur_url = p_json.get("strCustomURL", "")
                cur_summary = p_json.get("strSummary", "")
        except Exception:
            pass

        payload = {
            "sessionID": self.session_id,
            "type": "profileSave",
            "personaName": persona_name if persona_name is not None else cur_persona,
            "real_name": real_name if real_name is not None else cur_real,
            "customURL": cur_url,
            "summary": summary if summary is not None else cur_summary,
            "json": "1",
        }
        r = requests.post(
            f"https://steamcommunity.com/profiles/{steamid}/edit/",
            data=payload,
            cookies=cookies,
            headers=headers,
            timeout=15,
        )
        return r.status_code == 200

    def get_all_licenses(self):
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
                tds = re.findall(r"<td[^>]*>(.*?)</td>", row, re.DOTALL)
                if len(tds) < 3:
                    continue

                date_str = strip_html(tds[0])
                acq_method = strip_html(tds[2])
                m = re.search(r"RemoveFreeLicense\(\s*(\d+)\s*,\s*'([^']*)'", tds[1])
                removable = bool(m)
                pkg_id = m.group(1) if m else "0"

                td_clean = re.sub(r"<div.*?</div>", "", tds[1], flags=re.DOTALL)
                title = strip_html(td_clean)
                if not title and m:
                    try:
                        title = base64.b64decode(m.group(2)).decode("utf-8").strip()
                    except Exception:
                        title = f"Package {pkg_id}"

                if title:
                    items.append({
                        "packageid": pkg_id,
                        "name": title[:45],
                        "date": date_str,
                        "method": acq_method,
                        "removable": removable,
                    })
            return items
        except Exception:
            return []

    def get_purchase_history(self):
        url = "https://store.steampowered.com/account/history/?l=russian"
        try:
            r = requests.get(
                url,
                cookies=self.get_cookies(),
                headers={"User-Agent": USER_AGENT},
                timeout=15,
            )
            if r.status_code != 200:
                return []

            records = []
            rows = re.findall(
                r'<tr class="wallet_table_row[^>]*>(.*?)</tr>', r.text, re.DOTALL
            )
            for row in rows[:12]:
                dm = re.search(r'class="wht_date"[^>]*>(.*?)</td>', row, re.DOTALL)
                im = re.search(r'class="wht_items"[^>]*>(.*?)</td>', row, re.DOTALL)
                tm = re.search(r'class="wht_total"[^>]*>(.*?)</td>', row, re.DOTALL)

                date_s = strip_html(dm.group(1)) if dm else "—"
                item_s = strip_html(im.group(1)) if im else "Операция"
                total_s = strip_html(tm.group(1)) if tm else "0 ₸"
                records.append(f"📅 `{date_s}` — **{item_s[:38]}** (`{total_s}`)")
            return records
        except Exception:
            return []

    def redeem_cd_key(self, key_str):
        url = "https://store.steampowered.com/account/ajaxregisterkey/"
        headers = {
            "User-Agent": USER_AGENT,
            "Referer": "https://store.steampowered.com/account/registerkey",
            "X-Requested-With": "XMLHttpRequest",
        }
        payload = {"product_key": key_str.strip(), "sessionid": self.session_id}
        try:
            r = requests.post(
                url,
                data=payload,
                cookies=self.get_cookies(),
                headers=headers,
                timeout=15,
            ).json()
            success = r.get("success") == 1
            receipt = r.get("purchase_receipt_info", {})
            items = receipt.get("line_items", [])
            game_names = ", ".join([i.get("line_item_description", "Игра") for i in items])
            return {"ok": success, "games": game_names, "raw": r}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def remove_license(self, package_id):
        url = "https://store.steampowered.com/account/removelicense"
        headers = {
            "User-Agent": USER_AGENT,
            "Referer": "https://store.steampowered.com/account/licenses/",
            "X-Requested-With": "XMLHttpRequest",
        }
        payload = {"sessionid": self.session_id, "packageid": str(package_id)}
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
        except Exception:
            pass
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

    # --- НОВЫЕ 12 ФУНКЦИЙ И КАТАЛОГ FREE-TO-PLAY ---
    def fetch_f2p_catalog_page(self, start_offset=0, count=50):
        url = "https://store.steampowered.com/search/results/"
        params = {
            "query": "",
            "start": start_offset,
            "count": count,
            "maxprice": "free",
            "category1": "998",
            "infinite": "1",
            "cc": "kz",
            "l": "russian",
        }
        try:
            r = requests.get(
                url, params=params, headers={"User-Agent": USER_AGENT}, timeout=15
            ).json()
            html_block = r.get("results_html", "")
            matches = re.findall(
                r'data-ds-appid="(\d+)".*?<span class="title">(.*?)</span>',
                html_block,
                re.DOTALL,
            )
            results = []
            for appid, title in matches:
                results.append({
                    "appid": appid.strip(),
                    "name": html.unescape(title.strip()),
                })
            return results
        except Exception:
            return []

    def add_random_f2p_batch(self, target_count=5, sequential=False):
        claimed_f2p = load_json_set(CLAIMED_F2P_FILE)
        if sequential:
            offset = (bot_status["f2p_current_page"] * 50) % 2500
            bot_status["f2p_current_page"] += 1
        else:
            offset = random.choice([0, 50, 100, 150, 200, 300, 400, 500, 700, 900, 1200])

        candidates = self.fetch_f2p_catalog_page(start_offset=offset, count=50)
        if not candidates:
            candidates = self.fetch_f2p_catalog_page(start_offset=0, count=50)

        if not sequential:
            random.shuffle(candidates)

        added_games = []
        for game in candidates:
            if len(added_games) >= target_count:
                break
            appid = game["appid"]
            if appid in claimed_f2p:
                continue

            sub_id = self.get_subid_from_appid(appid)
            if sub_id and self.add_free_license(sub_id):
                claimed_f2p.add(appid)
                added_games.append(f"• **{game['name']}** (`{appid}`)")
                bot_status["f2p_added_count"] += 1
                bot_status["claimed_count"] += 1
                time.sleep(1.5)

        save_json_set(CLAIMED_F2P_FILE, claimed_f2p)
        return added_games

    def get_live_player_count(self, app_id):
        url = f"https://api.steampowered.com/ISteamUserStats/GetNumberOfCurrentPlayers/v1/?appid={app_id}"
        try:
            r = requests.get(url, timeout=10).json()
            if r.get("response", {}).get("result") == 1:
                return r["response"].get("player_count", 0)
        except Exception:
            pass
        return None

    def get_game_news(self, app_id, count=3):
        url = f"https://api.steampowered.com/ISteamNews/GetNewsForApp/v2/?appid={app_id}&count={count}&maxlength=250"
        try:
            r = requests.get(url, timeout=10).json()
            items = r.get("appnews", {}).get("newsitems", [])
            news_list = []
            for it in items:
                title = it.get("title", "Новость")
                link = it.get("url", "")
                dt = time.strftime("%d.%m.%Y", time.localtime(it.get("date", time.time())))
                news_list.append(f"📰 `{dt}` — **{title}**\n🔗 {link}")
            return news_list
        except Exception:
            return []

    def compare_regional_prices(self, app_id):
        regions = [
            ("🇰🇿 Казахстан", "kz"),
            ("🇺🇸 США", "us"),
            ("🇺🇦 Украина", "ua"),
            ("🇷🇺 Россия", "ru"),
        ]
        results = []
        game_name = f"App {app_id}"
        for label, cc in regions:
            try:
                url = f"https://store.steampowered.com/api/appdetails?appids={app_id}&cc={cc}&l=ru"
                r = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=10).json()
                app_d = r.get(str(app_id), {})
                if app_d.get("success"):
                    data = app_d.get("data", {})
                    game_name = data.get("name", game_name)
                    if data.get("is_free"):
                        results.append(f"{label}: `Бесплатно`")
                        continue
                    po = data.get("price_overview", {})
                    if po:
                        fin = po.get("final_formatted", "Н/Д")
                        disc = po.get("discount_percent", 0)
                        d_str = f" (-{disc}%)" if disc > 0 else ""
                        results.append(f"{label}: **`{fin}`**{d_str}")
                    else:
                        results.append(f"{label}: `Недоступно`")
            except Exception:
                results.append(f"{label}: `Ошибка`")
        return game_name, results

    def add_free_dlcs_for_game(self, app_id):
        url = f"https://store.steampowered.com/api/appdetails?appids={app_id}&cc=kz&l=ru"
        added_dlcs = []
        try:
            r = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=15).json()
            data = r.get(str(app_id), {}).get("data", {})
            dlc_ids = data.get("dlc", [])[:15]
            for dlc_id in dlc_ids:
                info = self.get_game_price_info(dlc_id)
                if info and info.get("is_free"):
                    sub_id = info.get("subid") or self.get_subid_from_appid(dlc_id)
                    if sub_id and self.add_free_license(sub_id):
                        added_dlcs.append(f"• {info['name']} (`{dlc_id}`)")
                        time.sleep(1.2)
        except Exception:
            pass
        return added_dlcs

    def get_global_achievements(self, app_id):
        url = f"https://api.steampowered.com/ISteamUserStats/GetGlobalAchievementPercentagesForApp/v0002/?gameid={app_id}"
        try:
            r = requests.get(url, timeout=10).json()
            achs = r.get("achievementpercentages", {}).get("achievements", [])
            if not achs:
                return None
            achs_sorted = sorted(achs, key=lambda x: float(x.get("percent", 0)), reverse=True)
            return {
                "total": len(achs),
                "easiest": achs_sorted[:4],
                "rarest": achs_sorted[-4:],
            }
        except Exception:
            return None

    def get_wishlist_items(self):
        steamid = self.data.get("steamid")
        if not steamid:
            return []
        url = f"https://store.steampowered.com/wishlist/profiles/{steamid}/wishlistdata/?p=0&cc=kz&l=russian"
        try:
            r = requests.get(
                url,
                cookies=self.get_cookies(),
                headers={"User-Agent": USER_AGENT},
                timeout=15,
            )
            if r.status_code != 200:
                return []
            data = r.json()
            if not isinstance(data, dict):
                return []
            items = []
            for appid, info in list(data.items())[:15]:
                if not str(appid).isdigit():
                    continue
                name = info.get("name", f"App {appid}")
                subs = info.get("subs", [])
                disc = 0
                price_txt = "Без цены"
                if subs:
                    disc = subs[0].get("discount_pct", 0) or 0
                    raw_p = subs[0].get("price", 0)
                    if raw_p:
                        price_txt = f"{int(raw_p)/100:g} ₸"
                items.append({
                    "appid": str(appid),
                    "name": html.unescape(name),
                    "discount": disc,
                    "price": price_txt,
                })
            return items
        except Exception:
            return []

    def clear_steam_cart(self):
        cookies = self.get_cookies()
        cookies["shoppingCartGID"] = "-1"
        try:
            r = requests.post(
                "https://store.steampowered.com/cart/",
                data={"action": "remove_all", "sessionid": self.session_id},
                cookies=cookies,
                headers={"User-Agent": USER_AGENT},
                timeout=15,
            )
            return r.status_code == 200
        except Exception:
            return False

    def get_top_sellers_kz(self):
        url = "https://store.steampowered.com/api/featuredcategories?cc=kz&l=ru"
        try:
            r = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=15).json()
            tops = r.get("top_sellers", {}).get("items", [])[:8]
            out = []
            for t in tops:
                appid = str(t.get("id"))
                name = t.get("name", "Игра")
                disc = t.get("discount_percent", 0)
                final_kzt = t.get("final_price", 0) / 100.0
                out.append({
                    "appid": appid,
                    "name": name,
                    "discount": disc,
                    "price": f"{final_kzt:g} ₸" if final_kzt > 0 else "Бесплатно",
                })
            return out
        except Exception:
            return []

    def inspect_package_subid(self, sub_id):
        url = f"https://store.steampowered.com/api/packagedetails?packageids={sub_id}&cc=kz&l=ru"
        try:
            r = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=15).json()
            pkg = r.get(str(sub_id), {})
            if not pkg.get("success"):
                return None
            data = pkg.get("data", {})
            name = data.get("name", f"Package {sub_id}")
            apps = [a.get("name", "") for a in data.get("apps", [])]
            price = data.get("price", {})
            final_kzt = price.get("final", 0) / 100.0 if price else 0
            return {
                "subid": str(sub_id),
                "name": name,
                "apps": apps,
                "price_kzt": f"{final_kzt:g} ₸" if final_kzt > 0 else "Бесплатно / 0 ₸",
            }
        except Exception:
            return None

    def get_game_price_info(self, app_id):
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
            packages = data.get("packages", [])
            sub_id = str(packages[0]) if packages else None
            pc_req = data.get("pc_requirements", {})
            metacritic = data.get("metacritic", {}).get("score", "Н/Д")
            recs = data.get("recommendations", {}).get("total", 0)
            genres = ", ".join([g.get("description", "") for g in data.get("genres", [])[:3]])

            base_info = {
                "appid": str(app_id),
                "subid": sub_id,
                "name": name,
                "genres": genres or "Игры",
                "pc_min": strip_html(pc_req.get("minimum", "Не указаны"))[:350],
                "pc_rec": strip_html(pc_req.get("recommended", "Не указаны"))[:350],
                "metacritic": metacritic,
                "recommendations": recs,
            }

            if is_free:
                base_info.update({
                    "is_free": True,
                    "discount": 100,
                    "initial_kzt": 0.0,
                    "final_kzt": 0.0,
                    "final_formatted": "Бесплатно (0 ₸)",
                    "initial_formatted": "0 ₸",
                })
                return base_info

            if not price_ov:
                base_info.update({
                    "is_free": False,
                    "discount": 0,
                    "initial_kzt": 0.0,
                    "final_kzt": 0.0,
                    "final_formatted": "Недоступно",
                    "initial_formatted": "—",
                })
                return base_info

            initial_kzt = price_ov.get("initial", 0) / 100.0
            final_kzt = price_ov.get("final", 0) / 100.0
            base_info.update({
                "is_free": False,
                "discount": int(price_ov.get("discount_percent", 0)),
                "initial_kzt": initial_kzt,
                "final_kzt": final_kzt,
                "final_formatted": price_ov.get("final_formatted", f"{final_kzt:g} ₸"),
                "initial_formatted": price_ov.get("initial_formatted", f"{initial_kzt:g} ₸"),
            })
            return base_info
        except Exception:
            return None

    def search_games_by_name(self, query):
        url = "https://store.steampowered.com/api/storesearch/"
        params = {"term": query, "l": "russian", "cc": "kz"}
        try:
            r = requests.get(
                url, params=params, headers={"User-Agent": USER_AGENT}, timeout=15
            ).json()
            items = r.get("items", [])[:8]
            results = []
            for it in items:
                appid = str(it.get("id"))
                name = it.get("name", "Игра")
                price_info = it.get("price", {})
                if price_info:
                    final_kzt = price_info.get("final", 0) / 100.0
                    disc = price_info.get("discount_percent", 0)
                    price_str = f"{final_kzt:g} ₸" + (f" (-{disc}%)" if disc > 0 else "")
                else:
                    price_str = "Бесплатно / 0 ₸"
                results.append({"appid": appid, "name": name, "price_str": price_str})
            return results
        except Exception:
            return []

    def get_top_daily_specials(self):
        url = "https://store.steampowered.com/api/featuredcategories?cc=kz&l=ru"
        try:
            r = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=15).json()
            specials = r.get("specials", {}).get("items", [])[:8]
            out = []
            for sp in specials:
                appid = str(sp.get("id"))
                name = sp.get("name", "Игра")
                disc = sp.get("discount_percent", 0)
                final_kzt = sp.get("final_price", 0) / 100.0
                init_kzt = sp.get("original_price", 0) / 100.0
                out.append({
                    "appid": appid,
                    "name": name,
                    "discount": disc,
                    "final_kzt": f"{final_kzt:g} ₸",
                    "init_kzt": f"{init_kzt:g} ₸",
                })
            return out
        except Exception:
            return []

    def auto_purchase_game(self, app_id, sub_id=None):
        if not sub_id:
            sub_id = self.get_subid_from_appid(app_id)
        if not sub_id:
            return {"ok": False, "msg": "Не найден пакет (SubID) для покупки."}

        self.add_free_license(sub_id)
        cookies = self.get_cookies()
        headers = {
            "User-Agent": USER_AGENT,
            "Referer": f"https://store.steampowered.com/app/{app_id}/",
            "X-Requested-With": "XMLHttpRequest",
        }

        try:
            requests.post(
                "https://store.steampowered.com/cart/",
                data={
                    "action": "add_to_cart",
                    "sessionid": self.session_id,
                    "subid": str(sub_id),
                },
                cookies=cookies,
                headers=headers,
                timeout=15,
            )

            init_url = "https://store.steampowered.com/checkout/inittransaction/"
            init_payload = {
                "gidShoppingCart": -1,
                "sessionid": self.session_id,
                "PaymentMethod": "steamaccount",
                "bUseAccountCart": 1,
                "Country": "KZ",
            }
            init_res = requests.post(
                init_url,
                data=init_payload,
                cookies=cookies,
                headers=headers,
                timeout=15,
            ).json()

            transid = init_res.get("transid")
            if transid:
                fin_url = "https://store.steampowered.com/checkout/finalizetransaction/"
                fin_res = requests.post(
                    fin_url,
                    data={"transid": transid, "CardCVV2": ""},
                    cookies=cookies,
                    headers=headers,
                    timeout=15,
                ).json()
                if fin_res.get("success") == 22 or fin_res.get("bSuccess"):
                    return {
                        "ok": True,
                        "msg": "✅ Игра автоматически оплачена с баланса кошелька Steam и добавлена в библиотеку!",
                    }

            return {
                "ok": True,
                "msg": "🛒 Игра добавлена в вашу корзину Steam! Подтвердите списание с кошелька.",
            }
        except Exception as e:
            return {"ok": False, "msg": f"Ошибка автопокупки: {e}"}


steam = SteamClient()
bot = telebot.TeleBot(TG_BOT_TOKEN)


def is_admin(user_id):
    saved_admin = steam.data.get("admin_id")
    if not saved_admin:
        steam.data["admin_id"] = user_id
        save_session(steam.data)
        return True
    return int(saved_admin) == int(user_id)


def notify_user(chat_id, text, reply_markup=None):
    if not chat_id:
        return
    try:
        bot.send_message(
            chat_id,
            text,
            parse_mode="Markdown",
            reply_markup=reply_markup,
            disable_notification=steam.data.get("silent_mode", False),
            disable_web_page_preview=True,
        )
    except Exception:
        pass


# ----------------- ГЛАВНОЕ МЕНЮ TELEGRAM -----------------
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
        f2p_state = "🟢 ВКЛ" if bot_status["f2p_auto_all_enabled"] else "⚪"
        kb.add(
            InlineKeyboardButton("📚 Вся библиотека", callback_data="menu_library"),
            InlineKeyboardButton(
                f"🎮 Фарм F2P (Рандом/Все) {f2p_state}", callback_data="menu_f2p_farm"
            ),
            InlineKeyboardButton("🧮 Калькулятор и Автобай", callback_data="menu_calc"),
            InlineKeyboardButton("📉 Радар скидок", callback_data="menu_discounts"),
            InlineKeyboardButton("🔎 Поиск игры в Steam", callback_data="menu_search"),
            InlineKeyboardButton("🔥 Скидки и Топ чарта (₸)", callback_data="menu_specials"),
            InlineKeyboardButton("🛠 20+ Инструментов Steam", callback_data="menu_tools"),
            InlineKeyboardButton("🎁 Автосбор 100% раздач", callback_data="menu_giveaways"),
            InlineKeyboardButton("👤 Профиль и Баланс", callback_data="menu_profile"),
            InlineKeyboardButton("📡 Тихий пинг", callback_data="menu_ping"),
            InlineKeyboardButton("🔄 Система / GitHub Обновление", callback_data="menu_system"),
        )
    return kb


def format_dashboard_text():
    if not steam.is_configured():
        return (
            "👋 **Добро пожаловать в Steam Ultimate Bot!**\n\n"
            "📄 Токен загружен из `tok.json`.\n"
            f"🌐 Сайт управления: `{tok_config['render_url']}`\n"
            "Авторизуйтесь в аккаунте Steam для доступа ко всем функциям:"
        )
    ago = (
        f"{int(time.time() - bot_status['last_ping_ts'])} сек. назад"
        if bot_status["last_ping_ts"] > 0
        else "Ожидание..."
    )
    watchlist_count = len(steam.data.get("watchlist", {}))
    f2p_farm_str = (
        "🟢 Активен (каждые 15 мин)"
        if bot_status["f2p_auto_all_enabled"]
        else "⏸ Выключен"
    )
    upd_ver = steam.data.get("last_update_time", "Актуальная сборка")
    gh_repo = steam.data.get("gh_repo") or tok_config.get("gh_repo") or "Не задан"
    return (
        f"🤖 **Панель управления Steam Ultimate**\n\n"
        f"👤 Аккаунт: `{steam.data.get('account_name')}`\n"
        f"🏷 Версия: `{upd_ver}` | 🐙 GitHub: `{gh_repo}`\n"
        f"🎮 Авто-фарм всех F2P: `{f2p_farm_str}` (Добавлено: `{bot_status['f2p_added_count']}`)\n"
        f"📉 Игр на радаре / автопокупке: `{watchlist_count}`\n"
        f"🎁 Забрано раздач за сеанс: `{bot_status['claimed_count']}`\n"
        f"📡 Последний тихий пинг: `{ago}`\n"
        f"🌐 Веб-сайт: [Открыть панель управления]({steam.data.get('render_url')})\n\n"
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


# =====================================================================
# ДВИЖОК АВТООБНОВЛЕНИЯ + ОТПРАВКА В GITHUB РЕПОЗИТОРИЙ
# =====================================================================
def get_sorted_buffer_code(chat_id):
    sess = update_sessions.get(chat_id)
    if not sess or not sess.get("chunks"):
        return ""
    sorted_chunks = [txt for _, txt in sorted(sess["chunks"], key=lambda item: item[0])]
    return "\n".join(sorted_chunks)


def apply_new_bot_code(chat_id, new_code):
    """
    1. Проверяет синтаксис нового кода.
    2. Автоматически отправляет обновленный файл в GitHub репозиторий через GitHub API!
    3. Сохраняет локально и перезапускает рабочий процесс бота.
    """
    new_code = new_code.strip()
    new_code = re.sub(r"^```[a-zA-Z]*\n", "", new_code)
    new_code = re.sub(r"\n```$", "", new_code)

    if len(new_code) < 100:
        if chat_id:
            notify_user(chat_id, "❌ Код пустой или слишком короткий.", reply_markup=get_main_keyboard())
        return False, "Код слишком короткий"

    if "ВСТАВЬТЕ_СЮДА_ТОКЕН_БОТА" in new_code:
        new_code = new_code.replace("ВСТАВЬТЕ_СЮДА_ТОКЕН_БОТА", TG_BOT_TOKEN)

    script_path = os.path.abspath(__file__)
    script_name = os.path.basename(script_path)

    try:
        compile(new_code, script_name, "exec")
    except SyntaxError as e:
        err_text = f"Ошибка синтаксиса в строке {e.lineno}: {e.msg}"
        if chat_id:
            notify_user(
                chat_id,
                f"❌ **{err_text}**\nПроверьте целостность файла или загрузите его через Веб-Сайт Render!",
                reply_markup=get_main_keyboard(),
            )
        return False, err_text

    if "BOT_WORKER_MODE" not in new_code and "app.run(" in new_code:
        new_code = new_code.replace(
            'app.run(host="0.0.0.0", port=port)',
            'threading.Thread(target=lambda: app.run(host="0.0.0.0", port=port), daemon=True).start()\n    while True:\n        time.sleep(3600)',
        )

    # Шаг 1: Отправляем обновленный файл в GitHub!
    gh_ok, gh_report = push_code_to_github(new_code)

    # Шаг 2: Сохраняем резервную копию и записываем локальный файл
    try:
        if os.path.exists(script_path):
            shutil.copyfile(script_path, BACKUP_FILE)
        with open(script_path, "w", encoding="utf-8") as f:
            f.write(new_code)
    except Exception as e:
        if chat_id:
            notify_user(chat_id, f"❌ Ошибка записи файла: {e}", reply_markup=get_main_keyboard())
        return False, str(e)

    upd_time = time.strftime("%d.%m %H:%M:%S")
    steam.data["last_update_time"] = f"Обновлен {upd_time}"
    steam.data["just_updated"] = f"{upd_time} | {gh_report}"
    save_session(steam.data)

    if chat_id:
        update_sessions.pop(chat_id, None)
        notify_user(
            chat_id,
            f"✅ **Код проверен и сохранен в `{script_name}` (`{len(new_code)}` симв.)!**\n"
            f"🐙 **Статус GitHub:** {gh_report}\n"
            f"🔄 Перезапускаю бота (2–3 сек)...",
        )

    def _graceful_restart():
        time.sleep(1.2)
        try:
            bot.stop_polling()
        except Exception:
            pass
        time.sleep(1.0)
        if os.environ.get("BOT_WORKER_MODE") == "1":
            os._exit(42)
        else:
            os.execv(sys.executable, [sys.executable] + sys.argv)

    threading.Thread(target=_graceful_restart, daemon=True).start()
    return True, f"Локально обновлено! {gh_report}"


@bot.message_handler(content_types=["document"])
def handle_document_update(message):
    if not is_admin(message.from_user.id):
        return
    doc = message.document
    fname = (doc.file_name or "").lower()
    is_upd_active = update_sessions.get(message.chat.id, {}).get("active", False)

    if fname.endswith(".py") or fname.endswith(".txt") or is_upd_active:
        bot.send_message(
            message.chat.id,
            f"📥 Скачиваю файл `{doc.file_name}`, пушу в GitHub и обновляю бота...",
            parse_mode="Markdown",
        )
        try:
            file_info = bot.get_file(doc.file_id)
            file_url = f"https://api.telegram.org/file/bot{TG_BOT_TOKEN}/{file_info.file_path}"
            resp = requests.get(file_url, timeout=30)
            if resp.status_code == 200:
                new_code = resp.content.decode("utf-8-sig", errors="replace")
                apply_new_bot_code(message.chat.id, new_code)
            else:
                notify_user(message.chat.id, f"❌ Ошибка скачивания файла: HTTP {resp.status_code}")
        except Exception as e:
            notify_user(message.chat.id, f"❌ Ошибка чтения файла: {e}", reply_markup=get_main_keyboard())


@bot.message_handler(
    func=lambda m: update_sessions.get(m.chat.id, {}).get("active", False),
    content_types=["text"],
)
def handle_chunked_update_text(message):
    chat_id = message.chat.id
    text_stripped = message.text.strip()
    low = text_stripped.lower()

    if low in ("отмена", "/cancel", "cancel"):
        update_sessions.pop(chat_id, None)
        notify_user(chat_id, "❎ Режим автообновления отменен.", reply_markup=get_main_keyboard())
        return

    if low in UPDATE_TRIGGER_CODES:
        with update_lock:
            full_code = get_sorted_buffer_code(chat_id)
            parts_cnt = len(update_sessions.get(chat_id, {}).get("chunks", []))
        if not full_code:
            notify_user(chat_id, "⚠️ Буфер пуст! Сначала отправьте код.")
            return
        bot.send_message(
            chat_id,
            f"🔑 Код подтверждения принят! Объединяю `{parts_cnt}` частей, отправляю в GitHub и обновляю...",
            parse_mode="Markdown",
        )
        apply_new_bot_code(chat_id, full_code)
        return

    if (text_stripped.startswith("http://") or text_stripped.startswith("https://")) and "\n" not in text_stripped:
        bot.send_message(chat_id, "🌐 Скачиваю код по ссылке...")
        try:
            r = requests.get(text_stripped, headers={"User-Agent": USER_AGENT}, timeout=15)
            if r.status_code == 200:
                apply_new_bot_code(chat_id, r.text)
                return
        except Exception as e:
            notify_user(chat_id, f"❌ Ошибка скачивания: {e}")
            return

    with update_lock:
        sess = update_sessions[chat_id]
        sess["chunks"].append((message.message_id, message.text))
        full_code = get_sorted_buffer_code(chat_id)
        total_chars = len(full_code)
        parts_count = len(sess["chunks"])

    is_valid_syntax = False
    try:
        clean_test = re.sub(r"^```[a-zA-Z]*\n", "", full_code.strip())
        clean_test = re.sub(r"\n```$", "", clean_test)
        compile(clean_test, "main.py", "exec")
        is_valid_syntax = True
    except SyntaxError:
        is_valid_syntax = False

    kb = InlineKeyboardMarkup(row_width=1)
    kb.add(
        InlineKeyboardButton(
            f"✅ Объединить, запушить в GitHub и обновить ({parts_count} ч.)",
            callback_data="upd_apply_now",
        ),
        InlineKeyboardButton("🧹 Очистить буфер", callback_data="upd_clear_buf"),
        InlineKeyboardButton("❌ Отмена", callback_data="upd_cancel"),
    )

    syntax_status = (
        "🟢 **Код валиден! Отправьте `1111` или нажмите кнопку ниже:**"
        if is_valid_syntax
        else "🟡 *Принимаю части... Когда отправите все, напишите `1111`:*"
    )

    msg_text = (
        f"📥 **Буфер автообновления:**\n"
        f"• Принято частей: `{parts_count} шт.` (`{total_chars}` симв.)\n\n"
        f"{syntax_status}"
    )

    with update_lock:
        status_mid = sess.get("status_msg_id")
        if status_mid:
            try:
                bot.edit_message_text(msg_text, chat_id, status_mid, parse_mode="Markdown", reply_markup=kb)
                return
            except Exception:
                pass
        else:
            try:
                sent = bot.send_message(chat_id, msg_text, parse_mode="Markdown", reply_markup=kb)
                sess["status_msg_id"] = sent.message_id
            except Exception:
                pass


# ----------------- СТАРТ И НАВИГАЦИЯ -----------------
@bot.message_handler(commands=["start", "menu"])
def cmd_start(message):
    if not is_admin(message.from_user.id):
        bot.send_message(message.chat.id, "⛔ Доступ запрещен.")
        return
    update_sessions.pop(message.chat.id, None)
    send_or_edit(message.chat.id, format_dashboard_text(), get_main_keyboard())


@bot.callback_query_handler(func=lambda call: True)
def handle_callbacks(call):
    if not is_admin(call.from_user.id):
        return

    chat_id = call.message.chat.id
    msg_id = call.message.message_id
    data = call.data

    if data == "back_main":
        update_sessions.pop(chat_id, None)
        send_or_edit(chat_id, format_dashboard_text(), get_main_keyboard(), msg_id)

    elif data == "update_bot":
        update_sessions[chat_id] = {"active": True, "chunks": [], "status_msg_id": None}
        kb = InlineKeyboardMarkup(row_width=1)
        kb.add(
            InlineKeyboardButton("🐙 Настроить авто-отправку в GitHub", callback_data="gh_setup"),
            InlineKeyboardButton("❌ Отменить обновление", callback_data="upd_cancel"),
        )
        send_or_edit(
            chat_id,
            "🔄 **Режим автообновления (Локально + GitHub API) активирован!**\n\n"
            "• **Способ 1 (Файлом):** Просто скиньте файл `.py` или `.txt` документом в чат.\n"
            "• **Способ 2 (Текстом):** Вставьте код в чат и напишите **`1111`**.\n"
            "• **Способ 3 (Через Веб-Сайт):** Загрузите файл на вашем сайте Render.\n\n"
            "💡 *Если настроен GitHub-токен, бот автоматически отправит обновленный файл прямо в ваш репозиторий GitHub!*",
            kb,
            msg_id,
        )

    elif data == "gh_setup":
        update_sessions.pop(chat_id, None)
        msg = bot.send_message(
            chat_id,
            "🐙 **Настройка прямой отправки обновлений в GitHub**\n\n"
            "Отправьте одной строкой через пробел:\n"
            "`ВАШ_GITHUB_TOKEN логин/репозиторий имя_файла.py`\n\n"
            "Пример:\n"
            "`ghp_12345abcdef myuser/steambot main.py`",
            parse_mode="Markdown",
        )
        bot.register_next_step_handler(msg, step_setup_github)

    elif data == "gh_push_current":
        bot.answer_callback_query(call.id, "Отправляю текущий код в GitHub...")
        with open(os.path.abspath(__file__), "r", encoding="utf-8") as f:
            cur_code = f.read()
        ok, rep = push_code_to_github(cur_code)
        notify_user(chat_id, f"🐙 **Результат отправки в GitHub:**\n{rep}", get_main_keyboard())

    elif data == "upd_apply_now":
        with update_lock:
            full_code = get_sorted_buffer_code(chat_id)
        if not full_code:
            bot.answer_callback_query(call.id, "Буфер пуст!", show_alert=True)
            return
        bot.answer_callback_query(call.id, "Обновляю бота и пушу в GitHub...")
        apply_new_bot_code(chat_id, full_code)

    elif data == "upd_clear_buf":
        with update_lock:
            update_sessions[chat_id] = {"active": True, "chunks": [], "status_msg_id": None}
        bot.answer_callback_query(call.id, "Буфер очищен!")

    elif data == "upd_cancel":
        update_sessions.pop(chat_id, None)
        bot.answer_callback_query(call.id, "Отменено")
        send_or_edit(chat_id, format_dashboard_text(), get_main_keyboard(), msg_id)

    elif data == "sys_rollback":
        if not os.path.exists(BACKUP_FILE):
            bot.answer_callback_query(call.id, "Бэкап еще не создан!", show_alert=True)
            return
        with open(BACKUP_FILE, "r", encoding="utf-8") as f:
            bak_code = f.read()
        apply_new_bot_code(chat_id, bak_code)

    # =================================================================
    # ФАРМ РАНДОМНЫХ И ВСЕХ FREE-TO-PLAY ИГР (КАЖДЫЕ 15 МИНУТ)
    # =================================================================
    elif data == "menu_f2p_farm":
        auto_state = (
            "🟢 ВКЛЮЧЕН (каждые 15 мин собирает новые F2P игры)"
            if bot_status["f2p_auto_all_enabled"]
            else "⚪ ВЫКЛЮЧЕН"
        )
        claimed_total = len(load_json_set(CLAIMED_F2P_FILE))
        kb = InlineKeyboardMarkup(row_width=1)
        kb.add(
            InlineKeyboardButton("🎲 Добавить 5 рандомных F2P игр сейчас", callback_data="f2p_rand_5"),
            InlineKeyboardButton("🎲 Добавить 15 рандомных F2P игр сейчас", callback_data="f2p_rand_15"),
            InlineKeyboardButton("🔢 Указать свое число рандомных F2P игр", callback_data="f2p_rand_custom"),
            InlineKeyboardButton(
                f"♾️ Авто-сбор ВСЕХ F2P (каждые 15 мин): {'Выключить 🔴' if bot_status['f2p_auto_all_enabled'] else 'Включить 🟢'}",
                callback_data="f2p_toggle_all",
            ),
            InlineKeyboardButton("🎯 Добавить популярные F2P-хиты (CS2, Dota, PUBG)", callback_data="test_f2p"),
            InlineKeyboardButton("⬅️ Назад в главное меню", callback_data="back_main"),
        )
        text = (
            f"🎮 **Генератор и Авто-сборщик всех Free-to-Play игр Steam**\n\n"
            f"• Авто-сбор ВСЕХ F2P (каждые 15 мин): **{auto_state}**\n"
            f"• Последний цикл авто-сбора: `{bot_status['last_f2p_check']}`\n"
            f"• Добавлено F2P за сеанс: `{bot_status['f2p_added_count']} шт.`\n"
            f"• Всего в базе собранных F2P: `{claimed_total} шт.`"
        )
        send_or_edit(chat_id, text, kb, msg_id)

    elif data in ("f2p_rand_5", "f2p_rand_15"):
        cnt = 5 if data == "f2p_rand_5" else 15
        bot.answer_callback_query(call.id, f"Ищу и добавляю {cnt} случайных F2P игр...")
        notify_user(chat_id, f"⏳ Ищу в каталоге Steam и добавляю **{cnt} рандомных Free-to-Play игр**...")

        def _run_rand_f2p(target_n):
            added = steam.add_random_f2p_batch(target_n)
            library_cache.pop(chat_id, None)
            if added:
                notify_user(
                    chat_id,
                    f"🎉 **Добавлено {len(added)} рандомных F2P игр в библиотеку!**\n\n" + "\n".join(added),
                    reply_markup=get_main_keyboard(),
                )
            else:
                notify_user(
                    chat_id,
                    "ℹ️ На выбранной странице игры уже добавлены (или лимит Steam). Попробуйте еще раз!",
                    reply_markup=get_main_keyboard(),
                )

        threading.Thread(target=_run_rand_f2p, args=(cnt,), daemon=True).start()

    elif data == "f2p_rand_custom":
        msg = bot.send_message(
            chat_id,
            "🔢 Сколько случайных Free-to-Play игр добавить за раз? (Введите число от `1` до `35`):",
            parse_mode="Markdown",
        )
        bot.register_next_step_handler(msg, step_f2p_custom_count)

    elif data == "f2p_toggle_all":
        bot_status["f2p_auto_all_enabled"] = not bot_status["f2p_auto_all_enabled"]
        steam.data["f2p_auto_all"] = bot_status["f2p_auto_all_enabled"]
        save_session(steam.data)
        bot.answer_callback_query(call.id, "Настройка авто-сбора F2P обновлена!")
        call.data = "menu_f2p_farm"
        handle_callbacks(call)

    # =================================================================
    # ПОЛНАЯ БИБЛИОТЕКА (КУПЛЕННЫЕ 🔒 + БЕСПЛАТНЫЕ ❌)
    # =================================================================
    elif data == "menu_library":
        kb = InlineKeyboardMarkup(row_width=2)
        kb.add(
            InlineKeyboardButton("📋 Все игры аккаунта", callback_data="lib_view_all_0"),
            InlineKeyboardButton("🔒 Только купленные", callback_data="lib_view_paid_0"),
            InlineKeyboardButton("❌ Только удаляемые", callback_data="lib_view_free_0"),
            InlineKeyboardButton("💎 Аналитика библиотеки", callback_data="tool_lib_stats"),
            InlineKeyboardButton("📄 Скачать список (.txt)", callback_data="lib_export_txt"),
            InlineKeyboardButton("🎲 Случайная игра", callback_data="tool_random_game"),
        )
        kb.add(InlineKeyboardButton("⬅️ Назад в главное меню", callback_data="back_main"))
        send_or_edit(
            chat_id,
            "📚 **Полная библиотека вашего аккаунта Steam**\n\n"
            "• **📋 Все игры аккаунта** — показывает и купленные игры, и бесплатные.\n"
            "• **🔒 Купленные игры** защищены от удаления.\n"
            "• **❌ Бесплатные игры** можно удалить с аккаунта в один клик.",
            kb,
            msg_id,
        )

    elif data.startswith("lib_view_"):
        parts = data.split("_")
        mode = parts[2]
        page = int(parts[3])

        if page == 0 or chat_id not in library_cache:
            bot.answer_callback_query(call.id, "Загружаю библиотеку из Steam...")
            library_cache[chat_id] = steam.get_all_licenses()

        all_items = library_cache.get(chat_id, [])
        if mode == "paid":
            items = [x for x in all_items if not x["removable"]]
            mode_title = "🔒 Купленные игры (Удаление запрещено)"
        elif mode == "free":
            items = [x for x in all_items if x["removable"]]
            mode_title = "❌ Бесплатные игры (Можно удалить)"
        else:
            items = all_items
            mode_title = "📋 Все игры аккаунта (Купленные + Бесплатные)"

        per_page = 6
        total_pages = max(1, (len(items) + per_page - 1) // per_page)
        page = max(0, min(page, total_pages - 1))
        chunk = items[page * per_page : (page + 1) * per_page]

        kb = InlineKeyboardMarkup(row_width=1)
        kb.row(
            InlineKeyboardButton(f"{'🔘' if mode=='all' else '⚪'} Все ({len(all_items)})", callback_data="lib_view_all_0"),
            InlineKeyboardButton(f"{'🔘' if mode=='paid' else '⚪'} Купленные", callback_data="lib_view_paid_0"),
            InlineKeyboardButton(f"{'🔘' if mode=='free' else '⚪'} Бесплатные", callback_data="lib_view_free_0"),
        )

        for idx, item in enumerate(chunk):
            if item["removable"]:
                kb.add(
                    InlineKeyboardButton(
                        f"❌ Удалить: {item['name']} ({item['date']})",
                        callback_data=f"lib_del_{item['packageid']}_{mode}_{page}",
                    )
                )
            else:
                kb.add(
                    InlineKeyboardButton(
                        f"🔒 Куплено: {item['name']} ({item['date']})",
                        callback_data=f"lib_locked_{idx}",
                    )
                )

        nav_btns = []
        if page > 0:
            nav_btns.append(InlineKeyboardButton("⬅️ Пред.", callback_data=f"lib_view_{mode}_{page-1}"))
        nav_btns.append(InlineKeyboardButton("🔄 Обновить", callback_data=f"lib_view_{mode}_0"))
        if page < total_pages - 1:
            nav_btns.append(InlineKeyboardButton("След. ➡️", callback_data=f"lib_view_{mode}_{page+1}"))
        kb.row(*nav_btns)
        kb.add(InlineKeyboardButton("⬅️ Назад в Библиотеку", callback_data="menu_library"))

        text = (
            f"📚 **{mode_title}**\n"
            f"Найдено: `{len(items)}` | Страница `{page + 1}` из `{total_pages}`\n\n"
            f"• `🔒 Куплено` — платная лицензия (защищена от удаления).\n"
            f"• `❌ Удалить` — бесплатная лицензия (нажмите для удаления)."
        )
        send_or_edit(chat_id, text, kb, msg_id)

    elif data.startswith("lib_locked_"):
        bot.answer_callback_query(
            call.id,
            "🔒 Это купленная игра (или активированная ключом). Её нельзя удалить!",
            show_alert=True,
        )

    elif data.startswith("lib_del_"):
        parts = data.split("_")
        pkg_id = parts[2]
        mode = parts[3] if len(parts) > 3 else "all"
        page = int(parts[4]) if len(parts) > 4 else 0
        bot.answer_callback_query(call.id, f"Удаляю лицензию {pkg_id}...")
        if steam.remove_license(pkg_id):
            if chat_id in library_cache:
                library_cache[chat_id] = [x for x in library_cache[chat_id] if x["packageid"] != pkg_id]
            notify_user(chat_id, f"✅ Бесплатная игра (пакет `{pkg_id}`) удалена!")
        else:
            notify_user(chat_id, f"❌ Не удалось удалить пакет `{pkg_id}`.")
        call.data = f"lib_view_{mode}_{page}"
        handle_callbacks(call)

    elif data == "lib_export_txt":
        bot.answer_callback_query(call.id, "Формирую файл со списком ваших игр...")
        items = steam.get_all_licenses()
        lines = [f"Всего игр/лицензий на аккаунте: {len(items)}\n" + "=" * 50]
        for i, it in enumerate(items, 1):
            status = "[Бесплатная]" if it["removable"] else "[Куплена 🔒]"
            lines.append(f"{i}. {it['name']} | {status} | Дата: {it['date']} | Тип: {it['method']}")
        bio_bytes = io.BytesIO("\n".join(lines).encode("utf-8"))
        bio_bytes.name = f"steam_library_{steam.data.get('account_name', 'user')}.txt"
        bot.send_document(chat_id, bio_bytes, caption=f"📄 Полный список игр (`{len(items)} шт.`)", parse_mode="Markdown")

    # =================================================================
    # КАЛЬКУЛЯТОР СКИДОК В ТЕНГЕ (₸) + АВТОПОКУПКА
    # =================================================================
    elif data == "menu_calc":
        msg = bot.send_message(
            chat_id,
            "🧮 **Калькулятор скидок в тенге (₸) и Автопокупка**\n\n"
            "Отправьте **AppID** игры (например `1091500` для Cyberpunk 2077) или ссылку на игру в Steam:",
            parse_mode="Markdown",
        )
        bot.register_next_step_handler(msg, step_calc_get_appid)

    elif data.startswith("calc_from_appid_"):
        app_id = data.replace("calc_from_appid_", "")
        bot.answer_callback_query(call.id, "Загружаю цену игры в тенге...")
        show_game_and_ask_discount(chat_id, app_id)

    elif data.startswith("watch_set_"):
        parts = data.split("_")
        mode = parts[2]
        app_id = parts[3]
        target_disc = int(parts[4])

        info = steam.get_game_price_info(app_id)
        game_name = info["name"] if info else f"App {app_id}"
        cur_disc = info["discount"] if info else 0

        if "watchlist" not in steam.data:
            steam.data["watchlist"] = {}

        steam.data["watchlist"][app_id] = {
            "name": game_name,
            "target_disc": 1 if mode == "any" else target_disc,
            "mode": mode,
            "last_discount": cur_disc,
        }
        save_session(steam.data)

        if mode == "autobuy":
            mode_desc = f"🛒 **АВТОПОКУПКА (через 5–10 мин)** при скидке **-{target_disc}% и выше**!"
            if cur_disc >= target_disc and app_id not in bot_status["autobuy_tasks"]:
                threading.Thread(
                    target=trigger_delayed_autobuy,
                    args=(chat_id, app_id, game_name, cur_disc),
                    daemon=True,
                ).start()
        elif mode == "notify":
            mode_desc = f"🔔 **Только уведомление** при скидке **-{target_disc}% и выше**."
        else:
            mode_desc = "🔔 **Уведомление при ЛЮБОЙ скидке**."

        notify_user(
            chat_id,
            f"✅ **Сохранено для «{game_name}» (`{app_id}`)!**\n\n{mode_desc}",
            reply_markup=get_main_keyboard(),
        )

    # =================================================================
    # РАДАР СКИДОК
    # =================================================================
    elif data == "menu_discounts":
        watchlist = steam.data.get("watchlist", {})
        kb = InlineKeyboardMarkup(row_width=1)
        kb.add(
            InlineKeyboardButton("🧮 Рассчитать скидку в ₸ / Добавить игру", callback_data="menu_calc"),
            InlineKeyboardButton("⭐ Импортировать из Списка желаемого Steam", callback_data="tool_wishlist"),
            InlineKeyboardButton("🔍 Проверить цены по списку сейчас", callback_data="disc_check_now"),
        )
        for appid, info in list(watchlist.items())[:8]:
            m_icon = "🛒 Автобай" if info.get("mode") == "autobuy" else "🔔 Увед."
            kb.add(
                InlineKeyboardButton(
                    f"🗑 Удалить: {info.get('name', appid)} [{m_icon} -{info.get('target_disc', 1)}%]",
                    callback_data=f"disc_rem_{appid}",
                )
            )
        kb.add(InlineKeyboardButton("⬅️ Назад в главное меню", callback_data="back_main"))

        if watchlist:
            lines = []
            for k, v in watchlist.items():
                m_str = "🛒 Автопокупка (5–10 мин)" if v.get("mode") == "autobuy" else "🔔 Уведомление"
                lines.append(
                    f"• **{v.get('name')}** (`{k}`) — цель `-{v.get('target_disc', 1)}%` ({m_str}, сейчас `-{v.get('last_discount', 0)}%`)"
                )
            list_text = "\n".join(lines)
        else:
            list_text = "_Список пуст. Добавьте игры через калькулятор или Список желаемого!_"

        text = (
            f"📉 **Радар скидок и Автопокупки (проверка каждые 15 мин)**\n"
            f"Последняя проверка: `{bot_status['last_discount_check']}`\n\n"
            f"{list_text}"
        )
        send_or_edit(chat_id, text, kb, msg_id)

    elif data.startswith("disc_rem_"):
        appid = data.replace("disc_rem_", "")
        steam.data.get("watchlist", {}).pop(appid, None)
        save_session(steam.data)
        bot.answer_callback_query(call.id, "Игра удалена из радара")
        call.data = "menu_discounts"
        handle_callbacks(call)

    elif data == "disc_check_now":
        bot.answer_callback_query(call.id, "Проверяю цены в тенге...")
        rep = check_discounts(force_report=True)
        notify_user(chat_id, rep or "ℹ️ Список отслеживания пуст.", reply_markup=get_main_keyboard())

    # =================================================================
    # ПОИСК, ТОП СКИДОК И ЛИДЕРЫ ПРОДАЖ
    # =================================================================
    elif data == "menu_search":
        msg = bot.send_message(
            chat_id,
            "🔎 **Поиск игры в магазине Steam**\nНапишите название игры (например: `Cyberpunk`, `GTA`, `Witcher`, `Rust`):",
            parse_mode="Markdown",
        )
        bot.register_next_step_handler(msg, step_search_game)

    elif data == "menu_specials":
        bot.answer_callback_query(call.id, "Загружаю скидки дня...")
        specials = steam.get_top_daily_specials()
        kb = InlineKeyboardMarkup(row_width=1)
        kb.add(InlineKeyboardButton("🏆 Показать Лидеров продаж (Топ чарта KZ)", callback_data="tool_top_sellers"))
        lines = ["🔥 **Топ актуальных скидок дня в Steam (Тенге ₸):**\n"]
        for sp in specials:
            lines.append(
                f"• **{sp['name']}** (`{sp['appid']}`) — **-{sp['discount']}%** → `{sp['final_kzt']}` (было `{sp['init_kzt']}`)"
            )
            kb.add(
                InlineKeyboardButton(
                    f"🧮 Калькулятор / Автобай: {sp['name']} (-{sp['discount']}%)",
                    callback_data=f"calc_from_appid_{sp['appid']}",
                )
            )
        kb.add(InlineKeyboardButton("⬅️ Назад в главное меню", callback_data="back_main"))
        send_or_edit(chat_id, "\n".join(lines), kb, msg_id)

    elif data == "tool_top_sellers":
        bot.answer_callback_query(call.id, "Загружаю лидеров продаж...")
        tops = steam.get_top_sellers_kz()
        kb = InlineKeyboardMarkup(row_width=1)
        lines = ["🏆 **Лидеры продаж Steam в Казахстане:**\n"]
        for i, t in enumerate(tops, 1):
            d_str = f" (-{t['discount']}%)" if t["discount"] > 0 else ""
            lines.append(f"{i}. **{t['name']}** — `{t['price']}`{d_str}")
            kb.add(
                InlineKeyboardButton(
                    f"🧮 Выбрать: {t['name']} ({t['price']})",
                    callback_data=f"calc_from_appid_{t['appid']}",
                )
            )
        kb.add(InlineKeyboardButton("⬅️ Назад", callback_data="menu_specials"))
        send_or_edit(chat_id, "\n".join(lines), kb, msg_id)

    # =================================================================
    # МЕНЮ 20+ ИНСТРУМЕНТОВ И СТАТИСТИКИ ИГР
    # =================================================================
    elif data == "menu_tools":
        kb = InlineKeyboardMarkup(row_width=2)
        kb.add(
            InlineKeyboardButton("👥 Онлайн игры (Live)", callback_data="tool_players"),
            InlineKeyboardButton("🌍 Цены по регионам", callback_data="tool_reg_prices"),
            InlineKeyboardButton("🎁 Собрать бесплатные DLC", callback_data="tool_free_dlc"),
            InlineKeyboardButton("📰 Патчи и новости игры", callback_data="tool_game_news"),
            InlineKeyboardButton("🏆 Редкие достижения", callback_data="tool_achievements"),
            InlineKeyboardButton("💻 Системные треб. и Рейтинг", callback_data="tool_sys_reqs"),
            InlineKeyboardButton("⭐ Список желаемого Steam", callback_data="tool_wishlist"),
            InlineKeyboardButton("💎 Аналитика библиотеки", callback_data="tool_lib_stats"),
            InlineKeyboardButton("📦 Инспектор SubID пакета", callback_data="tool_inspect_sub"),
            InlineKeyboardButton("🛒 Очистить корзину Steam", callback_data="tool_clear_cart"),
            InlineKeyboardButton("🔑 Активировать ключ Steam", callback_data="tool_redeem_key"),
            InlineKeyboardButton("🧾 История покупок", callback_data="tool_history"),
            InlineKeyboardButton("🎲 Случайная игра", callback_data="tool_random_game"),
            InlineKeyboardButton("🕵️ Статус Steam и CS2", callback_data="tool_server_status"),
            InlineKeyboardButton("🚀 Пакет F2P-хитов (5 шт)", callback_data="tool_batch_f2p"),
            InlineKeyboardButton("📝 Личные заметки", callback_data="tool_notes"),
            InlineKeyboardButton("🔕 Тихий режим (Звук)", callback_data="tool_toggle_silent"),
            InlineKeyboardButton("🧹 Очистить кэш бота", callback_data="tool_clear_cache"),
        )
        kb.add(InlineKeyboardButton("⬅️ Назад в главное меню", callback_data="back_main"))
        send_or_edit(
            chat_id,
            "🛠 **Расширенные инструменты и Статистика игр (20+ функций):**\n"
            "Выберите нужный инструмент ниже:",
            kb,
            msg_id,
        )

    elif data == "tool_players":
        msg = bot.send_message(chat_id, "👥 Отправьте **AppID** игры (например `730` для CS2), чтобы узнать текущий онлайн:", parse_mode="Markdown")
        bot.register_next_step_handler(msg, step_tool_players)

    elif data == "tool_reg_prices":
        msg = bot.send_message(chat_id, "🌍 Отправьте **AppID** игры для сравнения цены в KZ (₸), US ($), UA (₴) и RU (₽):", parse_mode="Markdown")
        bot.register_next_step_handler(msg, step_tool_regional_prices)

    elif data == "tool_free_dlc":
        msg = bot.send_message(chat_id, "🎁 Отправьте **AppID** игры, чтобы найти и активировать все бесплатные DLC к ней:", parse_mode="Markdown")
        bot.register_next_step_handler(msg, step_tool_free_dlc)

    elif data == "tool_game_news":
        msg = bot.send_message(chat_id, "📰 Отправьте **AppID** игры для чтения свежих патчноутов и новостей:", parse_mode="Markdown")
        bot.register_next_step_handler(msg, step_tool_game_news)

    elif data == "tool_achievements":
        msg = bot.send_message(chat_id, "🏆 Отправьте **AppID** игры для просмотра статистики достижений:", parse_mode="Markdown")
        bot.register_next_step_handler(msg, step_tool_achievements)

    elif data == "tool_sys_reqs":
        msg = bot.send_message(chat_id, "💻 Отправьте **AppID** игры для просмотра системных требований ПК и рейтинга Metacritic:", parse_mode="Markdown")
        bot.register_next_step_handler(msg, step_tool_sys_reqs)

    elif data == "tool_inspect_sub":
        msg = bot.send_message(chat_id, "📦 Отправьте числовой **SubID** пакета/бандла Steam:", parse_mode="Markdown")
        bot.register_next_step_handler(msg, step_tool_inspect_sub)

    elif data == "tool_notes":
        notes = steam.data.get("notes", [])
        n_txt = "\n".join([f"{i+1}. `{n}`" for i, n in enumerate(notes)]) or "_Заметок пока нет_"
        kb = InlineKeyboardMarkup(row_width=2).add(
            InlineKeyboardButton("➕ Добавить заметку", callback_data="note_add"),
            InlineKeyboardButton("🗑 Очистить заметки", callback_data="note_clear"),
            InlineKeyboardButton("⬅️ Назад", callback_data="menu_tools"),
        )
        send_or_edit(chat_id, f"📝 **Ваши сохраненные заметки:**\n\n{n_txt}", kb, msg_id)

    elif data == "note_add":
        msg = bot.send_message(chat_id, "✏️ Введите текст заметки:")
        bot.register_next_step_handler(msg, step_add_note)

    elif data == "note_clear":
        steam.data["notes"] = []
        save_session(steam.data)
        call.data = "tool_notes"
        handle_callbacks(call)

    elif data == "tool_wishlist":
        bot.answer_callback_query(call.id, "Загружаю ваш Список желаемого...")
        w_items = steam.get_wishlist_items()
        if not w_items:
            notify_user(chat_id, "ℹ️ Ваш Список желаемого пуст или скрыт настройками приватности.", reply_markup=get_main_keyboard())
            return
        kb = InlineKeyboardMarkup(row_width=1)
        kb.add(InlineKeyboardButton("📥 Импортировать ВСЕ эти игры в Радар скидок", callback_data="wishlist_import_all"))
        lines = [f"⭐ **Ваш Список желаемого Steam ({len(w_items)} игр):**\n"]
        for it in w_items:
            d_txt = f" 🔥 **-{it['discount']}%**" if it["discount"] > 0 else ""
            lines.append(f"• **{it['name']}** (`{it['appid']}`) — `{it['price']}`{d_txt}")
            kb.add(InlineKeyboardButton(f"🧮 Калькулятор: {it['name']}", callback_data=f"calc_from_appid_{it['appid']}"))
        kb.add(InlineKeyboardButton("⬅️ Назад в Инструменты", callback_data="menu_tools"))
        send_or_edit(chat_id, "\n".join(lines), kb, msg_id)

    elif data == "wishlist_import_all":
        w_items = steam.get_wishlist_items()
        steam.data.setdefault("watchlist", {})
        added_w = 0
        for it in w_items:
            aid = it["appid"]
            if aid not in steam.data["watchlist"]:
                steam.data["watchlist"][aid] = {
                    "name": it["name"],
                    "target_disc": 20,
                    "mode": "notify",
                    "last_discount": it["discount"],
                }
                added_w += 1
        save_session(steam.data)
        bot.answer_callback_query(call.id, f"Импортировано {added_w} игр!", show_alert=True)
        call.data = "menu_discounts"
        handle_callbacks(call)

    elif data == "tool_lib_stats":
        bot.answer_callback_query(call.id, "Анализирую вашу библиотеку...")
        items = library_cache.get(chat_id) or steam.get_all_licenses()
        library_cache[chat_id] = items
        total = len(items)
        paid_cnt = sum(1 for x in items if not x["removable"])
        free_cnt = sum(1 for x in items if x["removable"])
        keys_cnt = sum(1 for x in items if "розничн" in x["method"].lower() or "retail" in x["method"].lower())
        oldest_game = items[-1] if items else None
        newest_game = items[0] if items else None

        text = (
            f"💎 **Полная аналитика вашей библиотеки Steam**\n\n"
            f"📊 **Всего лицензий на аккаунте:** `{total} шт.`\n"
            f"🔒 **Купленные игры (защищены):** `{paid_cnt} шт.`\n"
            f"🔑 **Из них активировано ключами:** `{keys_cnt} шт.`\n"
            f"🆓 **Бесплатные и акционные игры:** `{free_cnt} шт.`\n\n"
            f"🆕 **Последняя добавленная игра:**\n"
            f"`{newest_game['name'] if newest_game else '—'}` ({newest_game['date'] if newest_game else '—'})\n"
            f"🏛 **Первая игра на аккаунте:**\n"
            f"`{oldest_game['name'] if oldest_game else '—'}` ({oldest_game['date'] if oldest_game else '—'})"
        )
        kb = InlineKeyboardMarkup().add(InlineKeyboardButton("⬅️ Назад", callback_data="menu_tools"))
        send_or_edit(chat_id, text, kb, msg_id)

    elif data == "tool_clear_cart":
        steam.clear_steam_cart()
        notify_user(chat_id, "🛒 **Корзина покупок Steam успешно очищена!**", reply_markup=get_main_keyboard())

    elif data == "tool_redeem_key":
        msg = bot.send_message(chat_id, "🔑 Отправьте ключ продукта Steam (`AAAAA-BBBBB-CCCCC`):", parse_mode="Markdown")
        bot.register_next_step_handler(msg, step_redeem_key)

    elif data == "tool_history":
        bot.answer_callback_query(call.id, "Загружаю историю транзакций...")
        hist = steam.get_purchase_history()
        text = "🧾 **Последние операции на аккаунте Steam:**\n\n" + ("\n".join(hist) if hist else "_Операции не найдены._")
        kb = InlineKeyboardMarkup().add(InlineKeyboardButton("⬅️ Назад в Инструменты", callback_data="menu_tools"))
        send_or_edit(chat_id, text, kb, msg_id)

    elif data == "tool_random_game":
        items = library_cache.get(chat_id) or steam.get_all_licenses()
        library_cache[chat_id] = items
        if not items:
            bot.answer_callback_query(call.id, "Библиотека пуста!", show_alert=True)
            return
        pick = random.choice(items)
        status_s = "Купленная игра 🔒" if not pick["removable"] else "Бесплатная игра"
        notify_user(
            chat_id,
            f"🎲 **Случайный выбор игры:**\n\n🎮 **{pick['name']}**\n📅 Добавлена: `{pick['date']}` ({status_s})",
            reply_markup=get_main_keyboard(),
        )

    elif data == "tool_server_status":
        bot.answer_callback_query(call.id, "Проверяю серверы Valve...")
        t0 = time.time()
        store_ok, comm_ok = False, False
        cs2_players = steam.get_live_player_count(730) or "Н/Д"
        dota_players = steam.get_live_player_count(570) or "Н/Д"
        try:
            store_ok = requests.get("https://store.steampowered.com/", timeout=10).status_code == 200
            comm_ok = requests.get("https://steamcommunity.com/", timeout=10).status_code == 200
        except Exception:
            pass
        ping_ms = int((time.time() - t0) * 300)
        text = (
            f"🕵️ **Статус серверов Steam и Онлайн:**\n\n"
            f"🛒 Магазин Steam: `{'🟢 Онлайн' if store_ok else '🔴 Недоступен'}`\n"
            f"👥 Сообщество Steam: `{'🟢 Онлайн' if comm_ok else '🔴 Недоступно'}`\n"
            f"🔫 Игроков в CS2 сейчас: **`{cs2_players}`**\n"
            f"⚔️ Игроков в Dota 2 сейчас: **`{dota_players}`**\n"
            f"⚡ Отклик API: `{ping_ms} мс`"
        )
        kb = InlineKeyboardMarkup().add(InlineKeyboardButton("⬅️ Назад в Инструменты", callback_data="menu_tools"))
        send_or_edit(chat_id, text, kb, msg_id)

    elif data == "tool_batch_f2p":
        bot.answer_callback_query(call.id, "Добавляю пакет из 5 игр...")
        pack = [
            ("730", "Counter-Strike 2"),
            ("570", "Dota 2"),
            ("440", "Team Fortress 2"),
            ("1172470", "Apex Legends"),
            ("291550", "Brawlhalla"),
        ]
        added_names = []
        for aid, gname in pack:
            sid = steam.get_subid_from_appid(aid)
            if sid and steam.add_free_license(sid):
                added_names.append(f"• {gname} (`{aid}`)")
                bot_status["claimed_count"] += 1
        library_cache.pop(chat_id, None)
        notify_user(
            chat_id,
            "✅ **Пакетное добавление завершено!**\n\n" + ("\n".join(added_names) or "Все игры уже на аккаунте."),
            reply_markup=get_main_keyboard(),
        )

    elif data == "tool_toggle_silent":
        steam.data["silent_mode"] = not steam.data.get("silent_mode", False)
        save_session(steam.data)
        state_txt = "Включен (без звука)" if steam.data["silent_mode"] else "Выключен (со звуком)"
        bot.answer_callback_query(call.id, f"Тихий режим: {state_txt}")
        send_or_edit(chat_id, format_dashboard_text(), get_main_keyboard(), msg_id)

    elif data == "tool_clear_cache":
        library_cache.clear()
        calc_state.clear()
        bot.answer_callback_query(call.id, "🧹 Кэш успешно очищен!", show_alert=True)

    # =================================================================
    # ПРОФИЛЬ И БАЛАНС
    # =================================================================
    elif data == "menu_profile":
        bot.answer_callback_query(call.id, "Проверяю баланс и профиль...")
        ok, balance = steam.check_health()
        kb = InlineKeyboardMarkup(row_width=1)
        kb.add(
            InlineKeyboardButton("📝 Изменить «О себе»", callback_data="change_bio"),
            InlineKeyboardButton("🏷 Изменить Никнейм в Steam", callback_data="change_nickname"),
            InlineKeyboardButton("👤 Изменить Настоящее имя", callback_data="change_realname"),
            InlineKeyboardButton("🔄 Сменить аккаунт Steam", callback_data="login_steam"),
            InlineKeyboardButton("⬅️ Назад в главное меню", callback_data="back_main"),
        )
        text = (
            f"👤 **Управление профилем и Кошельком Steam**\n\n"
            f"• Аккаунт: `{steam.data.get('account_name')}` (`{steam.data.get('steamid')}`)\n"
            f"• 💰 **Баланс кошелька Steam:** `{balance}`\n"
            f"• 🔐 Статус сессии: `{'Активна (OK)' if ok else 'Ошибка'}`"
        )
        send_or_edit(chat_id, text, kb, msg_id)

    elif data == "change_bio":
        msg = bot.send_message(chat_id, "✏️ Напишите новый текст для графы **«О себе»**:", parse_mode="Markdown")
        bot.register_next_step_handler(msg, step_change_bio)

    elif data == "change_nickname":
        msg = bot.send_message(chat_id, "🏷 Введите новый **Никнейм** для Steam:", parse_mode="Markdown")
        bot.register_next_step_handler(msg, step_change_nickname)

    elif data == "change_realname":
        msg = bot.send_message(chat_id, "👤 Введите новое **Настоящее имя** для профиля Steam:", parse_mode="Markdown")
        bot.register_next_step_handler(msg, step_change_realname)

    # =================================================================
    # РАЗДАЧИ, ТИХИЙ ПИНГ И СИСТЕМА
    # =================================================================
    elif data == "menu_giveaways":
        kb = InlineKeyboardMarkup(row_width=1)
        kb.add(
            InlineKeyboardButton("🔍 Проверить 100% раздачи сейчас", callback_data="check_now"),
            InlineKeyboardButton("⬅️ Назад в главное меню", callback_data="back_main"),
        )
        text = (
            f"🎁 **Автосбор платных игр со 100% скидкой**\n\n"
            f"• Автопроверка: `каждые 10 часов`\n"
            f"• Последняя проверка: `{bot_status['last_check']}`\n"
            f"• Забрано за сеанс: `{bot_status['claimed_count']}`"
        )
        send_or_edit(chat_id, text, kb, msg_id)

    elif data == "check_now":
        bot.answer_callback_query(call.id, "Проверяю раздачи...")
        count = check_giveaways()
        if count == 0:
            notify_user(chat_id, "ℹ️ Новых неактивированных раздач сейчас нет.", reply_markup=get_main_keyboard())

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
            InlineKeyboardButton("🔄 Обновить", callback_data="menu_ping"),
            InlineKeyboardButton("⚡ Пингануть сейчас", callback_data="force_ping"),
            InlineKeyboardButton("⏱ Пинг: 4 мин", callback_data="ping_int_4"),
            InlineKeyboardButton("⏱ Пинг: 14 мин", callback_data="ping_int_14"),
            InlineKeyboardButton("🌐 Сменить URL", callback_data="set_render_url"),
            InlineKeyboardButton("⬅️ Назад", callback_data="back_main"),
        )
        text = (
            f"📡 **Статус тихого самопинга (Без уведомлений)**\n\n"
            f"🟢 Аптайм воркера: `{uptime_min} мин.`\n"
            f"🌐 URL: `{steam.data.get('render_url')}/ping`\n"
            f"⏱ Интервал пинга: `каждые {bot_status['ping_interval_min']} мин.`\n"
            f"🕒 Последний пинг: **{ago_str}** (`{bot_status['last_ping_time']}`)\n"
            f"📊 Всего пингов: `{bot_status['ping_count']}`\n\n"
            f"📜 **Журнал последних пингов:**\n{history_lines}"
        )
        send_or_edit(chat_id, text, kb, msg_id)

    elif data in ("ping_int_4", "ping_int_14"):
        bot_status["ping_interval_min"] = 4 if data == "ping_int_4" else 14
        bot.answer_callback_query(call.id, f"Интервал пинга: {bot_status['ping_interval_min']} мин.")
        call.data = "menu_ping"
        handle_callbacks(call)

    elif data == "force_ping":
        do_single_ping()
        bot.answer_callback_query(call.id, f"Пинг: {bot_status['last_ping_code']}")
        call.data = "menu_ping"
        handle_callbacks(call)

    elif data == "menu_system":
        gh_repo = steam.data.get("gh_repo") or tok_config.get("gh_repo") or "Не настроен"
        gh_path = steam.data.get("gh_path") or tok_config.get("gh_path") or "main.py"
        kb = InlineKeyboardMarkup(row_width=1)
        kb.add(
            InlineKeyboardButton("🔄 Обновить код бота (Файл / Сообщения + 1111)", callback_data="update_bot"),
            InlineKeyboardButton("🐙 Настроить авто-отправку в GitHub", callback_data="gh_setup"),
            InlineKeyboardButton("🚀 Отправить текущий код в GitHub сейчас", callback_data="gh_push_current"),
            InlineKeyboardButton("⏪ Откатить к резервной копии (Rollback)", callback_data="sys_rollback"),
            InlineKeyboardButton("📥 Скачать текущий файл кода (.py)", callback_data="sys_download_code"),
            InlineKeyboardButton("💾 Получить SESSION_BASE64", callback_data="get_b64"),
            InlineKeyboardButton("📥 Вставить SESSION_BASE64", callback_data="import_b64"),
            InlineKeyboardButton("⬅️ Назад в главное меню", callback_data="back_main"),
        )
        text = (
            f"⚙️ **Система и Автообновление GitHub**\n\n"
            f"• Токен бота хранится в `{TOK_FILE}`.\n"
            f"• Текущая сборка: `{steam.data.get('last_update_time', 'Базовая')}`\n"
            f"• 🐙 GitHub Репозиторий: `{gh_repo}` (файл `{gh_path}`)\n"
            f"• Статус пуша в GitHub: `{bot_status['last_gh_push']}`\n"
            f"• 🌐 Сайт управления: `{steam.data.get('render_url')}`"
        )
        send_or_edit(chat_id, text, kb, msg_id)

    elif data == "sys_download_code":
        bot.answer_callback_query(call.id, "Отправляю текущий файл кода...")
        with open(os.path.abspath(__file__), "rb") as f:
            bot.send_document(chat_id, f, caption="📄 Текущий рабочий файл бота", parse_mode="Markdown")

    elif data == "login_steam":
        msg = bot.send_message(chat_id, "🔑 **Шаг 1 из 3.** Отправьте ваш **логин Steam**:", parse_mode="Markdown")
        bot.register_next_step_handler(msg, step_get_username)

    elif data == "import_b64":
        msg = bot.send_message(chat_id, "📥 Отправьте строку `SESSION_BASE64`:", parse_mode="Markdown")
        bot.register_next_step_handler(msg, step_import_b64)

    elif data == "get_b64":
        b64 = export_session_b64(steam.data)
        notify_user(chat_id, f"💾 **Ваш `SESSION_BASE64`:**\n\n`{b64}`", reply_markup=get_main_keyboard())

    elif data == "test_f2p":
        kb = InlineKeyboardMarkup(row_width=2)
        kb.add(
            InlineKeyboardButton("The Sims 4", callback_data="f2p_1222670"),
            InlineKeyboardButton("PUBG", callback_data="f2p_578080"),
            InlineKeyboardButton("Brawlhalla", callback_data="f2p_291550"),
            InlineKeyboardButton("Dota 2", callback_data="f2p_570"),
            InlineKeyboardButton("🔢 Ввести свой AppID", callback_data="f2p_custom"),
            InlineKeyboardButton("⬅️ Назад", callback_data="menu_f2p_farm"),
        )
        send_or_edit(chat_id, "🎮 Выберите бесплатную игру или введите свой `AppID`:", kb, msg_id)

    elif data.startswith("f2p_"):
        target = data.replace("f2p_", "")
        if target == "custom":
            msg = bot.send_message(chat_id, "🔢 Отправьте **AppID** бесплатной игры:", parse_mode="Markdown")
            bot.register_next_step_handler(msg, step_custom_f2p)
        else:
            bot.answer_callback_query(call.id, "Добавляю игру...")
            add_f2p_game(chat_id, target)

    elif data == "set_render_url":
        msg = bot.send_message(chat_id, "🌐 Отправьте ссылку на ваш Render-сервис:", parse_mode="Markdown")
        bot.register_next_step_handler(msg, step_set_render_url)


# ----------------- ОБРАБОТЧИКИ ШАГОВ ИНСТРУМЕНТОВ И GITHUB -----------------
def step_setup_github(message):
    chat_id = message.chat.id
    parts = message.text.strip().split()
    try:
        bot.delete_message(chat_id, message.message_id)
    except Exception:
        pass
    if len(parts) < 2:
        notify_user(chat_id, "❌ Нужно указать хотя бы Токен и Репозиторий (`owner/repo`).", get_main_keyboard())
        return
    gh_tok = parts[0].strip()
    gh_rep = parts[1].strip().replace("https://github.com/", "").strip("/")
    gh_pth = parts[2].strip() if len(parts) >= 3 else os.path.basename(os.path.abspath(__file__))

    steam.data["gh_token"] = gh_tok
    steam.data["gh_repo"] = gh_rep
    steam.data["gh_path"] = gh_pth
    save_session(steam.data)

    tok_config["gh_token"] = gh_tok
    tok_config["gh_repo"] = gh_rep
    tok_config["gh_path"] = gh_pth
    save_tok_config(tok_config)

    # Сразу пробуем запушить текущий файл для проверки!
    with open(os.path.abspath(__file__), "r", encoding="utf-8") as f:
        cur_code = f.read()
    ok, rep = push_code_to_github(cur_code)
    notify_user(
        chat_id,
        f"🐙 **Настройки GitHub сохранены!**\n"
        f"• Репозиторий: `{gh_rep}` | Файл: `{gh_pth}`\n"
        f"• Проверочный пуш: {rep}",
        get_main_keyboard(),
    )


def step_f2p_custom_count(message):
    chat_id = message.chat.id
    raw_n = re.sub(r"\D", "", message.text.strip())
    cnt = max(1, min(35, int(raw_n))) if raw_n else 5
    notify_user(chat_id, f"⏳ Ищу и добавляю **{cnt} рандомных Free-to-Play игр**...")

    def _run():
        added = steam.add_random_f2p_batch(cnt)
        library_cache.pop(chat_id, None)
        if added:
            notify_user(chat_id, f"🎉 **Добавлено {len(added)} игр:**\n\n" + "\n".join(added), get_main_keyboard())
        else:
            notify_user(chat_id, "ℹ️ Новых игр на этой странице не найдено, попробуйте еще раз!", get_main_keyboard())

    threading.Thread(target=_run, daemon=True).start()


def step_tool_players(message):
    chat_id = message.chat.id
    app_id = re.sub(r"\D", "", message.text.strip())
    if not app_id:
        notify_user(chat_id, "❌ Введите корректный AppID.", get_main_keyboard())
        return
    cnt = steam.get_live_player_count(app_id)
    info = steam.get_game_price_info(app_id)
    name = info["name"] if info else f"App {app_id}"
    notify_user(
        chat_id,
        f"👥 **Онлайн в «{name}» (`{app_id}`):**\n🎮 Сейчас в игре: **`{cnt if cnt is not None else 'Н/Д'} чел.`**",
        get_main_keyboard(),
    )


def step_tool_regional_prices(message):
    chat_id = message.chat.id
    m = re.search(r"/app/(\d+)", message.text)
    app_id = m.group(1) if m else re.sub(r"\D", "", message.text.strip())
    if not app_id:
        notify_user(chat_id, "❌ Введите корректный AppID.", get_main_keyboard())
        return
    name, lines = steam.compare_regional_prices(app_id)
    notify_user(
        chat_id,
        f"🌍 **Сравнение цен «{name}» (`{app_id}`):**\n\n" + "\n".join(lines),
        get_main_keyboard(),
    )


def step_tool_free_dlc(message):
    chat_id = message.chat.id
    app_id = re.sub(r"\D", "", message.text.strip())
    if not app_id:
        notify_user(chat_id, "❌ Введите корректный AppID.", get_main_keyboard())
        return
    notify_user(chat_id, f"⏳ Проверяю и активирую бесплатные DLC для AppID `{app_id}`...")
    added = steam.add_free_dlcs_for_game(app_id)
    if added:
        notify_user(chat_id, f"🎁 **Добавлены бесплатные DLC ({len(added)} шт.):**\n\n" + "\n".join(added), get_main_keyboard())
    else:
        notify_user(chat_id, "ℹ️ Бесплатных DLC не найдено (или они уже есть на аккаунте).", get_main_keyboard())


def step_tool_game_news(message):
    chat_id = message.chat.id
    app_id = re.sub(r"\D", "", message.text.strip())
    news = steam.get_game_news(app_id) if app_id else []
    notify_user(
        chat_id,
        f"📰 **Последние новости игры (`{app_id}`):**\n\n" + ("\n\n".join(news) if news else "Новостей не найдено."),
        get_main_keyboard(),
    )


def step_tool_achievements(message):
    chat_id = message.chat.id
    app_id = re.sub(r"\D", "", message.text.strip())
    ach = steam.get_global_achievements(app_id) if app_id else None
    if not ach:
        notify_user(chat_id, "ℹ️ Достижения для этой игры не найдены.", get_main_keyboard())
        return
    easy = "\n".join([f"• `{a['name']}` — **{float(a['percent']):.1f}%**" for a in ach["easiest"]])
    rare = "\n".join([f"• `{a['name']}` — **{float(a['percent']):.2f}%**" for a in ach["rarest"]])
    notify_user(
        chat_id,
        f"🏆 **Статистика достижений (`{app_id}`, всего `{ach['total']}`):**\n\n"
        f"🟢 **Самые частые:**\n{easy}\n\n💎 **Самые редкие:**\n{rare}",
        get_main_keyboard(),
    )


def step_tool_sys_reqs(message):
    chat_id = message.chat.id
    app_id = re.sub(r"\D", "", message.text.strip())
    info = steam.get_game_price_info(app_id) if app_id else None
    if not info:
        notify_user(chat_id, "❌ Игра не найдена.", get_main_keyboard())
        return
    notify_user(
        chat_id,
        f"💻 **Характеристики и Рейтинг «{info['name']}» (`{app_id}`):**\n\n"
        f"⭐ **Metacritic:** `{info['metacritic']}` | 💬 **Отзывов:** `{info['recommendations']:,}`\n"
        f"🎭 **Жанры:** `{info['genres']}`\n"
        f"💰 **Цена:** `{info['final_formatted']}`\n\n"
        f"🔹 **Минимальные требования:**\n{info['pc_min']}\n\n"
        f"🔸 **Рекомендуемые:**\n{info['pc_rec']}",
        get_main_keyboard(),
    )


def step_tool_inspect_sub(message):
    chat_id = message.chat.id
    sub_id = re.sub(r"\D", "", message.text.strip())
    info = steam.inspect_package_subid(sub_id) if sub_id else None
    if not info:
        notify_user(chat_id, "❌ Пакет SubID не найден.", get_main_keyboard())
        return
    apps_str = "\n".join([f"• {a}" for a in info["apps"][:15]]) or "Не указаны"
    notify_user(
        chat_id,
        f"📦 **Пакет `{info['subid']}` — {info['name']}**\n"
        f"💰 Цена: `{info['price_kzt']}`\n\n**Состав пакета:**\n{apps_str}",
        get_main_keyboard(),
    )


def step_add_note(message):
    steam.data.setdefault("notes", []).append(message.text.strip())
    save_session(steam.data)
    notify_user(message.chat.id, "✅ Заметка сохранена!", get_main_keyboard())


# ----------------- КАЛЬКУЛЯТОР СКИДОК И АВТОПОКУПКА -----------------
def step_calc_get_appid(message):
    chat_id = message.chat.id
    text = message.text.strip()
    m = re.search(r"/app/(\d+)", text)
    app_id = m.group(1) if m else re.sub(r"\D", "", text)
    if not app_id:
        notify_user(chat_id, "❌ Не удалось распознать AppID.", get_main_keyboard())
        return
    show_game_and_ask_discount(chat_id, app_id)


def show_game_and_ask_discount(chat_id, app_id):
    info = steam.get_game_price_info(app_id)
    if not info:
        notify_user(chat_id, f"❌ Игра с AppID `{app_id}` не найдена.", get_main_keyboard())
        return

    if info["is_free"]:
        kb = InlineKeyboardMarkup().add(
            InlineKeyboardButton("➕ Добавить в библиотеку бесплатно", callback_data=f"f2p_{app_id}"),
            InlineKeyboardButton("⬅️ В главное меню", callback_data="back_main"),
        )
        notify_user(chat_id, f"🎮 **{info['name']}** (`{app_id}`)\n💰 Игра уже **бесплатная (0 ₸)**!", kb)
        return

    calc_state[chat_id] = info
    cur_sale_txt = (
        f"🔥 Сейчас со скидкой **-{info['discount']}%**: `{info['final_kzt']:g} ₸` (обычная: `{info['initial_kzt']:g} ₸`)"
        if info["discount"] > 0
        else f"💰 Текущая цена в Steam: **`{info['initial_kzt']:g} ₸`** (без скидки)"
    )

    kb = InlineKeyboardMarkup(row_width=2)
    kb.add(
        InlineKeyboardButton("🛒 Автобай -30%", callback_data=f"watch_set_autobuy_{app_id}_30"),
        InlineKeyboardButton("🛒 Автобай -50%", callback_data=f"watch_set_autobuy_{app_id}_50"),
        InlineKeyboardButton("🛒 Автобай -75%", callback_data=f"watch_set_autobuy_{app_id}_75"),
        InlineKeyboardButton("🔔 Любая скидка", callback_data=f"watch_set_any_{app_id}_1"),
    )
    kb.add(InlineKeyboardButton("⬅️ Отмена", callback_data="back_main"))

    msg = bot.send_message(
        chat_id,
        f"🎮 **Игра найдена: {info['name']}** (AppID: `{app_id}`)\n"
        f"{cur_sale_txt}\n\n"
        f"🔢 **Введите желаемый процент скидки (от `1` до `99`, например `50`):**",
        parse_mode="Markdown",
        reply_markup=kb,
    )
    bot.register_next_step_handler(msg, step_calc_discount_percent)


def step_calc_discount_percent(message):
    chat_id = message.chat.id
    info = calc_state.get(chat_id)
    if not info:
        notify_user(chat_id, "⚠️ Выберите игру заново.", get_main_keyboard())
        return

    raw_num = re.sub(r"\D", "", message.text.strip())
    if not raw_num:
        notify_user(chat_id, "❌ Введите число от 1 до 99.", get_main_keyboard())
        return

    target_disc = max(1, min(99, int(raw_num)))
    base_kzt = info["initial_kzt"]
    discounted_kzt = round(base_kzt * (100 - target_disc) / 100.0, 2)
    saved_kzt = round(base_kzt - discounted_kzt, 2)
    app_id = info["appid"]

    kb = InlineKeyboardMarkup(row_width=1)
    kb.add(
        InlineKeyboardButton(
            f"🛒 Автопокупка (через 5–10 мин) при скидке -{target_disc}%",
            callback_data=f"watch_set_autobuy_{app_id}_{target_disc}",
        ),
        InlineKeyboardButton(
            f"🔔 Только уведомить при скидке -{target_disc}%",
            callback_data=f"watch_set_notify_{app_id}_{target_disc}",
        ),
        InlineKeyboardButton("🔔 Просто уведомить при ЛЮБОЙ скидке", callback_data=f"watch_set_any_{app_id}_1"),
        InlineKeyboardButton("⬅️ Назад в главное меню", callback_data="back_main"),
    )

    notify_user(
        chat_id,
        f"🧮 **Расчет скидки -{target_disc}% в тенге (₸):**\n\n"
        f"🎮 Игра: **{info['name']}** (`{app_id}`)\n"
        f"💵 Полная цена: `{base_kzt:g} ₸`\n"
        f"🏷 **Цена со скидкой -{target_disc}%:** **`{discounted_kzt:g} ₸`**\n"
        f"💰 Экономия: `{saved_kzt:g} ₸`\n\n"
        f"👇 **Выберите действие при появлении скидки -{target_disc}%:**",
        reply_markup=kb,
    )


def trigger_delayed_autobuy(chat_id, app_id, game_name, cur_disc):
    if app_id in bot_status["autobuy_tasks"]:
        return
    bot_status["autobuy_tasks"].add(app_id)

    delay_sec = random.randint(300, 600)
    delay_min = round(delay_sec / 60, 1)

    notify_user(
        chat_id,
        f"🎯 **Обнаружена целевая скидка -{cur_disc}% на «{game_name}»!**\n"
        f"⏳ Запущен таймер автопокупки: покупка через **{delay_min} мин.**",
    )

    time.sleep(delay_sec)
    res = steam.auto_purchase_game(app_id)
    bot_status["autobuy_tasks"].discard(app_id)

    notify_user(
        chat_id,
        f"🛒 **Результат автопокупки «{game_name}» (`{app_id}`):**\n\n"
        f"{res.get('msg')}\n"
        f"🔗 https://store.steampowered.com/app/{app_id}/",
    )


# ----------------- ПОИСК, КЛЮЧИ, ПРОФИЛЬ, ВХОД -----------------
def step_search_game(message):
    chat_id = message.chat.id
    query = message.text.strip()
    results = steam.search_games_by_name(query)
    if not results:
        notify_user(chat_id, f"❌ По запросу «{query}» ничего не найдено.", get_main_keyboard())
        return

    kb = InlineKeyboardMarkup(row_width=1)
    lines = [f"🔎 **Результаты поиска по запросу «{query}»:**\n"]
    for r in results:
        lines.append(f"• **{r['name']}** — `{r['price_str']}` (AppID: `{r['appid']}`)")
        kb.add(
            InlineKeyboardButton(
                f"🧮 Калькулятор / Автобай: {r['name']} ({r['price_str']})",
                callback_data=f"calc_from_appid_{r['appid']}",
            )
        )
    kb.add(InlineKeyboardButton("⬅️ Назад в главное меню", callback_data="back_main"))
    notify_user(chat_id, "\n".join(lines), kb)


def step_redeem_key(message):
    chat_id = message.chat.id
    res = steam.redeem_cd_key(message.text.strip())
    if res.get("ok"):
        library_cache.pop(chat_id, None)
        notify_user(chat_id, f"✅ **Ключ активирован!**\n🎮 Игра: **{res.get('games') or 'Добавлена'}**", get_main_keyboard())
    else:
        notify_user(chat_id, "❌ Не удалось активировать ключ.", get_main_keyboard())


def step_change_bio(message):
    val = message.text.strip()
    if steam.update_profile_field(summary=val):
        notify_user(message.chat.id, f"✅ **Графа «О себе» изменена:** `{val}`", get_main_keyboard())
    else:
        notify_user(message.chat.id, "❌ Ошибка обновления.", get_main_keyboard())


def step_change_nickname(message):
    val = message.text.strip()
    if steam.update_profile_field(persona_name=val):
        notify_user(message.chat.id, f"✅ **Никнейм изменен на:** `{val}`", get_main_keyboard())
    else:
        notify_user(message.chat.id, "❌ Ошибка смены никнейма.", get_main_keyboard())


def step_change_realname(message):
    val = message.text.strip()
    if steam.update_profile_field(real_name=val):
        notify_user(message.chat.id, f"✅ **Настоящее имя изменено на:** `{val}`", get_main_keyboard())
    else:
        notify_user(message.chat.id, "❌ Ошибка изменения имени.", get_main_keyboard())


def step_get_username(message):
    username = message.text.strip()
    pending_auth[message.chat.id] = {"username": username}
    msg = bot.send_message(message.chat.id, f"🔒 **Шаг 2 из 3.** Отправьте **пароль** от `{username}`:", parse_mode="Markdown")
    bot.register_next_step_handler(msg, step_get_password)


def step_get_password(message):
    chat_id = message.chat.id
    password = message.text.strip()
    try:
        bot.delete_message(chat_id, message.message_id)
    except Exception:
        pass
    if chat_id not in pending_auth:
        return
    pending_auth[chat_id]["password"] = password
    msg = bot.send_message(
        chat_id,
        "🛡 **Шаг 3 из 3.** Откройте приложение Steam → **Steam Guard (щит)** → отправьте **5-значный код**:",
        parse_mode="Markdown",
    )
    bot.register_next_step_handler(msg, step_get_guard_code)


def step_get_guard_code(message):
    chat_id = message.chat.id
    guard_code = message.text.strip()
    auth_data = pending_auth.pop(chat_id, None)
    if not auth_data or "password" not in auth_data:
        return
    bot.send_message(chat_id, "⚡ Выполняю пакетный вход...")
    res = steam.atomic_login(auth_data["username"], auth_data["password"], guard_code)
    if res.get("ok"):
        notify_user(chat_id, f"✅ **Успешный вход в `{steam.data.get('account_name')}`!**", get_main_keyboard())
    else:
        notify_user(chat_id, f"❌ **Ошибка:** {res.get('error')}", get_main_keyboard())


def step_import_b64(message):
    chat_id = message.chat.id
    try:
        decoded = json.loads(base64.b64decode(message.text.strip()).decode("utf-8"))
        decoded["admin_id"] = message.from_user.id
        if "render_url" not in decoded:
            decoded["render_url"] = tok_config["render_url"]
        steam.data = decoded
        save_session(steam.data)
        ok, bal = steam.check_health()
        notify_user(chat_id, f"✅ **Сессия импортирована!** Баланс: `{bal}`", get_main_keyboard())
    except Exception as e:
        notify_user(chat_id, f"❌ Ошибка: {e}", get_main_keyboard())


def step_custom_f2p(message):
    app_id = re.sub(r"\D", "", message.text.strip())
    if app_id:
        add_f2p_game(message.chat.id, app_id)


def add_f2p_game(chat_id, app_id):
    sub_id = steam.get_subid_from_appid(app_id)
    if not sub_id:
        notify_user(chat_id, f"❌ Бесплатный SubID для `{app_id}` не найден.", get_main_keyboard())
        return
    if steam.add_free_license(sub_id):
        bot_status["claimed_count"] += 1
        bot_status["f2p_added_count"] += 1
        library_cache.pop(chat_id, None)
        notify_user(chat_id, f"✅ **Игра добавлена в библиотеку!** (AppID: `{app_id}`)", get_main_keyboard())
    else:
        notify_user(chat_id, "❌ Ошибка активации лицензии.", get_main_keyboard())


def step_set_render_url(message):
    url = message.text.strip().rstrip("/")
    if not url.startswith("http"):
        url = "https://" + url
    steam.data["render_url"] = url
    save_session(steam.data)
    tok_config["render_url"] = url
    save_tok_config(tok_config)
    notify_user(message.chat.id, f"✅ URL самопинга сохранен: `{url}/ping`", get_main_keyboard())


# ----------------- ФОНОВЫЕ ЦИКЛЫ (РАЗДАЧИ, СКИДКИ 15 МИН, F2P ФАРМ 15 МИН) -----------------
def check_discounts(force_report=False):
    watchlist = steam.data.get("watchlist", {})
    if not watchlist:
        return ""

    bot_status["last_discount_check"] = time.strftime("%H:%M:%S")
    admin_id = steam.data.get("admin_id")
    report_lines = ["📊 **Текущие цены в тенге (₸) по вашему радару:**\n"]

    for app_id, saved in list(watchlist.items()):
        info = steam.get_game_price_info(app_id)
        if not info:
            continue

        cur_disc = info["discount"]
        prev_disc = saved.get("last_discount", 0)
        target_disc = saved.get("target_disc", 1)
        mode = saved.get("mode", "notify")

        if cur_disc > 0:
            report_lines.append(
                f"🔥 **{info['name']}** — **-{cur_disc}%** (`{info['final_kzt']:g} ₸` вместо `{info['initial_kzt']:g} ₸`) [Цель: `-{target_disc}%`]"
            )
        else:
            report_lines.append(
                f"• **{info['name']}** — без скидки (`{info['initial_kzt']:g} ₸`) [Цель: `-{target_disc}%`]"
            )

        if cur_disc >= target_disc and cur_disc != prev_disc and admin_id:
            if mode == "autobuy":
                threading.Thread(
                    target=trigger_delayed_autobuy,
                    args=(admin_id, app_id, info["name"], cur_disc),
                    daemon=True,
                ).start()
            else:
                notify_user(
                    admin_id,
                    f"🔥 **СКИДКА ДОСТИГЛА ЦЕЛИ (-{cur_disc}%)!**\n\n"
                    f"🎮 **{info['name']}**\n"
                    f"🏷 Новая цена: **`{info['final_kzt']:g} ₸`** (было `{info['initial_kzt']:g} ₸`)\n"
                    f"🔗 https://store.steampowered.com/app/{app_id}/",
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
            if steam.is_configured():
                check_discounts(force_report=False)
        except Exception:
            pass


def f2p_auto_all_loop():
    while True:
        time.sleep(F2P_FARM_INTERVAL)
        try:
            if steam.is_configured() and bot_status["f2p_auto_all_enabled"]:
                bot_status["last_f2p_check"] = time.strftime("%H:%M:%S")
                added = steam.add_random_f2p_batch(target_count=8, sequential=True)
                admin_id = steam.data.get("admin_id")
                if added and admin_id:
                    notify_user(
                        admin_id,
                        f"🎮 **Авто-фарм F2P (15 мин): добавлено {len(added)} новых бесплатных игр!**\n\n"
                        + "\n".join(added),
                    )
        except Exception:
            pass


def check_giveaways():
    if not steam.is_configured():
        return 0
    claimed = load_json_set(CLAIMED_FILE)
    bot_status["last_check"] = time.strftime("%Y-%m-%d %H:%M:%S")
    added = 0
    admin_id = steam.data.get("admin_id")

    try:
        resp = requests.get(
            "https://www.gamerpower.com/api/giveaways?platform=steam&type=game",
            timeout=15,
        )
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
                    save_json_set(CLAIMED_FILE, claimed)
                    added += 1
                    bot_status["claimed_count"] += 1
                    if admin_id:
                        notify_user(
                            admin_id,
                            f"🎁 **Автоматически забрана платная игра по раздаче!**\n\n"
                            f"🎮 **{title}**\n"
                            f"📦 AppID: `{app_id}` (SubID: `{sub_id}`)\n"
                            f"🔗 https://store.steampowered.com/app/{app_id}/",
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


# =====================================================================
# ПОЛНОЦЕННЫЙ ВЕБ-САЙТ УПРАВЛЕНИЯ АККАУНТОМ НА RENDER.COM (ВСЕ ФУНКЦИИ БОТА)
# =====================================================================
app = Flask(__name__)
app.secret_key = secrets.token_hex(16)

HTML_DASHBOARD = """
<!DOCTYPE html>
<html lang="ru">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Steam Ultimate — Веб-Центр Управления</title>
    <style>
        :root { --bg: #0b1120; --card: #1e293b; --border: #334155; --accent: #0284c7; --accent-h: #0ea5e9; --text: #f8fafc; --muted: #94a3b8; }
        body { font-family: system-ui, -apple-system, sans-serif; background: var(--bg); color: var(--text); margin: 0; padding: 15px; }
        .container { max-width: 1180px; margin: 0 auto; }
        .topbar { display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 10px; border-bottom: 1px solid var(--border); padding-bottom: 14px; margin-bottom: 18px; }
        h1 { color: #38bdf8; margin: 0; font-size: 22px; }
        h3 { color: #38bdf8; margin-top: 0; margin-bottom: 10px; font-size: 17px; }
        .tabs { display: flex; gap: 8px; flex-wrap: wrap; margin-bottom: 18px; }
        .tab-btn { background: #1e293b; color: #cbd5e1; border: 1px solid var(--border); padding: 9px 15px; border-radius: 8px; cursor: pointer; font-weight: 600; }
        .tab-btn.active { background: var(--accent); color: #fff; border-color: #38bdf8; }
        .tab-pane { display: none; }
        .tab-pane.active { display: block; }
        .grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(320px, 1fr)); gap: 16px; margin-bottom: 16px; }
        .card { background: var(--card); border: 1px solid var(--border); border-radius: 12px; padding: 18px; box-shadow: 0 4px 15px rgba(0,0,0,0.25); }
        input, select, textarea { width: 100%; box-sizing: border-box; padding: 9px 11px; margin: 5px 0; border-radius: 8px; border: 1px solid #475569; background: #0f172a; color: #fff; font-size: 14px; }
        button { background: var(--accent); color: #fff; border: none; padding: 9px 14px; border-radius: 8px; font-weight: 600; cursor: pointer; margin-top: 5px; }
        button:hover { background: var(--accent-h); }
        .btn-green { background: #16a34a; }
        .btn-green:hover { background: #22c55e; }
        .btn-red { background: #e11d48; }
        .btn-red:hover { background: #f43f5e; }
        .ok { color: #4ade80; font-weight: bold; }
        .err { color: #f87171; font-weight: bold; }
        .alert { background: #0369a1; border: 1px solid #38bdf8; padding: 12px 16px; border-radius: 8px; margin-bottom: 16px; font-weight: 600; }
        .scroll-box { max-height: 310px; overflow-y: auto; background: #0f172a; padding: 10px; border-radius: 8px; font-size: 13px; border: 1px solid #1e293b; }
        .row-item { display: flex; justify-content: space-between; align-items: center; gap: 8px; padding: 7px 4px; border-bottom: 1px solid #1e293b; }
        .badge { font-size: 11px; padding: 3px 7px; border-radius: 5px; background: #334155; white-space: nowrap; }
    </style>
    <script>
        function showTab(tabId) {
            document.querySelectorAll('.tab-pane').forEach(el => el.classList.remove('active'));
            document.querySelectorAll('.tab-btn').forEach(el => el.classList.remove('active'));
            document.getElementById(tabId).classList.add('active');
            document.getElementById('btn-' + tabId).classList.add('active');
            localStorage.setItem('activeSteamTab', tabId);
        }
        window.onload = function() {
            let saved = localStorage.getItem('activeSteamTab') || 'tab-main';
            if (document.getElementById(saved)) showTab(saved);
        };
        function filterLib() {
            let q = document.getElementById('libSearch').value.toLowerCase();
            let t = document.getElementById('libFilter').value;
            document.querySelectorAll('.lib-row').forEach(row => {
                let name = row.getAttribute('data-name').toLowerCase();
                let rem = row.getAttribute('data-rem');
                let matchQ = name.includes(q);
                let matchT = (t === 'all') || (t === 'paid' && rem === '0') || (t === 'free' && rem === '1');
                row.style.display = (matchQ && matchT) ? 'flex' : 'none';
            });
        }
    </script>
</head>
<body>
<div class="container">
    <div class="topbar">
        <h1>🚀 Steam Ultimate — Веб-Панель Управления</h1>
        {% if logged_in %}
        <div style="display:flex; gap:10px; align-items:center;">
            <span style="font-size:13px; color:var(--muted);">👤 {{ account_name }} | 💰 <b>{{ balance }}</b></span>
            <form method="POST" action="/logout" style="margin:0;"><button type="submit" class="btn-red" style="margin:0;">Выйти</button></form>
        </div>
        {% endif %}
    </div>

    {% if web_msg %}
    <div class="alert">💡 {{ web_msg }}</div>
    {% endif %}

    {% if not logged_in %}
    <div class="card" style="max-width:420px; margin:40px auto;">
        <h3>🔐 Вход в Веб-Панель</h3>
        <p style="color:var(--muted); font-size:13px;">Пароль администратора из tok.json (по умолчанию: <b>admin123</b>)</p>
        <form method="POST" action="/login">
            <input type="password" name="password" placeholder="Пароль администратора" required>
            <button type="submit" style="width:100%;">Войти в панель управления</button>
        </form>
    </div>
    {% else %}

    <div class="tabs">
        <button id="btn-tab-main" class="tab-btn active" onclick="showTab('tab-main')">👤 Аккаунт и Профиль</button>
        <button id="btn-tab-games" class="tab-btn" onclick="showTab('tab-games')">🎮 Выбор Игр, Поиск и Фарм F2P</button>
        <button id="btn-tab-calc" class="tab-btn" onclick="showTab('tab-calc')">🧮 Калькулятор (₸) и Радар</button>
        <button id="btn-tab-stats" class="tab-btn" onclick="showTab('tab-stats')">📊 Статистика Игр и Аналитика</button>
        <button id="btn-tab-lib" class="tab-btn" onclick="showTab('tab-lib')">📚 Библиотека ({{ lib_items|length }})</button>
        <button id="btn-tab-sys" class="tab-btn" onclick="showTab('tab-sys')">🔄 Автообновление (GitHub) и Пинг</button>
    </div>

    <!-- ВКЛАДКА 1: АККАУНТ И ПРОФИЛЬ -->
    <div id="tab-main" class="tab-pane active">
        <div class="grid">
            <div class="card">
                <h3>👤 Статус Аккаунта и Редактор Профиля</h3>
                <p>Статус Steam: <span class="{{ 'ok' if steam_ok else 'err' }}">{{ 'Авторизован (' ~ account_name ~ ')' if steam_ok else 'Не авторизован' }}</span></p>
                <p>SteamID: <code>{{ steamid }}</code> | 💰 Баланс: <b>{{ balance }}</b></p>
                <p>🎁 Забрано раздач: <b>{{ status.claimed_count }}</b> | 🎮 F2P: <b>{{ status.f2p_added_count }}</b></p>
                <form method="POST" action="/web_action">
                    <input type="hidden" name="act" value="update_profile">
                    <input type="text" name="bio" placeholder="Новый текст «О себе» в профиле Steam">
                    <input type="text" name="nick" placeholder="Новый Никнейм (оставьте пустым, чтобы не менять)">
                    <input type="text" name="realname" placeholder="Новое Настоящее имя">
                    <button type="submit">📝 Сохранить изменения профиля</button>
                </form>
            </div>

            <div class="card">
                <h3>🔑 Вход в Steam / Активация Ключей</h3>
                <form method="POST" action="/web_action">
                    <input type="hidden" name="act" value="atomic_login">
                    <input type="text" name="username" placeholder="Логин Steam" required>
                    <input type="password" name="password" placeholder="Пароль Steam" required>
                    <input type="text" name="guard" placeholder="5-значный код Steam Guard из приложения" required>
                    <button type="submit" class="btn-green">⚡ Войти в аккаунт Steam</button>
                </form>
                <hr style="border-color:var(--border); margin:12px 0;">
                <form method="POST" action="/web_action">
                    <input type="hidden" name="act" value="redeem_key">
