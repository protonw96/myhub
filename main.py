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

# ----------------- ЗАГРУЗКА ТОКЕНА ИЗ TOK.JSON -----------------
TOK_FILE = "tok.json"
DEFAULT_RENDER_URL = "https://myhub-yhxu.onrender.com"


def load_tok_config():
    cfg = {
        "tg_bot_token": os.environ.get("TG_BOT_TOKEN", "ВСТАВЬТЕ_СЮДА_ТОКЕН_БОТА"),
        "render_url": os.environ.get("RENDER_EXTERNAL_URL", DEFAULT_RENDER_URL),
        "web_password": os.environ.get("WEB_PASSWORD", "admin123"),
    }
    if os.path.exists(TOK_FILE):
        try:
            with open(TOK_FILE, "r", encoding="utf-8") as f:
                file_cfg = json.load(f)
                tok = file_cfg.get("tg_bot_token", "")
                if tok and "ВСТАВЬТЕ" not in tok:
                    cfg["tg_bot_token"] = tok
                if file_cfg.get("render_url"):
                    cfg["render_url"] = file_cfg["render_url"]
                if file_cfg.get("web_password"):
                    cfg["web_password"] = file_cfg["web_password"]
        except Exception:
            pass
    else:
        try:
            with open(TOK_FILE, "w", encoding="utf-8") as f:
                json.dump(cfg, f, indent=2, ensure_ascii=False)
        except Exception:
            pass
    return cfg


tok_config = load_tok_config()
TG_BOT_TOKEN = tok_config["tg_bot_token"]

SESSION_FILE = "session.json"
CLAIMED_FILE = "claimed_games.json"
CLAIMED_F2P_FILE = "claimed_f2p_apps.json"
BACKUP_FILE = "main.py.bak"

CHECK_GIVEAWAYS_INTERVAL = 10 * 3600
CHECK_DISCOUNTS_INTERVAL = 900
F2P_FARM_INTERVAL = 900

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
    "web_msg": "",
}

pending_auth = {}
calc_state = {}
library_cache = {}
update_lock = threading.Lock()
update_sessions = {}
UPDATE_TRIGGER_CODES = {"1111", "0000", "обновить", "готово", "ok", "ок", "/apply", "update"}
web_game_stat_cache = ""


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
        except Exception:
            pass
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
            r = requests.get(rsa_url, params={"account_name": username}, headers={"User-Agent": USER_AGENT}, timeout=15).json()
            resp = r.get("response", {})
            if not resp.get("publickey_mod"):
                return {"ok": False, "error": "Не удалось получить RSA-ключ Steam."}
            mod = int(resp["publickey_mod"], 16)
            exp = int(resp["publickey_exp"], 16)
            timestamp = resp["timestamp"]
            pub_key = rsa.PublicKey(mod, exp)
            enc_pw = base64.b64encode(rsa.encrypt(password.encode("utf-8"), pub_key)).decode("utf-8")

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
            b_res = requests.post(begin_url, data=begin_payload, headers={"User-Agent": USER_AGENT}, timeout=15).json().get("response", {})
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
                data={"client_id": client_id, "steamid": steamid, "code": guard_code.strip().upper(), "code_type": code_type},
                headers={"User-Agent": USER_AGENT},
                timeout=15,
            )

            poll_url = "https://api.steampowered.com/IAuthenticationService/PollAuthSessionStatus/v1/"
            for _ in range(5):
                p_res = requests.post(poll_url, data={"client_id": client_id, "request_id": request_id}, headers={"User-Agent": USER_AGENT}, timeout=15).json().get("response", {})
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
            return {"ok": False, "error": "Код отклонен или IP заблокирован. Используйте SESSION_BASE64."}
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
            r = requests.get("https://store.steampowered.com/account/", cookies=self.get_cookies(), headers={"User-Agent": USER_AGENT}, allow_redirects=False, timeout=15)
            if r.status_code == 200:
                bot_status["steam_ok"] = True
                m = re.search(r'class="accountData price"[^>]*>(.*?)</div>', r.text, re.DOTALL)
                if not m:
                    m = re.search(r'id="header_wallet_balance"[^>]*>(.*?)</a>', r.text, re.DOTALL)
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
        r = requests.post(f"https://steamcommunity.com/profiles/{steamid}/edit/", data=payload, cookies=cookies, headers=headers, timeout=15)
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
            r = requests.get(url, cookies=self.get_cookies(), headers={"User-Agent": USER_AGENT}, timeout=15)
            if r.status_code != 200:
                return []
            records = []
            rows = re.findall(r'<tr class="wallet_table_row[^>]*>(.*?)</tr>', r.text, re.DOTALL)
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
        headers = {"User-Agent": USER_AGENT, "Referer": "https://store.steampowered.com/account/registerkey", "X-Requested-With": "XMLHttpRequest"}
        try:
            r = requests.post(url, data={"product_key": key_str.strip(), "sessionid": self.session_id}, cookies=self.get_cookies(), headers=headers, timeout=15).json()
            success = r.get("success") == 1
            receipt = r.get("purchase_receipt_info", {})
            items = receipt.get("line_items", [])
            game_names = ", ".join([i.get("line_item_description", "Игра") for i in items])
            return {"ok": success, "games": game_names, "raw": r}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def remove_license(self, package_id):
        url = "https://store.steampowered.com/account/removelicense"
        headers = {"User-Agent": USER_AGENT, "Referer": "https://store.steampowered.com/account/licenses/", "X-Requested-With": "XMLHttpRequest"}
        try:
            r = requests.post(url, data={"sessionid": self.session_id, "packageid": str(package_id)}, cookies=self.get_cookies(), headers=headers, timeout=15)
            return r.status_code == 200 and r.json().get("success") == 1
        except Exception:
            return False

    def get_subid_from_appid(self, app_id):
        try:
            page = requests.get(f"https://store.steampowered.com/app/{app_id}/", cookies=self.get_cookies(), headers={"User-Agent": USER_AGENT}, timeout=15).text
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
        headers = {"User-Agent": USER_AGENT, "Referer": "https://store.steampowered.com/", "X-Requested-With": "XMLHttpRequest"}
        u1 = f"https://store.steampowered.com/freelicense/addfreelicense/{sub_id}"
        r1 = requests.post(u1, data={"sessionid": self.session_id, "ajax": "true"}, cookies=cookies, headers=headers, timeout=15)
        u2 = "https://store.steampowered.com/checkout/addfreelicense"
        r2 = requests.post(u2, data={"action": "add_to_cart", "sessionid": self.session_id, "subid": str(sub_id)}, cookies=cookies, headers=headers, timeout=15)
        return r1.status_code == 200 or r2.status_code == 200

    def fetch_f2p_catalog_page(self, start_offset=0, count=50):
        url = "https://store.steampowered.com/search/results/"
        params = {"query": "", "start": start_offset, "count": count, "maxprice": "free", "category1": "998", "infinite": "1", "cc": "kz", "l": "russian"}
        try:
            r = requests.get(url, params=params, headers={"User-Agent": USER_AGENT}, timeout=15).json()
            html_block = r.get("results_html", "")
            matches = re.findall(r'data-ds-appid="(\d+)".*?<span class="title">(.*?)</span>', html_block, re.DOTALL)
            return [{"appid": appid.strip(), "name": html.unescape(title.strip())} for appid, title in matches]
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
            out = []
            for it in items:
                dt = time.strftime("%d.%m.%Y", time.localtime(it.get("date", time.time())))
                out.append(f"📰 `{dt}` — **{it.get('title', 'Новость')}**\n🔗 {it.get('url', '')}")
            return out
        except Exception:
            return []

    def compare_regional_prices(self, app_id):
        regions = [("🇰🇿 Казахстан", "kz"), ("🇺🇸 США", "us"), ("🇺🇦 Украина", "ua"), ("🇷🇺 Россия", "ru")]
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
            for dlc_id in data.get("dlc", [])[:15]:
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
            return {"total": len(achs), "easiest": achs_sorted[:4], "rarest": achs_sorted[-4:]}
        except Exception:
            return None

    def get_wishlist_items(self):
        steamid = self.data.get("steamid")
        if not steamid:
            return []
        url = f"https://store.steampowered.com/wishlist/profiles/{steamid}/wishlistdata/?p=0&cc=kz&l=russian"
        try:
            r = requests.get(url, cookies=self.get_cookies(), headers={"User-Agent": USER_AGENT}, timeout=15)
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
                disc = subs[0].get("discount_pct", 0) or 0 if subs else 0
                raw_p = subs[0].get("price", 0) if subs else 0
                price_txt = f"{int(raw_p)/100:g} ₸" if raw_p else "Без цены"
                items.append({"appid": str(appid), "name": html.unescape(name), "discount": disc, "price": price_txt})
            return items
        except Exception:
            return []

    def clear_steam_cart(self):
        cookies = self.get_cookies()
        cookies["shoppingCartGID"] = "-1"
        try:
            r = requests.post("https://store.steampowered.com/cart/", data={"action": "remove_all", "sessionid": self.session_id}, cookies=cookies, headers={"User-Agent": USER_AGENT}, timeout=15)
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
                final_kzt = t.get("final_price", 0) / 100.0
                out.append({"appid": str(t.get("id")), "name": t.get("name", "Игра"), "discount": t.get("discount_percent", 0), "price": f"{final_kzt:g} ₸" if final_kzt > 0 else "Бесплатно"})
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
            price = data.get("price", {})
            final_kzt = price.get("final", 0) / 100.0 if price else 0
            return {"subid": str(sub_id), "name": data.get("name", f"Package {sub_id}"), "apps": [a.get("name", "") for a in data.get("apps", [])], "price_kzt": f"{final_kzt:g} ₸" if final_kzt > 0 else "Бесплатно / 0 ₸"}
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
            base_info = {
                "appid": str(app_id),
                "subid": sub_id,
                "name": name,
                "genres": ", ".join([g.get("description", "") for g in data.get("genres", [])[:3]]) or "Игры",
                "pc_min": strip_html(pc_req.get("minimum", "Не указаны"))[:350],
                "pc_rec": strip_html(pc_req.get("recommended", "Не указаны"))[:350],
                "metacritic": data.get("metacritic", {}).get("score", "Н/Д"),
                "recommendations": data.get("recommendations", {}).get("total", 0),
            }
            if is_free:
                base_info.update({"is_free": True, "discount": 100, "initial_kzt": 0.0, "final_kzt": 0.0, "final_formatted": "Бесплатно (0 ₸)", "initial_formatted": "0 ₸"})
                return base_info
            if not price_ov:
                base_info.update({"is_free": False, "discount": 0, "initial_kzt": 0.0, "final_kzt": 0.0, "final_formatted": "Недоступно", "initial_formatted": "—"})
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
        try:
            r = requests.get(url, params={"term": query, "l": "russian", "cc": "kz"}, headers={"User-Agent": USER_AGENT}, timeout=15).json()
            results = []
            for it in r.get("items", [])[:6]:
                price_info = it.get("price", {})
                if price_info:
                    final_kzt = price_info.get("final", 0) / 100.0
                    disc = price_info.get("discount_percent", 0)
                    price_str = f"{final_kzt:g} ₸" + (f" (-{disc}%)" if disc > 0 else "")
                else:
                    price_str = "Бесплатно / Н/Д"
                results.append({"appid": str(it.get("id")), "name": it.get("name", "Игра"), "price_str": price_str})
            return results
        except Exception:
            return []

    def get_top_daily_specials(self):
        url = "https://store.steampowered.com/api/featuredcategories?cc=kz&l=ru"
        try:
            r = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=15).json()
            out = []
            for sp in r.get("specials", {}).get("items", [])[:8]:
                final_kzt = sp.get("final_price", 0) / 100.0
                init_kzt = sp.get("original_price", 0) / 100.0
                out.append({"appid": str(sp.get("id")), "name": sp.get("name", "Игра"), "discount": sp.get("discount_percent", 0), "final_kzt": f"{final_kzt:g} ₸", "init_kzt": f"{init_kzt:g} ₸"})
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
        headers = {"User-Agent": USER_AGENT, "Referer": f"https://store.steampowered.com/app/{app_id}/", "X-Requested-With": "XMLHttpRequest"}
        try:
            requests.post("https://store.steampowered.com/cart/", data={"action": "add_to_cart", "sessionid": self.session_id, "subid": str(sub_id)}, cookies=cookies, headers=headers, timeout=15)
            init_res = requests.post("https://store.steampowered.com/checkout/inittransaction/", data={"gidShoppingCart": -1, "sessionid": self.session_id, "PaymentMethod": "steamaccount", "bUseAccountCart": 1, "Country": "KZ"}, cookies=cookies, headers=headers, timeout=15).json()
            transid = init_res.get("transid")
            if transid:
                fin_res = requests.post("https://store.steampowered.com/checkout/finalizetransaction/", data={"transid": transid, "CardCVV2": ""}, cookies=cookies, headers=headers, timeout=15).json()
                if fin_res.get("success") == 22 or fin_res.get("bSuccess"):
                    return {"ok": True, "msg": "✅ Игра автоматически оплачена с баланса кошелька Steam и добавлена в библиотеку!"}
            return {"ok": True, "msg": "🛒 Игра добавлена в вашу корзину Steam! Подтвердите списание с кошелька."}
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
        bot.send_message(chat_id, text, parse_mode="Markdown", reply_markup=reply_markup, disable_notification=steam.data.get("silent_mode", False), disable_web_page_preview=True)
    except Exception:
        pass


# ----------------- ГЛАВНОЕ МЕНЮ TELEGRAM -----------------
def get_main_keyboard():
    kb = InlineKeyboardMarkup(row_width=2)
    if not steam.is_configured():
        kb.add(InlineKeyboardButton("🔑 Войти в Steam", callback_data="login_steam"), InlineKeyboardButton("📥 Вставить сессию", callback_data="import_b64"))
        kb.add(InlineKeyboardButton("📡 Статус и Пинг", callback_data="menu_ping"), InlineKeyboardButton("🔄 Обновить бота", callback_data="update_bot"))
    else:
        f2p_state = "🟢 ВКЛ" if bot_status["f2p_auto_all_enabled"] else "⚪"
        kb.add(
            InlineKeyboardButton("📚 Вся библиотека", callback_data="menu_library"),
            InlineKeyboardButton(f"🎮 Фарм F2P (Рандом/Все) {f2p_state}", callback_data="menu_f2p_farm"),
            InlineKeyboardButton("🧮 Калькулятор и Автобай", callback_data="menu_calc"),
            InlineKeyboardButton("📉 Радар скидок", callback_data="menu_discounts"),
            InlineKeyboardButton("🔎 Поиск игры в Steam", callback_data="menu_search"),
            InlineKeyboardButton("🔥 Скидки и Топ чарта (₸)", callback_data="menu_specials"),
            InlineKeyboardButton("🛠 20+ Инструментов Steam", callback_data="menu_tools"),
            InlineKeyboardButton("🎁 Автосбор 100% раздач", callback_data="menu_giveaways"),
            InlineKeyboardButton("👤 Профиль и Баланс", callback_data="menu_profile"),
            InlineKeyboardButton("📡 Тихий пинг", callback_data="menu_ping"),
            InlineKeyboardButton("🔄 Система / Автообновление", callback_data="menu_system"),
        )
    return kb


def format_dashboard_text():
    if not steam.is_configured():
        return f"👋 **Добро пожаловать в Steam Ultimate Bot!**\n\n📄 Токен загружен из `tok.json`.\n🌐 Сайт управления: `{tok_config['render_url']}`\nАвторизуйтесь в аккаунте Steam:"
    ago = f"{int(time.time() - bot_status['last_ping_ts'])} сек. назад" if bot_status["last_ping_ts"] > 0 else "Ожидание..."
    watchlist_count = len(steam.data.get("watchlist", {}))
    f2p_farm_str = "🟢 Активен (каждые 15 мин)" if bot_status["f2p_auto_all_enabled"] else "⏸ Выключен"
    upd_ver = steam.data.get("last_update_time", "Актуальная сборка")
    return (
        f"🤖 **Панель управления Steam Ultimate**\n\n"
        f"👤 Аккаунт: `{steam.data.get('account_name')}`\n"
        f"🏷 Версия: `{upd_ver}`\n"
        f"🎮 Авто-фарм всех F2P: `{f2p_farm_str}` (Добавлено: `{bot_status['f2p_added_count']}`)\n"
        f"📉 Игр на радаре / автопокупке: `{watchlist_count}`\n"
        f"🎁 Забрано раздач за сеанс: `{bot_status['claimed_count']}`\n"
        f"📡 Последний тихий пинг: `{ago}`\n"
        f"🌐 Веб-панель: [Открыть сайт управления]({steam.data.get('render_url')})\n\n"
        f"👇 Выберите нужный раздел:"
    )


def send_or_edit(chat_id, text, reply_markup, message_id=None):
    if message_id:
        try:
            bot.edit_message_text(text, chat_id, message_id, parse_mode="Markdown", reply_markup=reply_markup, disable_web_page_preview=True)
            return
        except Exception:
            pass
    bot.send_message(chat_id, text, parse_mode="Markdown", reply_markup=reply_markup, disable_web_page_preview=True)


# ----------------- ДВИЖОК АВТООБНОВЛЕНИЯ -----------------
def get_sorted_buffer_code(chat_id):
    sess = update_sessions.get(chat_id)
    if not sess or not sess.get("chunks"):
        return ""
    sorted_chunks = [txt for _, txt in sorted(sess["chunks"], key=lambda item: item[0])]
    return "\n".join(sorted_chunks)


def apply_new_bot_code(chat_id, new_code):
    new_code = new_code.strip()
    new_code = re.sub(r"^```[a-zA-Z]*\n", "", new_code)
    new_code = re.sub(r"\n```$", "", new_code)
    if len(new_code) < 100:
        if chat_id:
            notify_user(chat_id, "❌ Код пустой или слишком короткий.", get_main_keyboard())
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
            notify_user(chat_id, f"❌ **{err_text}**\nСтарый бот продолжает работать без сбоев!", get_main_keyboard())
        return False, err_text
    try:
        if os.path.exists(script_path):
            shutil.copyfile(script_path, BACKUP_FILE)
        with open(script_path, "w", encoding="utf-8") as f:
            f.write(new_code)
    except Exception as e:
        return False, str(e)
    upd_time = time.strftime("%d.%m %H:%M:%S")
    steam.data["last_update_time"] = f"Обновлен {upd_time}"
    steam.data["just_updated"] = upd_time
    save_session(steam.data)
    if chat_id:
        update_sessions.pop(chat_id, None)
        notify_user(chat_id, f"✅ **Код проверен и сохранен в `{script_name}` (`{len(new_code)}` симв.)!**\n🔄 Перезапускаю бота...")

    def _graceful_restart():
        time.sleep(1.0)
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
    return True, "Успешно обновлено!"


@bot.message_handler(content_types=["document"])
def handle_document_update(message):
    if not is_admin(message.from_user.id):
        return
    doc = message.document
    fname = (doc.file_name or "").lower()
    if fname.endswith(".py") or fname.endswith(".txt") or update_sessions.get(message.chat.id, {}).get("active"):
        bot.send_message(message.chat.id, f"📥 Скачиваю файл `{doc.file_name}` и проверяю код...", parse_mode="Markdown")
        try:
            file_info = bot.get_file(doc.file_id)
            file_url = f"https://api.telegram.org/file/bot{TG_BOT_TOKEN}/{file_info.file_path}"
            resp = requests.get(file_url, timeout=30)
            if resp.status_code == 200:
                apply_new_bot_code(message.chat.id, resp.content.decode("utf-8-sig", errors="replace"))
        except Exception as e:
            notify_user(message.chat.id, f"❌ Ошибка чтения файла: {e}", get_main_keyboard())


@bot.message_handler(func=lambda m: update_sessions.get(m.chat.id, {}).get("active", False), content_types=["text"])
def handle_chunked_update_text(message):
    chat_id = message.chat.id
    text_stripped = message.text.strip()
    low = text_stripped.lower()
    if low in ("отмена", "/cancel", "cancel"):
        update_sessions.pop(chat_id, None)
        notify_user(chat_id, "❎ Режим автообновления отменен.", get_main_keyboard())
        return
    if low in UPDATE_TRIGGER_CODES:
        with update_lock:
            full_code = get_sorted_buffer_code(chat_id)
            parts_cnt = len(update_sessions.get(chat_id, {}).get("chunks", []))
        if not full_code:
            notify_user(chat_id, "⚠️ Буфер пуст! Сначала отправьте код.")
            return
        bot.send_message(chat_id, f"🔑 Объединяю `{parts_cnt}` частей и обновляю...", parse_mode="Markdown")
        apply_new_bot_code(chat_id, full_code)
        return
    if (text_stripped.startswith("http://") or text_stripped.startswith("https://")) and "\n" not in text_stripped:
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
    is_valid = False
    try:
        clean_test = re.sub(r"^```[a-zA-Z]*\n|\n```$", "", full_code.strip())
        compile(clean_test, "main.py", "exec")
        is_valid = True
    except SyntaxError:
        pass
    kb = InlineKeyboardMarkup(row_width=1).add(
        InlineKeyboardButton(f"✅ Объединить и обновить ({parts_count} ч., {total_chars} симв.)", callback_data="upd_apply_now"),
        InlineKeyboardButton("🧹 Очистить буфер", callback_data="upd_clear_buf"),
        InlineKeyboardButton("❌ Отмена", callback_data="upd_cancel"),
    )
    st = "🟢 **Код валиден! Отправьте `1111` или нажмите кнопку ниже:**" if is_valid else "🟡 *Принимаю части... Когда отправите все, напишите `1111`:*"
    msg_text = f"📥 **Буфер автообновления:** `{parts_count} ч.` (`{total_chars}` симв.)\n\n{st}"
    with update_lock:
        status_mid = sess.get("status_msg_id")
        if status_mid:
            try:
                bot.edit_message_text(msg_text, chat_id, status_mid, parse_mode="Markdown", reply_markup=kb)
                return
            except Exception:
                pass
        sent = bot.send_message(chat_id, msg_text, parse_mode="Markdown", reply_markup=kb)
        sess["status_msg_id"] = sent.message_id


# ----------------- КОМАНДЫ И КОЛБЭКИ TELEGRAM -----------------
@bot.message_handler(commands=["start", "menu"])
def cmd_start(message):
    if not is_admin(message.from_user.id):
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
        kb = InlineKeyboardMarkup().add(InlineKeyboardButton("❌ Отменить обновление", callback_data="upd_cancel"))
        send_or_edit(
            chat_id,
            "🔄 **Автообновление активировано!**\n"
            "1️⃣ Скиньте файл `.py` документом.\n"
            "2️⃣ ИЛИ отправьте код сообщениями и напишите **`1111`**.\n"
            "3️⃣ ИЛИ обновите через веб-сайт на Render.",
            kb,
            msg_id,
        )

    elif data == "upd_apply_now":
        with update_lock:
            full_code = get_sorted_buffer_code(chat_id)
        if full_code:
            apply_new_bot_code(chat_id, full_code)

    elif data == "upd_clear_buf":
        with update_lock:
            update_sessions[chat_id] = {"active": True, "chunks": [], "status_msg_id": None}
        bot.answer_callback_query(call.id, "Буфер очищен!")

    elif data == "upd_cancel":
        update_sessions.pop(chat_id, None)
        send_or_edit(chat_id, format_dashboard_text(), get_main_keyboard(), msg_id)

    elif data == "sys_rollback":
        if os.path.exists(BACKUP_FILE):
            with open(BACKUP_FILE, "r", encoding="utf-8") as f:
                apply_new_bot_code(chat_id, f.read())
        else:
            bot.answer_callback_query(call.id, "Резервная копия еще не создана!", show_alert=True)

    elif data == "menu_f2p_farm":
        auto_state = "🟢 ВКЛЮЧЕН (каждые 15 мин)" if bot_status["f2p_auto_all_enabled"] else "⚪ ВЫКЛЮЧЕН"
        claimed_total = len(load_json_set(CLAIMED_F2P_FILE))
        kb = InlineKeyboardMarkup(row_width=1).add(
            InlineKeyboardButton("🎲 Добавить 5 рандомных F2P игр сейчас", callback_data="f2p_rand_5"),
            InlineKeyboardButton("🎲 Добавить 15 рандомных F2P игр сейчас", callback_data="f2p_rand_15"),
            InlineKeyboardButton("🔢 Указать свое число рандомных F2P игр", callback_data="f2p_rand_custom"),
            InlineKeyboardButton(f"♾️ Авто-сбор ВСЕХ F2P (15 мин): {'Выключить 🔴' if bot_status['f2p_auto_all_enabled'] else 'Включить 🟢'}", callback_data="f2p_toggle_all"),
            InlineKeyboardButton("🎯 Популярные F2P-хиты (CS2, Dota, PUBG)", callback_data="test_f2p"),
            InlineKeyboardButton("⬅️ Назад в главное меню", callback_data="back_main"),
        )
        send_or_edit(
            chat_id,
            f"🎮 **Генератор и Авто-сборщик всех Free-to-Play игр**\n\n"
            f"• Авто-сбор (каждые 15 мин): **{auto_state}**\n"
            f"• Последняя проверка: `{bot_status['last_f2p_check']}`\n"
            f"• Добавлено F2P за сеанс: `{bot_status['f2p_added_count']} шт.` (В базе: `{claimed_total} шт.`)",
            kb,
            msg_id,
        )

    elif data in ("f2p_rand_5", "f2p_rand_15"):
        cnt = 5 if data == "f2p_rand_5" else 15
        bot.answer_callback_query(call.id, f"Добавляю {cnt} случайных F2P игр...")
        notify_user(chat_id, f"⏳ Ищу в каталоге Steam и добавляю **{cnt} рандомных Free-to-Play игр**...")

        def _run_rand(n):
            added = steam.add_random_f2p_batch(n)
            library_cache.pop(chat_id, None)
            msg = f"🎉 **Добавлено {len(added)} рандомных F2P игр:**\n\n" + "\n".join(added) if added else "ℹ️ Игры на этой странице уже добавлены, попробуйте еще раз!"
            notify_user(chat_id, msg, get_main_keyboard())

        threading.Thread(target=_run_rand, args=(cnt,), daemon=True).start()

    elif data == "f2p_rand_custom":
        msg = bot.send_message(chat_id, "🔢 Сколько случайных Free-to-Play игр добавить? (1–35):")
        bot.register_next_step_handler(msg, step_f2p_custom_count)

    elif data == "f2p_toggle_all":
        bot_status["f2p_auto_all_enabled"] = not bot_status["f2p_auto_all_enabled"]
        steam.data["f2p_auto_all"] = bot_status["f2p_auto_all_enabled"]
        save_session(steam.data)
        call.data = "menu_f2p_farm"
        handle_callbacks(call)

    elif data == "menu_library":
        kb = InlineKeyboardMarkup(row_width=2).add(
            InlineKeyboardButton("📋 Все игры аккаунта", callback_data="lib_view_all_0"),
            InlineKeyboardButton("🔒 Только купленные", callback_data="lib_view_paid_0"),
            InlineKeyboardButton("❌ Только удаляемые", callback_data="lib_view_free_0"),
            InlineKeyboardButton("💎 Аналитика библиотеки", callback_data="tool_lib_stats"),
            InlineKeyboardButton("📄 Скачать список (.txt)", callback_data="lib_export_txt"),
            InlineKeyboardButton("🎲 Случайная игра", callback_data="tool_random_game"),
        ).add(InlineKeyboardButton("⬅️ Назад в главное меню", callback_data="back_main"))
        send_or_edit(chat_id, "📚 **Полная библиотека Steam**\n`🔒 Купленные` защищены от удаления, `❌ Бесплатные` можно удалить в 1 клик.", kb, msg_id)

    elif data.startswith("lib_view_"):
        parts = data.split("_")
        mode, page = parts[2], int(parts[3])
        if page == 0 or chat_id not in library_cache:
            bot.answer_callback_query(call.id, "Загружаю библиотеку из Steam...")
            library_cache[chat_id] = steam.get_all_licenses()
        all_items = library_cache.get(chat_id, [])
        items = [x for x in all_items if (not x["removable"] if mode == "paid" else (x["removable"] if mode == "free" else True))]
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
                kb.add(InlineKeyboardButton(f"❌ Удалить: {item['name']} ({item['date']})", callback_data=f"lib_del_{item['packageid']}_{mode}_{page}"))
            else:
                kb.add(InlineKeyboardButton(f"🔒 Куплено: {item['name']} ({item['date']})", callback_data=f"lib_locked_{idx}"))
        nav_btns = []
        if page > 0:
            nav_btns.append(InlineKeyboardButton("⬅️ Пред.", callback_data=f"lib_view_{mode}_{page-1}"))
        nav_btns.append(InlineKeyboardButton("🔄 Обновить", callback_data=f"lib_view_{mode}_0"))
        if page < total_pages - 1:
            nav_btns.append(InlineKeyboardButton("След. ➡️", callback_data=f"lib_view_{mode}_{page+1}"))
        kb.row(*nav_btns)
        kb.add(InlineKeyboardButton("⬅️ Назад в Библиотеку", callback_data="menu_library"))
        send_or_edit(chat_id, f"📚 **Игр в списке: `{len(items)}`** (Стр. `{page + 1}` из `{total_pages}`):", kb, msg_id)

    elif data.startswith("lib_locked_"):
        bot.answer_callback_query(call.id, "🔒 Это купленная игра (или ключ). Её нельзя удалить!", show_alert=True)

    elif data.startswith("lib_del_"):
        parts = data.split("_")
        pkg_id, mode, page = parts[2], parts[3], int(parts[4])
        if steam.remove_license(pkg_id):
            if chat_id in library_cache:
                library_cache[chat_id] = [x for x in library_cache[chat_id] if x["packageid"] != pkg_id]
            notify_user(chat_id, f"✅ Бесплатная лицензия `{pkg_id}` удалена!")
        call.data = f"lib_view_{mode}_{page}"
        handle_callbacks(call)

    elif data == "lib_export_txt":
        items = steam.get_all_licenses()
        lines = [f"{i}. {it['name']} | {'[Бесплатная]' if it['removable'] else '[Куплена 🔒]'} | {it['date']}" for i, it in enumerate(items, 1)]
        bio_bytes = io.BytesIO("\n".join(lines).encode("utf-8"))
        bio_bytes.name = "steam_library.txt"
        bot.send_document(chat_id, bio_bytes, caption=f"📄 Полный список игр (`{len(items)} шт.`)")

    elif data == "menu_calc":
        msg = bot.send_message(chat_id, "🧮 Отправьте **AppID** игры или ссылку на неё в Steam:", parse_mode="Markdown")
        bot.register_next_step_handler(msg, step_calc_get_appid)

    elif data.startswith("calc_from_appid_"):
        show_game_and_ask_discount(chat_id, data.replace("calc_from_appid_", ""))

    elif data.startswith("watch_set_"):
        parts = data.split("_")
        mode, app_id, target_disc = parts[2], parts[3], int(parts[4])
        info = steam.get_game_price_info(app_id)
        game_name = info["name"] if info else f"App {app_id}"
        cur_disc = info["discount"] if info else 0
        steam.data.setdefault("watchlist", {})[app_id] = {"name": game_name, "target_disc": 1 if mode == "any" else target_disc, "mode": mode, "last_discount": cur_disc}
        save_session(steam.data)
        if mode == "autobuy" and cur_disc >= target_disc and app_id not in bot_status["autobuy_tasks"]:
            threading.Thread(target=trigger_delayed_autobuy, args=(chat_id, app_id, game_name, cur_disc), daemon=True).start()
        notify_user(chat_id, f"✅ **Сохранено для «{game_name}» (`{app_id}`)!** (Цель: `-{target_disc}%`, Режим: `{mode}`)", get_main_keyboard())

    elif data == "menu_discounts":
        watchlist = steam.data.get("watchlist", {})
        kb = InlineKeyboardMarkup(row_width=1).add(
            InlineKeyboardButton("🧮 Рассчитать скидку в ₸ / Добавить игру", callback_data="menu_calc"),
            InlineKeyboardButton("⭐ Импортировать из Списка желаемого Steam", callback_data="tool_wishlist"),
            InlineKeyboardButton("🔍 Проверить цены сейчас", callback_data="disc_check_now"),
        )
        for appid, info in list(watchlist.items())[:8]:
            m_icon = "🛒 Автобай" if info.get("mode") == "autobuy" else "🔔 Увед."
            kb.add(InlineKeyboardButton(f"🗑 Удалить: {info.get('name', appid)} [{m_icon} -{info.get('target_disc', 1)}%]", callback_data=f"disc_rem_{appid}"))
        kb.add(InlineKeyboardButton("⬅️ Назад", callback_data="back_main"))
        lines = [f"• **{v.get('name')}** (`{k}`) — цель `-{v.get('target_disc', 1)}%` (сейчас `-{v.get('last_discount', 0)}%`)" for k, v in watchlist.items()]
        send_or_edit(chat_id, f"📉 **Радар скидок (каждые 15 мин)**\nПосл. проверка: `{bot_status['last_discount_check']}`\n\n" + ("\n".join(lines) or "_Список пуст_"), kb, msg_id)

    elif data.startswith("disc_rem_"):
        steam.data.get("watchlist", {}).pop(data.replace("disc_rem_", ""), None)
        save_session(steam.data)
        call.data = "menu_discounts"
        handle_callbacks(call)

    elif data == "disc_check_now":
        rep = check_discounts(force_report=True)
        notify_user(chat_id, rep or "ℹ️ Список отслеживания пуст.", get_main_keyboard())

    elif data == "menu_search":
        msg = bot.send_message(chat_id, "🔎 Напишите название игры для поиска:")
        bot.register_next_step_handler(msg, step_search_game)

    elif data == "menu_specials":
        specials = steam.get_top_daily_specials()
        kb = InlineKeyboardMarkup(row_width=1).add(InlineKeyboardButton("🏆 Показать Лидеров продаж (Топ чарта KZ)", callback_data="tool_top_sellers"))
        lines = ["🔥 **Топ скидок дня в Steam (Тенге ₸):**\n"]
        for sp in specials:
            lines.append(f"• **{sp['name']}** (`{sp['appid']}`) — **-{sp['discount']}%** → `{sp['final_kzt']}`")
            kb.add(InlineKeyboardButton(f"🧮 Калькулятор: {sp['name']} (-{sp['discount']}%)", callback_data=f"calc_from_appid_{sp['appid']}"))
        kb.add(InlineKeyboardButton("⬅️ Назад", callback_data="back_main"))
        send_or_edit(chat_id, "\n".join(lines), kb, msg_id)

    elif data == "tool_top_sellers":
        tops = steam.get_top_sellers_kz()
        kb = InlineKeyboardMarkup(row_width=1)
        lines = ["🏆 **Лидеры продаж Steam в Казахстане:**\n"]
        for i, t in enumerate(tops, 1):
            lines.append(f"{i}. **{t['name']}** — `{t['price']}`")
            kb.add(InlineKeyboardButton(f"🧮 Выбрать: {t['name']} ({t['price']})", callback_data=f"calc_from_appid_{t['appid']}"))
        kb.add(InlineKeyboardButton("⬅️ Назад", callback_data="menu_specials"))
        send_or_edit(chat_id, "\n".join(lines), kb, msg_id)

    elif data == "menu_tools":
        kb = InlineKeyboardMarkup(row_width=2).add(
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
        ).add(InlineKeyboardButton("⬅️ Назад в главное меню", callback_data="back_main"))
        send_or_edit(chat_id, "🛠 **Расширенные инструменты и Статистика игр (20+ функций):**", kb, msg_id)

    elif data == "tool_players":
        msg = bot.send_message(chat_id, "👥 Отправьте **AppID** игры (например `730` для CS2):", parse_mode="Markdown")
        bot.register_next_step_handler(msg, step_tool_players)

    elif data == "tool_reg_prices":
        msg = bot.send_message(chat_id, "🌍 Отправьте **AppID** игры для сравнения цены в 4 странах:", parse_mode="Markdown")
        bot.register_next_step_handler(msg, step_tool_regional_prices)

    elif data == "tool_free_dlc":
        msg = bot.send_message(chat_id, "🎁 Отправьте **AppID** игры для поиска бесплатных DLC:", parse_mode="Markdown")
        bot.register_next_step_handler(msg, step_tool_free_dlc)

    elif data == "tool_game_news":
        msg = bot.send_message(chat_id, "📰 Отправьте **AppID** игры для чтения новостей:", parse_mode="Markdown")
        bot.register_next_step_handler(msg, step_tool_game_news)

    elif data == "tool_achievements":
        msg = bot.send_message(chat_id, "🏆 Отправьте **AppID** игры для просмотра достижений:", parse_mode="Markdown")
        bot.register_next_step_handler(msg, step_tool_achievements)

    elif data == "tool_sys_reqs":
        msg = bot.send_message(chat_id, "💻 Отправьте **AppID** игры для просмотра требований ПК и рейтинга:", parse_mode="Markdown")
        bot.register_next_step_handler(msg, step_tool_sys_reqs)

    elif data == "tool_inspect_sub":
        msg = bot.send_message(chat_id, "📦 Отправьте **SubID** пакета Steam:", parse_mode="Markdown")
        bot.register_next_step_handler(msg, step_tool_inspect_sub)

    elif data == "tool_notes":
        notes = steam.data.get("notes", [])
        n_txt = "\n".join([f"{i+1}. `{n}`" for i, n in enumerate(notes)]) or "_Заметок пока нет_"
        kb = InlineKeyboardMarkup(row_width=2).add(
            InlineKeyboardButton("➕ Добавить заметку", callback_data="note_add"),
            InlineKeyboardButton("🗑 Очистить заметки", callback_data="note_clear"),
            InlineKeyboardButton("⬅️ Назад", callback_data="menu_tools"),
        )
        send_or_edit(chat_id, f"📝 **Ваши заметки:**\n\n{n_txt}", kb, msg_id)

    elif data == "note_add":
        msg = bot.send_message(chat_id, "✏️ Введите текст заметки:")
        bot.register_next_step_handler(msg, step_add_note)

    elif data == "note_clear":
        steam.data["notes"] = []
        save_session(steam.data)
        call.data = "tool_notes"
        handle_callbacks(call)

    elif data == "tool_wishlist":
        w_items = steam.get_wishlist_items()
        if not w_items:
            notify_user(chat_id, "ℹ️ Ваш Список желаемого пуст или скрыт.", get_main_keyboard())
            return
        kb = InlineKeyboardMarkup(row_width=1).add(InlineKeyboardButton("📥 Импортировать ВСЕ в Радар скидок", callback_data="wishlist_import_all"))
        lines = [f"⭐ **Ваш Список желаемого ({len(w_items)} игр):**\n"]
        for it in w_items:
            lines.append(f"• **{it['name']}** (`{it['appid']}`) — `{it['price']}`")
            kb.add(InlineKeyboardButton(f"🧮 Калькулятор: {it['name']}", callback_data=f"calc_from_appid_{it['appid']}"))
        kb.add(InlineKeyboardButton("⬅️ Назад", callback_data="menu_tools"))
        send_or_edit(chat_id, "\n".join(lines), kb, msg_id)

    elif data == "wishlist_import_all":
        w_items = steam.get_wishlist_items()
        steam.data.setdefault("watchlist", {})
        for it in w_items:
            steam.data["watchlist"].setdefault(it["appid"], {"name": it["name"], "target_disc": 20, "mode": "notify", "last_discount": it["discount"]})
        save_session(steam.data)
        call.data = "menu_discounts"
        handle_callbacks(call)

    elif data == "tool_lib_stats":
        items = library_cache.get(chat_id) or steam.get_all_licenses()
        library_cache[chat_id] = items
        paid_cnt = sum(1 for x in items if not x["removable"])
        free_cnt = sum(1 for x in items if x["removable"])
        oldest = items[-1]["name"] if items else "—"
        newest = items[0]["name"] if items else "—"
        text = f"💎 **Аналитика библиотеки**\n\n📊 Всего игр: `{len(items)}`\n🔒 Купленных: `{paid_cnt}`\n🆓 Бесплатных: `{free_cnt}`\n🆕 Новая: `{newest}`\n🏛 Первая: `{oldest}`"
        send_or_edit(chat_id, text, InlineKeyboardMarkup().add(InlineKeyboardButton("⬅️ Назад", callback_data="menu_tools")), msg_id)

    elif data == "tool_clear_cart":
        steam.clear_steam_cart()
        notify_user(chat_id, "🛒 **Корзина Steam очищена!**", get_main_keyboard())

    elif data == "tool_redeem_key":
        msg = bot.send_message(chat_id, "🔑 Отправьте ключ Steam (`AAAAA-BBBBB-CCCCC`):", parse_mode="Markdown")
        bot.register_next_step_handler(msg, step_redeem_key)

    elif data == "tool_history":
        hist = steam.get_purchase_history()
        send_or_edit(chat_id, "🧾 **Последние операции:**\n\n" + ("\n".join(hist) or "Пусто"), InlineKeyboardMarkup().add(InlineKeyboardButton("⬅️ Назад", callback_data="menu_tools")), msg_id)

    elif data == "tool_random_game":
        items = library_cache.get(chat_id) or steam.get_all_licenses()
        if items:
            pick = random.choice(items)
            notify_user(chat_id, f"🎲 **Случайный выбор:**\n🎮 **{pick['name']}** (`{pick['date']}`)", get_main_keyboard())

    elif data == "tool_server_status":
        cs2_p = steam.get_live_player_count(730) or "Н/Д"
        dota_p = steam.get_live_player_count(570) or "Н/Д"
        send_or_edit(chat_id, f"🕵️ **Статус Steam:** 🟢 Онлайн\n🔫 CS2: `{cs2_p}` игроков\n⚔️ Dota 2: `{dota_p}` игроков", InlineKeyboardMarkup().add(InlineKeyboardButton("⬅️ Назад", callback_data="menu_tools")), msg_id)

    elif data == "tool_batch_f2p":
        for aid in ("730", "570", "440", "1172470", "291550"):
            sid = steam.get_subid_from_appid(aid)
            if sid:
                steam.add_free_license(sid)
        notify_user(chat_id, "✅ Пакет из 5 F2P-хитов добавлен!", get_main_keyboard())

    elif data == "tool_toggle_silent":
        steam.data["silent_mode"] = not steam.data.get("silent_mode", False)
        save_session(steam.data)
        send_or_edit(chat_id, format_dashboard_text(), get_main_keyboard(), msg_id)

    elif data == "tool_clear_cache":
        library_cache.clear()
        calc_state.clear()
        bot.answer_callback_query(call.id, "🧹 Кэш очищен!", show_alert=True)

    elif data == "menu_profile":
        ok, balance = steam.check_health()
        kb = InlineKeyboardMarkup(row_width=1).add(
            InlineKeyboardButton("📝 Изменить «О себе»", callback_data="change_bio"),
            InlineKeyboardButton("🏷 Изменить Никнейм", callback_data="change_nickname"),
            InlineKeyboardButton("👤 Изменить Настоящее имя", callback_data="change_realname"),
            InlineKeyboardButton("🔄 Сменить аккаунт Steam", callback_data="login_steam"),
            InlineKeyboardButton("⬅️ Назад", callback_data="back_main"),
        )
        send_or_edit(chat_id, f"👤 **Профиль Steam**\nАккаунт: `{steam.data.get('account_name')}`\n💰 Баланс: `{balance}`", kb, msg_id)

    elif data == "change_bio":
        msg = bot.send_message(chat_id, "✏️ Введите новый текст «О себе»:")
        bot.register_next_step_handler(msg, step_change_bio)

    elif data == "change_nickname":
        msg = bot.send_message(chat_id, "🏷 Введите новый Никнейм:")
        bot.register_next_step_handler(msg, step_change_nickname)

    elif data == "change_realname":
        msg = bot.send_message(chat_id, "👤 Введите новое Настоящее имя:")
        bot.register_next_step_handler(msg, step_change_realname)

    elif data == "menu_giveaways":
        kb = InlineKeyboardMarkup(row_width=1).add(
            InlineKeyboardButton("🔍 Проверить 100% раздачи сейчас", callback_data="check_now"),
            InlineKeyboardButton("⬅️ Назад", callback_data="back_main"),
        )
        send_or_edit(chat_id, f"🎁 **Автосбор 100% раздач**\nПосл. проверка: `{bot_status['last_check']}`\nЗабрано: `{bot_status['claimed_count']}`", kb, msg_id)

    elif data == "check_now":
        cnt = check_giveaways()
        notify_user(chat_id, f"ℹ️ Новых раздач добавлено: {cnt}", get_main_keyboard())

    elif data == "menu_ping":
        history_lines = "\n".join(bot_status["ping_history"][-8:]) or "_Пока нет записей_"
        kb = InlineKeyboardMarkup(row_width=2).add(
            InlineKeyboardButton("🔄 Обновить", callback_data="menu_ping"),
            InlineKeyboardButton("⚡ Пингануть", callback_data="force_ping"),
            InlineKeyboardButton("⏱ Пинг: 4 мин", callback_data="ping_int_4"),
            InlineKeyboardButton("⏱ Пинг: 14 мин", callback_data="ping_int_14"),
            InlineKeyboardButton("🌐 Сменить URL", callback_data="set_render_url"),
            InlineKeyboardButton("⬅️ Назад", callback_data="back_main"),
        )
        send_or_edit(chat_id, f"📡 **Тихий самопинг**\nURL: `{steam.data.get('render_url')}/ping`\nПингов: `{bot_status['ping_count']}`\n\n{history_lines}", kb, msg_id)

    elif data in ("ping_int_4", "ping_int_14"):
        bot_status["ping_interval_min"] = 4 if data == "ping_int_4" else 14
        call.data = "menu_ping"
        handle_callbacks(call)

    elif data == "force_ping":
        do_single_ping()
        call.data = "menu_ping"
        handle_callbacks(call)

    elif data == "menu_system":
        kb = InlineKeyboardMarkup(row_width=1).add(
            InlineKeyboardButton("🔄 Обновить код бота", callback_data="update_bot"),
            InlineKeyboardButton("⏪ Откатить к резервной копии", callback_data="sys_rollback"),
            InlineKeyboardButton("📥 Скачать текущий файл (.py)", callback_data="sys_download_code"),
            InlineKeyboardButton("💾 Получить SESSION_BASE64", callback_data="get_b64"),
            InlineKeyboardButton("📥 Вставить SESSION_BASE64", callback_data="import_b64"),
            InlineKeyboardButton("⬅️ Назад", callback_data="back_main"),
        )
        send_or_edit(chat_id, f"⚙️ **Система**\nСборка: `{steam.data.get('last_update_time', 'Базовая')}`\nСайт: `{steam.data.get('render_url')}`", kb, msg_id)

    elif data == "sys_download_code":
        with open(os.path.abspath(__file__), "rb") as f:
            bot.send_document(chat_id, f, caption="📄 Текущий рабочий код бота")

    elif data == "login_steam":
        msg = bot.send_message(chat_id, "🔑 Шаг 1/3. Отправьте ваш логин Steam:")
        bot.register_next_step_handler(msg, step_get_username)

    elif data == "import_b64":
        msg = bot.send_message(chat_id, "📥 Отправьте строку SESSION_BASE64:")
        bot.register_next_step_handler(msg, step_import_b64)

    elif data == "get_b64":
        notify_user(chat_id, f"💾 `{export_session_b64(steam.data)}`", get_main_keyboard())

    elif data == "test_f2p":
        kb = InlineKeyboardMarkup(row_width=2).add(
            InlineKeyboardButton("The Sims 4", callback_data="f2p_1222670"),
            InlineKeyboardButton("PUBG", callback_data="f2p_578080"),
            InlineKeyboardButton("Brawlhalla", callback_data="f2p_291550"),
            InlineKeyboardButton("Dota 2", callback_data="f2p_570"),
            InlineKeyboardButton("🔢 Ввести свой AppID", callback_data="f2p_custom"),
            InlineKeyboardButton("⬅️ Назад", callback_data="menu_f2p_farm"),
        )
        send_or_edit(chat_id, "🎮 Выберите бесплатную игру или введите свой AppID:", kb, msg_id)

    elif data.startswith("f2p_"):
        target = data.replace("f2p_", "")
        if target == "custom":
            msg = bot.send_message(chat_id, "🔢 Отправьте AppID бесплатной игры:")
            bot.register_next_step_handler(msg, step_custom_f2p)
        else:
            add_f2p_game(chat_id, target)

    elif data == "set_render_url":
        msg = bot.send_message(chat_id, "🌐 Отправьте ссылку на ваш Render-сервис:")
        bot.register_next_step_handler(msg, step_set_render_url)


# ----------------- ФУНКЦИИ ШАГОВ -----------------
def step_f2p_custom_count(message):
    chat_id = message.chat.id
    raw_n = re.sub(r"\D", "", message.text.strip())
    cnt = max(1, min(35, int(raw_n))) if raw_n else 5
    notify_user(chat_id, f"⏳ Ищу и добавляю **{cnt} рандомных Free-to-Play игр**...")

    def _run():
        added = steam.add_random_f2p_batch(cnt)
        library_cache.pop(chat_id, None)
        msg = f"🎉 **Добавлено {len(added)} игр:**\n\n" + "\n".join(added) if added else "ℹ️ Попробуйте еще раз!"
        notify_user(chat_id, msg, get_main_keyboard())

    threading.Thread(target=_run, daemon=True).start()


def step_tool_players(message):
    app_id = re.sub(r"\D", "", message.text.strip())
    cnt = steam.get_live_player_count(app_id) if app_id else None
    info = steam.get_game_price_info(app_id) if app_id else None
    name = info["name"] if info else f"App {app_id}"
    notify_user(message.chat.id, f"👥 **Онлайн в «{name}» (`{app_id}`):** `{cnt if cnt is not None else 'Н/Д'} чел.`", get_main_keyboard())


def step_tool_regional_prices(message):
    m = re.search(r"/app/(\d+)", message.text)
    app_id = m.group(1) if m else re.sub(r"\D", "", message.text.strip())
    name, lines = steam.compare_regional_prices(app_id)
    notify_user(message.chat.id, f"🌍 **Цены «{name}» (`{app_id}`):**\n\n" + "\n".join(lines), get_main_keyboard())


def step_tool_free_dlc(message):
    app_id = re.sub(r"\D", "", message.text.strip())
    added = steam.add_free_dlcs_for_game(app_id) if app_id else []
    msg = f"🎁 **Добавлены DLC ({len(added)} шт.):**\n\n" + "\n".join(added) if added else "ℹ️ Бесплатных DLC не найдено."
    notify_user(message.chat.id, msg, get_main_keyboard())


def step_tool_game_news(message):
    app_id = re.sub(r"\D", "", message.text.strip())
    news = steam.get_game_news(app_id) if app_id else []
    notify_user(message.chat.id, f"📰 **Новости (`{app_id}`):**\n\n" + ("\n\n".join(news) or "Нет новостей."), get_main_keyboard())


def step_tool_achievements(message):
    app_id = re.sub(r"\D", "", message.text.strip())
    ach = steam.get_global_achievements(app_id) if app_id else None
    if not ach:
        notify_user(message.chat.id, "ℹ️ Достижения не найдены.", get_main_keyboard())
        return
    easy = "\n".join([f"• `{a['name']}` — **{float(a['percent']):.1f}%**" for a in ach["easiest"]])
    rare = "\n".join([f"• `{a['name']}` — **{float(a['percent']):.2f}%**" for a in ach["rarest"]])
    notify_user(message.chat.id, f"🏆 **Достижения (`{app_id}`):**\n\n🟢 Частые:\n{easy}\n\n💎 Редкие:\n{rare}", get_main_keyboard())


def step_tool_sys_reqs(message):
    app_id = re.sub(r"\D", "", message.text.strip())
    info = steam.get_game_price_info(app_id) if app_id else None
    if not info:
        notify_user(message.chat.id, "❌ Игра не найдена.", get_main_keyboard())
        return
    notify_user(
        message.chat.id,
        f"💻 **«{info['name']}» (`{app_id}`)**\n⭐ Metacritic: `{info['metacritic']}` | Отзывов: `{info['recommendations']}`\n💰 Цена: `{info['final_formatted']}`\n\n🔹 Мин: {info['pc_min']}\n\n🔸 Рек: {info['pc_rec']}",
        get_main_keyboard(),
    )


def step_tool_inspect_sub(message):
    sub_id = re.sub(r"\D", "", message.text.strip())
    info = steam.inspect_package_subid(sub_id) if sub_id else None
    if not info:
        notify_user(message.chat.id, "❌ Пакет не найден.", get_main_keyboard())
        return
    apps_str = "\n".join([f"• {a}" for a in info["apps"][:15]]) or "Не указаны"
    notify_user(message.chat.id, f"📦 **Пакет `{info['subid']}` — {info['name']}**\n💰 Цена: `{info['price_kzt']}`\n\n{apps_str}", get_main_keyboard())


def step_add_note(message):
    steam.data.setdefault("notes", []).append(message.text.strip())
    save_session(steam.data)
    notify_user(message.chat.id, "✅ Заметка сохранена!", get_main_keyboard())


def step_calc_get_appid(message):
    m = re.search(r"/app/(\d+)", message.text)
    app_id = m.group(1) if m else re.sub(r"\D", "", message.text.strip())
    if app_id:
        show_game_and_ask_discount(message.chat.id, app_id)


def show_game_and_ask_discount(chat_id, app_id):
    info = steam.get_game_price_info(app_id)
    if not info:
        notify_user(chat_id, f"❌ Игра `{app_id}` не найдена.", get_main_keyboard())
        return
    if info["is_free"]:
        kb = InlineKeyboardMarkup().add(InlineKeyboardButton("➕ Добавить бесплатно", callback_data=f"f2p_{app_id}"), InlineKeyboardButton("⬅️ Назад", callback_data="back_main"))
        notify_user(chat_id, f"🎮 **{info['name']}** уже бесплатная (0 ₸)!", kb)
        return
    calc_state[chat_id] = info
    kb = InlineKeyboardMarkup(row_width=2).add(
        InlineKeyboardButton("🛒 Автобай -30%", callback_data=f"watch_set_autobuy_{app_id}_30"),
        InlineKeyboardButton("🛒 Автобай -50%", callback_data=f"watch_set_autobuy_{app_id}_50"),
        InlineKeyboardButton("🛒 Автобай -75%", callback_data=f"watch_set_autobuy_{app_id}_75"),
        InlineKeyboardButton("🔔 Любая скидка", callback_data=f"watch_set_any_{app_id}_1"),
    ).add(InlineKeyboardButton("⬅️ Отмена", callback_data="back_main"))
    msg = bot.send_message(chat_id, f"🎮 **{info['name']}** (`{app_id}`)\n💰 Полная цена: **`{info['initial_kzt']:g} ₸`** (сейчас: `{info['final_formatted']}`)\n\n🔢 Введите желаемый процент скидки (1–99):", parse_mode="Markdown", reply_markup=kb)
    bot.register_next_step_handler(msg, step_calc_discount_percent)


def step_calc_discount_percent(message):
    chat_id = message.chat.id
    info = calc_state.get(chat_id)
    if not info:
        return
    raw_num = re.sub(r"\D", "", message.text.strip()) or "50"
    target_disc = max(1, min(99, int(raw_num)))
    base_kzt = info["initial_kzt"]
    discounted_kzt = round(base_kzt * (100 - target_disc) / 100.0, 2)
    saved_kzt = round(base_kzt - discounted_kzt, 2)
    app_id = info["appid"]
    kb = InlineKeyboardMarkup(row_width=1).add(
        InlineKeyboardButton(f"🛒 Автопокупка (через 5–15 мин) при -{target_disc}%", callback_data=f"watch_set_autobuy_{app_id}_{target_disc}"),
        InlineKeyboardButton(f"🔔 Только уведомить при -{target_disc}%", callback_data=f"watch_set_notify_{app_id}_{target_disc}"),
        InlineKeyboardButton("⬅️ Назад", callback_data="back_main"),
    )
    notify_user(chat_id, f"🧮 **Расчет скидки -{target_disc}%:**\n🎮 **{info['name']}**\n💵 Полная цена: `{base_kzt:g} ₸`\n🏷 **Со скидкой:** **`{discounted_kzt:g} ₸`** (экономия `{saved_kzt:g} ₸`)", kb)


def trigger_delayed_autobuy(chat_id, app_id, game_name, cur_disc):
    if app_id in bot_status["autobuy_tasks"]:
        return
    bot_status["autobuy_tasks"].add(app_id)
    delay_sec = random.randint(300, 900)
    notify_user(chat_id, f"🎯 **Скидка -{cur_disc}% на «{game_name}»!** Автопокупка через **{round(delay_sec/60, 1)} мин.**")
    time.sleep(delay_sec)
    res = steam.auto_purchase_game(app_id)
    bot_status["autobuy_tasks"].discard(app_id)
    notify_user(chat_id, f"🛒 **Результат автопокупки «{game_name}»:**\n{res.get('msg')}")


def step_search_game(message):
    results = steam.search_games_by_name(message.text.strip())
    kb = InlineKeyboardMarkup(row_width=1)
    for r in results:
        kb.add(InlineKeyboardButton(f"🧮 {r['name']} ({r['price_str']})", callback_data=f"calc_from_appid_{r['appid']}"))
    kb.add(InlineKeyboardButton("⬅️ Назад", callback_data="back_main"))
    notify_user(message.chat.id, "🔎 **Результаты поиска:**", kb)


def step_redeem_key(message):
    res = steam.redeem_cd_key(message.text.strip())
    notify_user(message.chat.id, f"✅ Ключ активирован: {res.get('games')}" if res.get("ok") else "❌ Ошибка активации ключа.", get_main_keyboard())


def step_change_bio(message):
    steam.update_profile_field(summary=message.text.strip())
    notify_user(message.chat.id, "✅ Графа «О себе» обновлена!", get_main_keyboard())


def step_change_nickname(message):
    steam.update_profile_field(persona_name=message.text.strip())
    notify_user(message.chat.id, "✅ Никнейм изменен!", get_main_keyboard())


def step_change_realname(message):
    steam.update_profile_field(real_name=message.text.strip())
    notify_user(message.chat.id, "✅ Настоящее имя изменено!", get_main_keyboard())


def step_get_username(message):
    pending_auth[message.chat.id] = {"username": message.text.strip()}
    msg = bot.send_message(message.chat.id, "🔒 Шаг 2/3. Отправьте пароль:")
    bot.register_next_step_handler(msg, step_get_password)


def step_get_password(message):
    chat_id = message.chat.id
    pwd = message.text.strip()
    try:
        bot.delete_message(chat_id, message.message_id)
    except Exception:
        pass
    if chat_id in pending_auth:
        pending_auth[chat_id]["password"] = pwd
        msg = bot.send_message(chat_id, "🛡 Шаг 3/3. Отправьте 5-значный код Steam Guard:")
        bot.register_next_step_handler(msg, step_get_guard_code)


def step_get_guard_code(message):
    chat_id = message.chat.id
    auth_data = pending_auth.pop(chat_id, None)
    if auth_data and "password" in auth_data:
        res = steam.atomic_login(auth_data["username"], auth_data["password"], message.text.strip())
        notify_user(chat_id, "✅ Успешный вход!" if res.get("ok") else f"❌ Ошибка: {res.get('error')}", get_main_keyboard())


def step_import_b64(message):
    try:
        decoded = json.loads(base64.b64decode(message.text.strip()).decode("utf-8"))
        decoded["admin_id"] = message.from_user.id
        steam.data = decoded
        save_session(steam.data)
        notify_user(message.chat.id, "✅ Сессия успешно импортирована!", get_main_keyboard())
    except Exception as e:
        notify_user(message.chat.id, f"❌ Ошибка: {e}", get_main_keyboard())


def step_custom_f2p(message):
    app_id = re.sub(r"\D", "", message.text.strip())
    if app_id:
        add_f2p_game(message.chat.id, app_id)


def add_f2p_game(chat_id, app_id):
    sub_id = steam.get_subid_from_appid(app_id)
    if sub_id and steam.add_free_license(sub_id):
        bot_status["claimed_count"] += 1
        bot_status["f2p_added_count"] += 1
        library_cache.pop(chat_id, None)
        notify_user(chat_id, f"✅ **Игра `{app_id}` добавлена в библиотеку!**", get_main_keyboard())
    else:
        notify_user(chat_id, "❌ Не удалось активировать лицензию.", get_main_keyboard())


def step_set_render_url(message):
    url = message.text.strip().rstrip("/")
    if not url.startswith("http"):
        url = "https://" + url
    steam.data["render_url"] = url
    save_session(steam.data)
    tok_config["render_url"] = url
    notify_user(message.chat.id, f"✅ URL самопинга сохранен: `{url}/ping`", get_main_keyboard())


# ----------------- ФОНОВЫЕ ЦИКЛЫ (15 МИН) -----------------
def check_discounts(force_report=False):
    watchlist = steam.data.get("watchlist", {})
    if not watchlist:
        return ""
    bot_status["last_discount_check"] = time.strftime("%H:%M:%S")
    admin_id = steam.data.get("admin_id")
    report_lines = ["📊 **Цены по вашему радару:**\n"]
    for app_id, saved in list(watchlist.items()):
        info = steam.get_game_price_info(app_id)
        if not info:
            continue
        cur_disc = info["discount"]
        target_disc = saved.get("target_disc", 1)
        mode = saved.get("mode", "notify")
        report_lines.append(f"• **{info['name']}**: `{info['final_formatted']}` (-{cur_disc}%, цель -{target_disc}%)")
        if cur_disc >= target_disc and cur_disc != saved.get("last_discount", 0) and admin_id:
            if mode == "autobuy":
                threading.Thread(target=trigger_delayed_autobuy, args=(admin_id, app_id, info["name"], cur_disc), daemon=True).start()
            else:
                notify_user(admin_id, f"🔥 **Скидка -{cur_disc}% на {info['name']}!**\nЦена: `{info['final_formatted']}`")
        steam.data["watchlist"][app_id]["last_discount"] = cur_disc
    save_session(steam.data)
    return "\n".join(report_lines) if force_report else ""


def discounts_loop():
    while True:
        time.sleep(CHECK_DISCOUNTS_INTERVAL)
        try:
            if steam.is_configured():
                check_discounts()
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
                    notify_user(admin_id, f"🎮 **Авто-фарм F2P (15 мин): добавлено {len(added)} игр!**\n\n" + "\n".join(added))
        except Exception:
            pass


def check_giveaways():
    if not steam.is_configured():
        return 0
    claimed = load_json_set(CLAIMED_FILE)
    bot_status["last_check"] = time.strftime("%Y-%m-%d %H:%M:%S")
    added = 0
    try:
        giveaways = requests.get("https://www.gamerpower.com/api/giveaways?platform=steam&type=game", timeout=15).json()
    except Exception:
        return 0
    for g in giveaways:
        gid = str(g.get("id"))
        open_url = g.get("open_giveaway_url", "")
        if gid not in claimed and "store.steampowered.com/app/" in open_url:
            try:
                app_id = open_url.split("/app/")[1].split("/")[0]
                sub_id = steam.get_subid_from_appid(app_id)
                if sub_id and steam.add_free_license(sub_id):
                    claimed.add(gid)
                    save_json_set(CLAIMED_FILE, claimed)
                    added += 1
                    bot_status["claimed_count"] += 1
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


# ----------------- ВЕБ-САЙТ УПРАВЛЕНИЯ (БЕЗ ТРОЙНЫХ КАВЫЧЕК!) -----------------
app = Flask(__name__)
app.secret_key = secrets.token_hex(16)

HTML_LINES = [
    "<!DOCTYPE html><html lang='ru'><head><meta charset='UTF-8'>",
    "<meta name='viewport' content='width=device-width, initial-scale=1.0'>",
    "<title>Steam Ultimate Control Center</title>",
    "<style>",
    "body{font-family:system-ui,sans-serif;background:#0b1120;color:#f8fafc;margin:0;padding:15px;}",
    ".container{max-width:1080px;margin:0 auto;}",
    ".header{display:flex;justify-content:space-between;align-items:center;border-bottom:1px solid #334155;padding-bottom:12px;margin-bottom:16px;}",
    "h1,h3{color:#38bdf8;margin-top:0;}",
    ".grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(300px,1fr));gap:15px;margin-bottom:15px;}",
    ".card{background:#1e293b;border:1px solid #334155;border-radius:12px;padding:16px;}",
    "input,select,textarea{width:100%;box-sizing:border-box;padding:9px;margin:5px 0;border-radius:8px;border:1px solid #475569;background:#0f172a;color:#fff;}",
    "button{background:#0284c7;color:#fff;border:none;padding:9px 14px;border-radius:8px;font-weight:bold;cursor:pointer;margin-top:5px;}",
    ".btn-green{background:#16a34a;}.btn-red{background:#e11d48;}",
    ".ok{color:#4ade80;font-weight:bold;}.err{color:#f87171;font-weight:bold;}",
    ".alert{background:#0369a1;padding:12px;border-radius:8px;margin-bottom:15px;font-weight:bold;}",
    ".list-box{max-height:240px;overflow-y:auto;background:#0f172a;padding:10px;border-radius:8px;font-size:13px;}",
    ".list-item{display:flex;justify-content:space-between;align-items:center;padding:5px 0;border-bottom:1px solid #1e293b;}",
    "</style></head><body><div class='container'>",
    "<div class='header'><h1>🚀 Steam Ultimate Web Panel</h1>",
    "{% if logged_in %}<form method='POST' action='/logout'><button class='btn-red'>Выйти</button></form>{% endif %}</div>",
    "{% if web_msg %}<div class='alert'>💡 {{ web_msg }}</div>{% endif %}",
    "{% if not logged_in %}",
    "<div class='card' style='max-width:400px;margin:30px auto;'><h3>🔐 Вход в панель (пароль в tok.json, по умолч. admin123)</h3>",
    "<form method='POST' action='/login'><input type='password' name='password' placeholder='Пароль' required>",
    "<button type='submit' style='width:100%;'>Войти</button></form></div>",
    "{% else %}",
    "<div class='grid'>",
    "<div class='card'><h3>👤 Аккаунт и Баланс</h3>",
    "<p>Статус: <span class='{{ \"ok\" if steam_ok else \"err\" }}'>{{ account_name if steam_ok else \"Не авторизован\" }}</span></p>",
    "<p>💰 Баланс: <b>{{ balance }}</b> | 🎁 Раздач: <b>{{ status.claimed_count }}</b> | 🎮 F2P: <b>{{ status.f2p_added_count }}</b></p>",
    "<form method='POST' action='/web_action'><input type='hidden' name='act' value='update_profile'>",
    "<input type='text' name='bio' placeholder='Новый текст «О себе»'><input type='text' name='nick' placeholder='Новый Никнейм'>",
    "<button type='submit'>📝 Обновить профиль</button></form></div>",
    "<div class='card'><h3>🎮 Фарм F2P (Рандом и Авто 15м)</h3>",
    "<p>Авто-сбор всех F2P (15 мин): <b>{{ 'ВКЛ' if status.f2p_auto_all_enabled else 'ВЫКЛ' }}</b></p>",
    "<form method='POST' action='/web_action' style='display:flex;gap:6px;flex-wrap:wrap;'>",
    "<button name='act' value='f2p_rand_5' class='btn-green'>🎲 +5 F2P</button>",
    "<button name='act' value='f2p_rand_15' class='btn-green'>🎲 +15 F2P</button>",
    "<button name='act' value='f2p_toggle'>♾️ Вкл/Выкл Авто (15м)</button>",
    "<button name='act' value='check_giveaways'>🎁 Проверить раздачи</button></form>",
    "<form method='POST' action='/web_action' style='margin-top:8px;'><input type='hidden' name='act' value='redeem_key'>",
    "<input type='text' name='cdkey' placeholder='Ключ Steam (XXXXX-XXXXX-XXXXX)'><button type='submit'>🔑 Активировать ключ</button></form></div>",
    "<div class='card'><h3>📡 Тихий Самопинг</h3>",
    "<p>Пингов: <b>{{ status.ping_count }}</b> ({{ status.last_ping_time }} — {{ status.last_ping_code }})</p>",
    "<div class='list-box' style='height:90px;'>{% for l in status.ping_history|reverse %}{{ l }}<br>{% endfor %}</div>",
    "<form method='POST' action='/web_action' style='display:flex;gap:6px;'>",
    "<button name='act' value='ping_now'>⚡ Пинг</button><button name='act' value='ping_4'>⏱ 4м</button><button name='act' value='ping_14'>⏱ 14м</button></form></div>",
    "</div>",
    "<div class='grid'>",
    "<div class='card'><h3>🧮 Калькулятор Скидок (₸) и Автопокупка</h3>",
    "<form method='POST' action='/web_action'><input type='hidden' name='act' value='add_watch'>",
    "<input type='text' name='appid' placeholder='AppID игры (например 1091500)' required>",
    "<input type='number' name='discount' value='50' min='1' max='99'>",
    "<select name='mode'><option value='autobuy'>🛒 Автопокупка (5-15 мин)</option><option value='notify'>🔔 Только уведомление</option></select>",
    "<button type='submit'>➕ Рассчитать в ₸ и сохранить</button></form>",
    "<div class='list-box' style='margin-top:8px;'>{% for aid, w in watchlist.items() %}",
    "<div class='list-item'><span><b>{{ w.name }}</b> ({{ aid }}) -{{ w.target_disc }}% [{{ w.mode }}]</span>",
    "<form method='POST' action='/web_action' style='margin:0;'><input type='hidden' name='act' value='del_watch'><input type='hidden' name='appid' value='{{ aid }}'><button class='btn-red' style='padding:3px 7px;margin:0;'>🗑</button></form></div>",
    "{% else %}Список пуст{% endfor %}</div></div>",
    "<div class='card'><h3>📊 Статистика Игры по AppID</h3>",
    "<form method='POST' action='/web_action'><input type='hidden' name='act' value='inspect_game'>",
    "<input type='text' name='appid' placeholder='AppID игры (730, 570, 1091500)' required>",
    "<button type='submit'>🔎 Статистика и цены</button> <button type='submit' name='subact' value='free_dlc' class='btn-green'>🎁 Собрать бесплатные DLC</button></form>",
    "{% if game_stat %}<div class='list-box' style='margin-top:8px;white-space:pre-line;'>{{ game_stat }}</div>{% endif %}</div>",
    "</div>",
    "<div class='grid'>",
    "<div class='card'><h3>📚 Библиотека ({{ lib_items|length }} игр: 🔒 {{ paid_cnt }} | ❌ {{ free_cnt }})</h3>",
    "<form method='POST' action='/web_action'><button name='act' value='reload_lib'>🔄 Загрузить библиотеку со Steam</button></form>",
    "<div class='list-box' style='margin-top:8px;'>{% for item in lib_items[:60] %}",
    "<div class='list-item'><span>{{ '❌' if item.removable else '🔒' }} <b>{{ item.name }}</b> ({{ item.date }})</span>",
    "{% if item.removable %}<form method='POST' action='/web_action' style='margin:0;'><input type='hidden' name='act' value='del_pkg'><input type='hidden' name='pkgid' value='{{ item.packageid }}'><button class='btn-red' style='padding:3px 7px;margin:0;'>Удалить</button></form>{% else %}<span>🔒</span>{% endif %}</div>",
    "{% endfor %}</div></div>",
    "<div class='card'><h3>🔄 Автообновление Кода Бота</h3>",
    "<form method='POST' action='/web_update' enctype='multipart/form-data'>",
    "<input type='file' name='pyfile' accept='.py,.txt'>",
    "<textarea name='code' rows='4' placeholder='ИЛИ вставьте новый Python-код сюда...'></textarea>",
    "<button type='submit' class='btn-green'>🚀 Проверить и обновить бота</button></form></div>",
    "</div>{% endif %}</div></body></html>",
]
HTML_DASHBOARD = "\n".join(HTML_LINES)


@app.route("/")
def web_index():
    if not session.get("logged_in"):
        return render_template_string(HTML_DASHBOARD, logged_in=False, web_msg=bot_status["web_msg"])
    ok, bal = steam.check_health()
    items = library_cache.get("web") or []
    paid_c = sum(1 for x in items if not x["removable"])
    free_c = sum(1 for x in items if x["removable"])
    msg = bot_status["web_msg"]
    bot_status["web_msg"] = ""
    return render_template_string(
        HTML_DASHBOARD,
        logged_in=True,
        steam_ok=ok,
        account_name=steam.data.get("account_name", "User"),
        balance=bal,
        status=bot_status,
        watchlist=steam.data.get("watchlist", {}),
        lib_items=items,
        paid_cnt=paid_c,
        free_cnt=free_c,
        game_stat=web_game_stat_cache,
        web_msg=msg,
    )


@app.route("/login", methods=["POST"])
def web_login():
    if request.form.get("password") == tok_config["web_password"]:
        session["logged_in"] = True
        bot_status["web_msg"] = "Вход выполнен!"
    else:
        bot_status["web_msg"] = "Неверный пароль!"
    return redirect(url_for("web_index"))


@app.route("/logout", methods=["POST"])
def web_logout():
    session.clear()
    return redirect(url_for("web_index"))


@app.route("/web_action", methods=["POST"])
def web_action():
    global web_game_stat_cache
    if not session.get("logged_in"):
        return redirect(url_for("web_index"))
    act = request.form.get("act", "")
    if act == "ping_now":
        do_single_ping()
        bot_status["web_msg"] = f"Пинг: {bot_status['last_ping_code']}"
    elif act in ("ping_4", "ping_14"):
        bot_status["ping_interval_min"] = 4 if act == "ping_4" else 14
        bot_status["web_msg"] = f"Интервал пинга: {bot_status['ping_interval_min']} мин."
    elif act == "update_profile":
        bio = request.form.get("bio", "").strip()
        nick = request.form.get("nick", "").strip()
        steam.update_profile_field(summary=bio if bio else None, persona_name=nick if nick else None)
        bot_status["web_msg"] = "Профиль обновлен!"
    elif act in ("f2p_rand_5", "f2p_rand_15"):
        cnt = 5 if act == "f2p_rand_5" else 15
        threading.Thread(target=steam.add_random_f2p_batch, args=(cnt,), daemon=True).start()
        bot_status["web_msg"] = f"Запущено добавление {cnt} рандомных F2P игр!"
    elif act == "f2p_toggle":
        bot_status["f2p_auto_all_enabled"] = not bot_status["f2p_auto_all_enabled"]
        steam.data["f2p_auto_all"] = bot_status["f2p_auto_all_enabled"]
        save_session(steam.data)
        bot_status["web_msg"] = "Авто-фарм F2P (15 мин) переключен!"
    elif act == "check_giveaways":
        cnt = check_giveaways()
        bot_status["web_msg"] = f"Раздач добавлено: {cnt}"
    elif act == "redeem_key":
        k = request.form.get("cdkey", "").strip()
        if k:
            res = steam.redeem_cd_key(k)
            bot_status["web_msg"] = f"Ключ активирован: {res.get('games')}" if res.get("ok") else "Ошибка ключа."
    elif act == "add_watch":
        aid = re.sub(r"\D", "", request.form.get("appid", ""))
        disc = max(1, min(99, int(request.form.get("discount", 50) or 50)))
        mode = request.form.get("mode", "notify")
        info = steam.get_game_price_info(aid) if aid else None
        if info:
            calc_p = round(info["initial_kzt"] * (100 - disc) / 100.0, 2)
            steam.data.setdefault("watchlist", {})[aid] = {"name": info["name"], "target_disc": disc, "mode": mode, "last_discount": info["discount"]}
            save_session(steam.data)
            bot_status["web_msg"] = f"{info['name']}: со скидкой -{disc}% = {calc_p:g} ₸. Сохранено!"
    elif act == "del_watch":
        steam.data.get("watchlist", {}).pop(request.form.get("appid", ""), None)
        save_session(steam.data)
    elif act == "inspect_game":
        aid = re.sub(r"\D", "", request.form.get("appid", ""))
        if request.form.get("subact") == "free_dlc" and aid:
            dlcs = steam.add_free_dlcs_for_game(aid)
            bot_status["web_msg"] = f"Добавлено бесплатных DLC: {len(dlcs)} шт."
        elif aid:
            info = steam.get_game_price_info(aid)
            players = steam.get_live_player_count(aid)
            _, reg_lines = steam.compare_regional_prices(aid)
            if info:
                web_game_stat_cache = f"🎮 {info['name']} ({aid})\n👥 Онлайн: {players}\n⭐ Metacritic: {info['metacritic']} | Цена KZ: {info['final_formatted']}\n🌍 {' | '.join([strip_html(x) for x in reg_lines])}"
    elif act == "reload_lib":
        library_cache["web"] = steam.get_all_licenses()
        bot_status["web_msg"] = f"Библиотека загружена: {len(library_cache['web'])} игр."
    elif act == "del_pkg":
        pkgid = request.form.get("pkgid", "")
        if steam.remove_license(pkgid):
            if "web" in library_cache:
                library_cache["web"] = [x for x in library_cache["web"] if x["packageid"] != pkgid]
            bot_status["web_msg"] = f"Лицензия {pkgid} удалена!"
    return redirect(url_for("web_index"))


@app.route("/web_update", methods=["POST"])
def web_update():
    if not session.get("logged_in"):
        return redirect(url_for("web_index"))
    uploaded = request.files.get("pyfile")
    new_code = uploaded.read().decode("utf-8-sig", errors="replace") if (uploaded and uploaded.filename) else request.form.get("code", "")
    _, msg = apply_new_bot_code(steam.data.get("admin_id"), new_code)
    bot_status["web_msg"] = f"Автообновление: {msg}"
    return redirect(url_for("web_index"))


@app.route("/ping")
def ping_endpoint():
    return jsonify({"status": "alive", "steam_configured": steam.is_configured(), "last_ping": bot_status["last_ping_time"], "ping_count": bot_status["ping_count"]}), 200


def do_single_ping():
    base_url = steam.data.get("render_url") or os.environ.get("RENDER_EXTERNAL_URL") or tok_config["render_url"]
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
        bot_status["ping_history"].append(f"• {now_str} — ✅ HTTP {r.status_code} ({ms} мс)")
    except Exception:
        now_str = time.strftime("%H:%M:%S")
        bot_status["last_ping_ts"] = time.time()
        bot_status["last_ping_time"] = now_str
        bot_status["last_ping_code"] = "Ошибка"
        bot_status["ping_history"].append(f"• {now_str} — ❌ Ошибка сети")
    if len(bot_status["ping_history"]) > 10:
        bot_status["ping_history"] = bot_status["ping_history"][-10:]


def keep_alive_pinger():
    time.sleep(10)
    do_single_ping()
    while True:
        interval_sec = max(60, int(bot_status.get("ping_interval_min", 4)) * 60)
        time.sleep(max(60, interval_sec + random.randint(-20, 20)))
        do_single_ping()


def notify_if_just_updated():
    upd_flag = steam.data.pop("just_updated", None)
    if upd_flag:
        save_session(steam.data)
        admin_id = steam.data.get("admin_id")
        if admin_id:
            time.sleep(1.5)
            notify_user(admin_id, f"🎉 **Автообновление успешно установлено (`{upd_flag}`)!**", get_main_keyboard())


def run_supervisor():
    script_path = os.path.abspath(__file__)
    env = os.environ.copy()
    env["BOT_WORKER_MODE"] = "1"
    while True:
        start_ts = time.time()
        proc = subprocess.Popen([sys.executable, script_path], env=env)
        exit_code = proc.wait()
        if exit_code == 42:
            time.sleep(2)
            continue
        if (time.time() - start_ts) < 5 and os.path.exists(BACKUP_FILE):
            try:
                shutil.copyfile(BACKUP_FILE, script_path)
            except Exception:
                pass
        time.sleep(3)


def run_worker():
    port = int(os.environ.get("PORT", 10000))
    threading.Thread(target=lambda: app.run(host="0.0.0.0", port=port, use_reloader=False), daemon=True).start()
    threading.Thread(target=giveaway_loop, daemon=True).start()
    threading.Thread(target=discounts_loop, daemon=True).start()
    threading.Thread(target=f2p_auto_all_loop, daemon=True).start()
    threading.Thread(target=keep_alive_pinger, daemon=True).start()
    threading.Thread(target=notify_if_just_updated, daemon=True).start()
    while True:
        try:
            bot.infinity_polling(skip_pending=True, timeout=20, long_polling_timeout=20)
        except Exception:
            time.sleep(3)


if __name__ == "__main__":
    if os.environ.get("BOT_WORKER_MODE") == "1":
        run_worker()
    else:
        run_supervisor()
# === КОНЕЦ ФАЙЛА ===
