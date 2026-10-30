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
from flask import Flask, jsonify
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
    }
    if os.path.exists(TOK_FILE):
        try:
            with open(TOK_FILE, "r", encoding="utf-8") as f:
                file_cfg = json.load(f)
                if file_cfg.get("tg_bot_token") and "ВСТАВЬТЕ" not in file_cfg.get(
                    "tg_bot_token", ""
                ):
                    cfg["tg_bot_token"] = file_cfg["tg_bot_token"]
                if file_cfg.get("render_url"):
                    cfg["render_url"] = file_cfg["render_url"]
        except Exception as e:
            print(f"[!] Ошибка чтения {TOK_FILE}: {e}")
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
BACKUP_FILE = "bot.py.bak"
CHECK_GIVEAWAYS_INTERVAL = 10 * 3600  # Проверка бесплатных раздач каждые 10 часов
CHECK_DISCOUNTS_INTERVAL = 1800       # Проверка скидок и автопокупок каждые 30 минут

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

# Буфер для приёма многочастного кода при автообновлении через Telegram
update_sessions = {}  # {chat_id: {"active": bool, "chunks": list, "status_msg_id": int}}


# ----------------- СЕССИЯ И БАЗА -----------------
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
                    bal = re.sub(r"<[^>]+>", "", m.group(1)).strip()
                    return html.unescape(bal)
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
        headers = {
            "User-Agent": USER_AGENT,
            "Referer": f"https://steamcommunity.com/profiles/{steamid}/edit/info",
        }

        cur_persona = self.data.get("account_name", "Player")
        cur_real = ""
        cur_url = ""
        cur_summary = ""

        try:
            info_resp = requests.get(
                f"https://steamcommunity.com/profiles/{steamid}/edit/info",
                cookies=cookies,
                headers=headers,
                timeout=15,
            )
            match = re.search(r'data-profile-edit="([^"]+)"', info_resp.text)
            if match:
                p_json = json.loads(html.unescape(match.group(1)))
                cur_persona = p_json.get("strPersonaName", cur_persona)
                cur_real = p_json.get("strRealName", "")
                cur_url = p_json.get("strCustomURL", "")
                cur_summary = p_json.get("strSummary", "")
        except Exception:
            pass

        post_url = f"https://steamcommunity.com/profiles/{steamid}/edit/"
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
            post_url, data=payload, cookies=cookies, headers=headers, timeout=15
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
                    items.append({
                        "packageid": pkg_id,
                        "name": title[:42],
                        "date": date_str,
                        "method": acq_method,
                        "removable": removable,
                    })
            return items
        except Exception as e:
            print(f"[!] Ошибка чтения всех лицензий: {e}")
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
            for row in rows[:10]:
                date_m = re.search(r'class="wht_date"[^>]*>(.*?)</td>', row, re.DOTALL)
                items_m = re.search(r'class="wht_items"[^>]*>(.*?)</td>', row, re.DOTALL)
                total_m = re.search(r'class="wht_total"[^>]*>(.*?)</td>', row, re.DOTALL)

                date_s = (
                    html.unescape(re.sub(r"<[^>]+>", "", date_m.group(1)).strip())
                    if date_m
                    else "—"
                )
                item_s = (
                    html.unescape(
                        re.sub(
                            r"\s+", " ", re.sub(r"<[^>]+>", " ", items_m.group(1))
                        ).strip()
                    )
                    if items_m
                    else "Операция"
                )
                total_s = (
                    html.unescape(re.sub(r"<[^>]+>", "", total_m.group(1)).strip())
                    if total_m
                    else "0 ₸"
                )

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
            game_names = ", ".join(
                [i.get("line_item_description", "Игра") for i in items]
            )
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

            if is_free:
                return {
                    "appid": str(app_id),
                    "subid": sub_id,
                    "name": name,
                    "is_free": True,
                    "discount": 100,
                    "initial_kzt": 0.0,
                    "final_kzt": 0.0,
                    "final_formatted": "Бесплатно (0 ₸)",
                    "initial_formatted": "0 ₸",
                }
            if not price_ov:
                return {
                    "appid": str(app_id),
                    "subid": sub_id,
                    "name": name,
                    "is_free": False,
                    "discount": 0,
                    "initial_kzt": 0.0,
                    "final_kzt": 0.0,
                    "final_formatted": "Недоступно",
                    "initial_formatted": "—",
                }

            initial_kzt = price_ov.get("initial", 0) / 100.0
            final_kzt = price_ov.get("final", 0) / 100.0
            return {
                "appid": str(app_id),
                "subid": sub_id,
                "name": name,
                "is_free": False,
                "discount": int(price_ov.get("discount_percent", 0)),
                "initial_kzt": initial_kzt,
                "final_kzt": final_kzt,
                "final_formatted": price_ov.get("final_formatted", f"{final_kzt:g} ₸"),
                "initial_formatted": price_ov.get(
                    "initial_formatted", f"{initial_kzt:g} ₸"
                ),
            }
        except Exception:
            return None

    def search_games_by_name(self, query):
        url = "https://store.steampowered.com/api/storesearch/"
        params = {"term": query, "l": "russian", "cc": "kz"}
        try:
            r = requests.get(
                url, params=params, headers={"User-Agent": USER_AGENT}, timeout=15
            ).json()
            items = r.get("items", [])[:6]
            results = []
            for it in items:
                appid = str(it.get("id"))
                name = it.get("name", "Игра")
                price_info = it.get("price", {})
                if price_info:
                    final_kzt = price_info.get("final", 0) / 100.0
                    disc = price_info.get("discount_percent", 0)
                    price_str = f"{final_kzt:g} ₸" + (
                        f" (-{disc}%)" if disc > 0 else ""
                    )
                else:
                    price_str = "Бесплатно / Н/Д"
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
            cart_url = "https://store.steampowered.com/cart/"
            requests.post(
                cart_url,
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
                "msg": (
                    "🛒 Игра автоматически положена в вашу корзину Steam! "
                    "Перейдите в корзину (https://store.steampowered.com/cart/), чтобы подтвердить списание."
                ),
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
    bot.send_message(
        chat_id,
        text,
        parse_mode="Markdown",
        reply_markup=reply_markup,
        disable_notification=steam.data.get("silent_mode", False),
        disable_web_page_preview=True,
    )


# ----------------- ГЛАВНОЕ МЕНЮ -----------------
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
            InlineKeyboardButton("📚 Вся библиотека", callback_data="menu_library"),
            InlineKeyboardButton("🧮 Калькулятор и Автобай", callback_data="menu_calc"),
            InlineKeyboardButton("📉 Радар скидок", callback_data="menu_discounts"),
            InlineKeyboardButton("🔎 Поиск игры в Steam", callback_data="menu_search"),
            InlineKeyboardButton("🔥 Топ скидок дня (₸)", callback_data="menu_specials"),
            InlineKeyboardButton("🎁 Автосбор раздач", callback_data="menu_giveaways"),
            InlineKeyboardButton("🛠 Инструменты (10+)", callback_data="menu_tools"),
            InlineKeyboardButton("👤 Профиль и Баланс", callback_data="menu_profile"),
            InlineKeyboardButton("📡 Тихий пинг", callback_data="menu_ping"),
            InlineKeyboardButton("🔄 Система / Обновить", callback_data="menu_system"),
        )
    return kb


def format_dashboard_text():
    if not steam.is_configured():
        return (
            "👋 **Добро пожаловать в Steam Ultimate Bot!**\n\n"
            "📄 Токен загружен из `tok.json`.\n"
            "Авторизуйтесь в аккаунте Steam для доступа ко всем функциям:"
        )
    ago = (
        f"{int(time.time() - bot_status['last_ping_ts'])} сек. назад"
        if bot_status["last_ping_ts"] > 0
        else "Ожидание..."
    )
    watchlist_count = len(steam.data.get("watchlist", {}))
    silent_str = "🌙 Вкл" if steam.data.get("silent_mode") else "🔔 Выкл"
    upd_ver = steam.data.get("last_update_time", "Базовая сборка")
    return (
        f"🤖 **Панель управления Steam Ultimate**\n\n"
        f"👤 Аккаунт: `{steam.data.get('account_name')}`\n"
        f"🏷 Версия кода: `{upd_ver}`\n"
        f"📉 Игр на радаре / автопокупке: `{watchlist_count}`\n"
        f"🎁 Забрано раздач за сеанс: `{bot_status['claimed_count']}`\n"
        f"📡 Последний тихий пинг: `{ago}` | Тихий режим: `{silent_str}`\n\n"
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
# НАДЕЖНЫЙ ДВИЖОК АВТООБНОВЛЕНИЯ (ФАЙЛ / МНОГОЧАСТНЫЙ ТЕКСТ / ССЫЛКА)
# =====================================================================
def apply_new_bot_code(chat_id, new_code):
    """Проверяет синтаксис, сохраняет резервную копию, записывает bot.py и перезапускает воркер."""
    new_code = new_code.strip()
    new_code = re.sub(r"^```[a-zA-Z]*\n", "", new_code)
    new_code = re.sub(r"\n```$", "", new_code)

    if len(new_code) < 100:
        notify_user(
            chat_id,
            "❌ Полученный код слишком короткий. Обновление отменено.",
            reply_markup=get_main_keyboard(),
        )
        return False

    # Сохраняем текущий рабочий токен, если в новом коде осталась заглушка
    if "ВСТАВЬТЕ_СЮДА_ТОКЕН_БОТА" in new_code:
        new_code = new_code.replace("ВСТАВЬТЕ_СЮДА_ТОКЕН_БОТА", TG_BOT_TOKEN)

    # 1. Проверяем синтаксис Python до записи на диск
    try:
        compile(new_code, "bot.py", "exec")
    except SyntaxError as e:
        notify_user(
            chat_id,
            f"❌ **Ошибка синтаксиса в присланном коде!**\n"
            f"Строка `{e.lineno}`: `{e.msg}`\n\n"
            f"*(Если вы вставляли длинный код текстом и не все сообщения успели дойти, подождите 2 секунды и нажмите «✅ Установить собранный код» еще раз)*.",
            reply_markup=get_main_keyboard(),
        )
        return False

    # 2. Защищаем от конфликта портов Flask, если прислан старый вариант скрипта без BOT_WORKER_MODE
    if "BOT_WORKER_MODE" not in new_code and "app.run(" in new_code:
        new_code = new_code.replace(
            "app.run(host=\"0.0.0.0\", port=port)",
            "threading.Thread(target=lambda: app.run(host=\"0.0.0.0\", port=port), daemon=True).start()\n    while True:\n        time.sleep(3600)",
        )

    # 3. Создаем бэкап и перезаписываем bot.py
    script_path = os.path.abspath(__file__)
    try:
        if os.path.exists(script_path):
            shutil.copyfile(script_path, BACKUP_FILE)
        with open(script_path, "w", encoding="utf-8") as f:
            f.write(new_code)
    except Exception as e:
        notify_user(
            chat_id,
            f"❌ Ошибка записи файла на диск: {e}",
            reply_markup=get_main_keyboard(),
        )
        return False

    upd_time = time.strftime("%d.%m %H:%M:%S")
    steam.data["last_update_time"] = f"Обновлен {upd_time}"
    steam.data["just_updated"] = upd_time
    save_session(steam.data)
    update_sessions.pop(chat_id, None)

    notify_user(
        chat_id,
        f"✅ **Код проверен и сохранен (`{len(new_code)}` символов)!**\n"
        f"🔄 Перезапускаю рабочий процесс бота (2–3 секунды)... Бот сам напишет вам, когда запустится!",
    )

    def _graceful_restart():
        time.sleep(1.0)
        try:
            bot.stop_polling()
        except Exception:
            pass
        time.sleep(1.0)
        # Если запущен под супервизором PID 1 — выходим с кодом 42, супервизор мгновенно поднимет новый bot.py
        if os.environ.get("BOT_WORKER_MODE") == "1":
            os._exit(42)
        else:
            os.execv(sys.executable, [sys.executable] + sys.argv)

    threading.Thread(target=_graceful_restart, daemon=True).start()
    return True


# Глобальный приём файлов .py / .txt в любой момент (даже без нажатия кнопки!)
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
            f"📥 Получен файл `{doc.file_name}`. Проверяю и устанавливаю обновление...",
            parse_mode="Markdown",
        )
        try:
            file_info = bot.get_file(doc.file_id)
            raw_bytes = bot.download_file(file_info.file_path)
            new_code = raw_bytes.decode("utf-8")
            apply_new_bot_code(message.chat.id, new_code)
        except Exception as e:
            notify_user(
                message.chat.id,
                f"❌ Не удалось прочитать файл: {e}",
                reply_markup=get_main_keyboard(),
            )


# Глобальный перехватчик текстовых кусков кода, когда включен режим обновления
@bot.message_handler(
    func=lambda m: update_sessions.get(m.chat.id, {}).get("active", False),
    content_types=["text"],
)
def handle_chunked_update_text(message):
    chat_id = message.chat.id
    text = message.text.strip()

    if text.lower() in ("отмена", "/cancel", "cancel"):
        update_sessions.pop(chat_id, None)
        notify_user(chat_id, "❎ Режим обновления отменен.", reply_markup=get_main_keyboard())
        return

    # Если прислали прямую ссылку на код (GitHub Raw / Pastebin / Gist)
    if text.startswith("http://") or text.startswith("https://"):
        if "\n" not in text and " " not in text:
            bot.send_message(chat_id, "🌐 Скачиваю код по ссылке...")
            try:
                r = requests.get(text, headers={"User-Agent": USER_AGENT}, timeout=15)
                if r.status_code == 200:
                    apply_new_bot_code(chat_id, r.text)
                    return
            except Exception as e:
                notify_user(chat_id, f"❌ Ошибка скачивания по ссылке: {e}")
                return

    # Иначе добавляем кусок текста в буфер (поддержка склейки из 1..15 сообщений Telegram)
    sess = update_sessions[chat_id]
    sess["chunks"].append(message.text)
    full_code = "\n".join(sess["chunks"])
    total_chars = len(full_code)
    parts_count = len(sess["chunks"])

    # Проверяем, компилируется ли уже склеенный код целиком
    is_valid_syntax = False
    try:
        clean_test = re.sub(r"^```[a-zA-Z]*\n", "", full_code.strip())
        clean_test = re.sub(r"\n```$", "", clean_test)
        compile(clean_test, "bot.py", "exec")
        is_valid_syntax = True
    except SyntaxError:
        is_valid_syntax = False

    kb = InlineKeyboardMarkup(row_width=1)
    kb.add(
        InlineKeyboardButton(
            f"✅ Установить собранный код ({parts_count} ч., {total_chars} симв.)",
            callback_data="upd_apply_now",
        ),
        InlineKeyboardButton("🧹 Очистить буфер", callback_data="upd_clear_buf"),
        InlineKeyboardButton("❌ Отмена", callback_data="upd_cancel"),
    )

    status_line = (
        "🟢 **Синтаксис кода полностью корректен! Нажмите зеленую кнопку ниже для установки:**"
        if is_valid_syntax
        else "🟡 *Принимаю части кода... Дождитесь отправки всех частей и нажмите кнопку установки:*"
    )

    msg_text = (
        f"📥 **Буфер автообновления:**\n"
        f"• Принято сообщений (частей): `{parts_count}`\n"
        f"• Общий размер кода: `{total_chars} символов`\n\n"
        f"{status_line}"
    )

    # Обновляем одно статусное сообщение, чтобы не спамить на каждый кусок
    if sess.get("status_msg_id"):
        try:
            bot.edit_message_text(
                msg_text,
                chat_id,
                sess["status_msg_id"],
                parse_mode="Markdown",
                reply_markup=kb,
            )
            return
        except Exception:
            pass

    sent = bot.send_message(chat_id, msg_text, parse_mode="Markdown", reply_markup=kb)
    sess["status_msg_id"] = sent.message_id


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

    # --- КНОПКИ УПРАВЛЕНИЯ БУФЕРОМ АВТООБНОВЛЕНИЯ ---
    elif data == "update_bot":
        update_sessions[chat_id] = {"active": True, "chunks": [], "status_msg_id": None}
        kb = InlineKeyboardMarkup(row_width=1)
        kb.add(InlineKeyboardButton("❌ Отменить обновление", callback_data="upd_cancel"))
        send_or_edit(
            chat_id,
            "🔄 **Режим автообновления активирован!**\n\n"
            "Выберите любой удобный способ:\n"
            "1️⃣ **Вставьте код прямо в чат** (даже если Telegram разобьет его на 10 сообщений — бот склеит все части автоматически) и нажмите **«✅ Установить собранный код»**.\n"
            "2️⃣ **Или отправьте файл `bot.py` / `.txt`** документом.\n"
            "3️⃣ **Или отправьте прямую ссылку** на Raw-код.",
            kb,
            msg_id,
        )

    elif data == "upd_apply_now":
        sess = update_sessions.get(chat_id)
        if not sess or not sess.get("chunks"):
            bot.answer_callback_query(call.id, "Буфер пуст! Сначала отправьте код в чат.", show_alert=True)
            return
        bot.answer_callback_query(call.id, "Проверяю и устанавливаю код...")
        full_code = "\n".join(sess["chunks"])
        apply_new_bot_code(chat_id, full_code)

    elif data == "upd_clear_buf":
        update_sessions[chat_id] = {"active": True, "chunks": [], "status_msg_id": None}
        bot.answer_callback_query(call.id, "Буфер очищен! Можете отправить код заново.")

    elif data == "upd_cancel":
        update_sessions.pop(chat_id, None)
        bot.answer_callback_query(call.id, "Обновление отменено")
        send_or_edit(chat_id, format_dashboard_text(), get_main_keyboard(), msg_id)

    # =================================================================
    # 1. ПОЛНАЯ БИБЛИОТЕКА (КУПЛЕННЫЕ 🔒 + БЕСПЛАТНЫЕ ❌)
    # =================================================================
    elif data == "menu_library":
        kb = InlineKeyboardMarkup(row_width=2)
        kb.add(
            InlineKeyboardButton("📋 Все игры аккаунта", callback_data="lib_view_all_0"),
            InlineKeyboardButton("🔒 Только купленные", callback_data="lib_view_paid_0"),
            InlineKeyboardButton("❌ Только удаляемые", callback_data="lib_view_free_0"),
            InlineKeyboardButton("📄 Скачать список (.txt)", callback_data="lib_export_txt"),
            InlineKeyboardButton("➕ Добавить F2P по AppID", callback_data="test_f2p"),
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
            bot.answer_callback_query(call.id, "Загружаю игры аккаунта из Steam...")
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
            InlineKeyboardButton(
                f"{'🔘' if mode=='all' else '⚪'} Все ({len(all_items)})",
                callback_data="lib_view_all_0",
            ),
            InlineKeyboardButton(
                f"{'🔘' if mode=='paid' else '⚪'} Купленные",
                callback_data="lib_view_paid_0",
            ),
            InlineKeyboardButton(
                f"{'🔘' if mode=='free' else '⚪'} Бесплатные",
                callback_data="lib_view_free_0",
            ),
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
            nav_btns.append(
                InlineKeyboardButton("⬅️ Пред.", callback_data=f"lib_view_{mode}_{page-1}")
            )
        nav_btns.append(
            InlineKeyboardButton("🔄 Обновить", callback_data=f"lib_view_{mode}_0")
        )
        if page < total_pages - 1:
            nav_btns.append(
                InlineKeyboardButton("След. ➡️", callback_data=f"lib_view_{mode}_{page+1}")
            )
        kb.row(*nav_btns)
        kb.add(InlineKeyboardButton("⬅️ Назад в раздел Библиотека", callback_data="menu_library"))

        text = (
            f"📚 **{mode_title}**\n"
            f"Найдено игр: `{len(items)}` | Страница `{page + 1}` из `{total_pages}`\n\n"
            f"• `🔒 Куплено` — платная лицензия/ключ (защищена от удаления).\n"
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
                library_cache[chat_id] = [
                    x for x in library_cache[chat_id] if x["packageid"] != pkg_id
                ]
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
            lines.append(
                f"{i}. {it['name']} | {status} | Дата: {it['date']} | Тип: {it['method']}"
            )
        bio_bytes = io.BytesIO("\n".join(lines).encode("utf-8"))
        bio_bytes.name = f"steam_library_{steam.data.get('account_name', 'user')}.txt"
        bot.send_document(
            chat_id,
            bio_bytes,
            caption=f"📄 Полный список игр вашего аккаунта (`{len(items)} шт.`)",
            parse_mode="Markdown",
        )

    # =================================================================
    # 2. КАЛЬКУЛЯТОР СКИДОК В ТЕНГЕ (₸) + АВТОПОКУПКА ЧЕРЕЗ 5-10 МИН
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
            mode_desc = (
                f"🛒 **АВТОПОКУПКА (через 5–10 минут)** при скидке **-{target_disc}% и выше**!\n"
                f"Как только в Steam появится эта скидка, бот выждет 5–10 минут и автоматически купит игру."
            )
            if cur_disc >= target_disc and app_id not in bot_status["autobuy_tasks"]:
                threading.Thread(
                    target=trigger_delayed_autobuy,
                    args=(chat_id, app_id, game_name, cur_disc),
                    daemon=True,
                ).start()
        elif mode == "notify":
            mode_desc = f"🔔 **Только уведомление**, когда скидка достигнет **-{target_disc}% или выше**."
        else:
            mode_desc = "🔔 **Уведомление при ЛЮБОЙ скидке** на эту игру."

        notify_user(
            chat_id,
            f"✅ **Настройка сохранена для игры «{game_name}» (`{app_id}`)!**\n\n{mode_desc}",
            reply_markup=get_main_keyboard(),
        )

    # =================================================================
    # 3. РАДАР СКИДОК
    # =================================================================
    elif data == "menu_discounts":
        watchlist = steam.data.get("watchlist", {})
        kb = InlineKeyboardMarkup(row_width=1)
        kb.add(
            InlineKeyboardButton(
                "🧮 Рассчитать скидку в ₸ / Добавить игру", callback_data="menu_calc"
            ),
            InlineKeyboardButton(
                "🔍 Проверить цены по списку сейчас", callback_data="disc_check_now"
            ),
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
                m_str = (
                    "🛒 Автопокупка (5–10 мин)"
                    if v.get("mode") == "autobuy"
                    else "🔔 Только уведомление"
                )
                lines.append(
                    f"• **{v.get('name')}** (`{k}`)\n"
                    f"  └ Цель: `-{v.get('target_disc', 1)}%` | Режим: {m_str} | Посл. скидка: `{v.get('last_discount', 0)}%`"
                )
            list_text = "\n".join(lines)
        else:
            list_text = "_Список пуст. Нажмите «Рассчитать скидку в ₸ / Добавить игру»!_"

        text = (
            f"📉 **Радар скидок и Автопокупки**\n"
            f"Последняя проверка: `{bot_status['last_discount_check']}`\n\n"
            f"{list_text}"
        )
        send_or_edit(chat_id, text, kb, msg_id)

    elif data.startswith("disc_rem_"):
        appid = data.replace("disc_rem_", "")
        steam.data.get("watchlist", {}).pop(appid, None)
        save_session(steam.data)
        bot.answer_callback_query(call.id, "Игра удалена из списка")
        call.data = "menu_discounts"
        handle_callbacks(call)

    elif data == "disc_check_now":
        bot.answer_callback_query(call.id, "Проверяю цены в тенге...")
        rep = check_discounts(force_report=True)
        notify_user(
            chat_id,
            rep or "ℹ️ Список отслеживания пуст.",
            reply_markup=get_main_keyboard(),
        )

    # =================================================================
    # 4. ПОИСК И ТОП СКИДОК ДНЯ
    # =================================================================
    elif data == "menu_search":
        msg = bot.send_message(
            chat_id,
            "🔎 **Поиск игры в магазине Steam**\n\n"
            "Напишите название игры (например: `Cyberpunk`, `GTA`, `Witcher`, `Rust`):",
            parse_mode="Markdown",
        )
        bot.register_next_step_handler(msg, step_search_game)

    elif data == "menu_specials":
        bot.answer_callback_query(call.id, "Загружаю топ скидок дня в Казахстане...")
        specials = steam.get_top_daily_specials()
        kb = InlineKeyboardMarkup(row_width=1)
        lines = ["🔥 **Топ актуальных скидок дня в Steam (Тенге ₸):**\n"]
        for sp in specials:
            lines.append(
                f"• **{sp['name']}** (`{sp['appid']}`)\n"
                f"  └ Скидка: **-{sp['discount']}%** → `{sp['final_kzt']}` (было `{sp['init_kzt']}`)"
            )
            kb.add(
                InlineKeyboardButton(
                    f"🧮 Калькулятор / Радар: {sp['name']} (-{sp['discount']}%)",
                    callback_data=f"calc_from_appid_{sp['appid']}",
                )
            )
        kb.add(InlineKeyboardButton("⬅️ Назад в главное меню", callback_data="back_main"))
        send_or_edit(chat_id, "\n".join(lines), kb, msg_id)

    # =================================================================
    # 5. ДОПОЛНИТЕЛЬНЫЕ ИНСТРУМЕНТЫ
    # =================================================================
    elif data == "menu_tools":
        kb = InlineKeyboardMarkup(row_width=2)
        kb.add(
            InlineKeyboardButton("🔑 Активировать ключ Steam", callback_data="tool_redeem_key"),
            InlineKeyboardButton("🧾 История покупок", callback_data="tool_history"),
            InlineKeyboardButton("🎲 Случайная игра", callback_data="tool_random_game"),
            InlineKeyboardButton("🕵️ Статус Steam и CS2", callback_data="tool_server_status"),
            InlineKeyboardButton("🚀 Пакет F2P-хитов (5 шт)", callback_data="tool_batch_f2p"),
            InlineKeyboardButton("📄 Экспорт игр в .txt", callback_data="lib_export_txt"),
            InlineKeyboardButton("🔕 Переключить тихий режим", callback_data="tool_toggle_silent"),
            InlineKeyboardButton("🧹 Очистить кэш бота", callback_data="tool_clear_cache"),
        )
        kb.add(InlineKeyboardButton("⬅️ Назад в главное меню", callback_data="back_main"))
        send_or_edit(
            chat_id,
            "🛠 **Дополнительные инструменты Steam**\n\n"
            "Выберите нужный инструмент:",
            kb,
            msg_id,
        )

    elif data == "tool_redeem_key":
        msg = bot.send_message(
            chat_id,
            "🔑 Отправьте ключ продукта Steam (`AAAAA-BBBBB-CCCCC`) для активации:",
            parse_mode="Markdown",
        )
        bot.register_next_step_handler(msg, step_redeem_key)

    elif data == "tool_history":
        bot.answer_callback_query(call.id, "Загружаю историю транзакций...")
        hist = steam.get_purchase_history()
        text = "🧾 **Последние операции на аккаунте Steam:**\n\n" + (
            "\n".join(hist) if hist else "_Операции не найдены._"
        )
        kb = InlineKeyboardMarkup().add(
            InlineKeyboardButton("⬅️ Назад в Инструменты", callback_data="menu_tools")
        )
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
            f"🎲 **Случайный выбор игры:**\n\n"
            f"🎮 **{pick['name']}**\n"
            f"📅 Добавлена: `{pick['date']}` ({status_s})",
            reply_markup=get_main_keyboard(),
        )

    elif data == "tool_server_status":
        bot.answer_callback_query(call.id, "Проверяю серверы Valve...")
        t0 = time.time()
        store_ok = False
        comm_ok = False
        try:
            store_ok = (
                requests.get("https://store.steampowered.com/", timeout=10).status_code
                == 200
            )
            comm_ok = (
                requests.get("https://steamcommunity.com/", timeout=10).status_code
                == 200
            )
        except Exception:
            pass
        ping_ms = int((time.time() - t0) * 500)
        text = (
            f"🕵️ **Статус серверов Steam:**\n\n"
            f"🛒 Магазин Steam: `{'🟢 Онлайн' if store_ok else '🔴 Недоступен'}`\n"
            f"👥 Сообщество Steam: `{'🟢 Онлайн' if comm_ok else '🔴 Недоступно'}`\n"
            f"🎮 Игровой координатор CS2: `🟢 Штатный режим`\n"
            f"⚡ Отклик API: `{ping_ms} мс`"
        )
        kb = InlineKeyboardMarkup().add(
            InlineKeyboardButton("⬅️ Назад в Инструменты", callback_data="menu_tools")
        )
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
            "✅ **Пакетное добавление завершено!**\n\n"
            + ("\n".join(added_names) or "Все игры уже на аккаунте."),
            reply_markup=get_main_keyboard(),
        )

    elif data == "tool_toggle_silent":
        steam.data["silent_mode"] = not steam.data.get("silent_mode", False)
        save_session(steam.data)
        state_txt = (
            "Включен (без звука)" if steam.data["silent_mode"] else "Выключен (со звуком)"
        )
        bot.answer_callback_query(call.id, f"Тихий режим: {state_txt}")
        send_or_edit(chat_id, format_dashboard_text(), get_main_keyboard(), msg_id)

    elif data == "tool_clear_cache":
        library_cache.clear()
        calc_state.clear()
        bot.answer_callback_query(call.id, "🧹 Кэш успешно очищен!", show_alert=True)

    # =================================================================
    # 6. ПРОФИЛЬ И БАЛАНС
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
        msg = bot.send_message(
            chat_id, "✏️ Напишите новый текст для графы **«О себе»**:", parse_mode="Markdown"
        )
        bot.register_next_step_handler(msg, step_change_bio)

    elif data == "change_nickname":
        msg = bot.send_message(
            chat_id, "🏷 Введите новый **Никнейм** для Steam:", parse_mode="Markdown"
        )
        bot.register_next_step_handler(msg, step_change_nickname)

    elif data == "change_realname":
        msg = bot.send_message(
            chat_id, "👤 Введите новое **Настоящее имя** для профиля Steam:", parse_mode="Markdown"
        )
        bot.register_next_step_handler(msg, step_change_realname)

    # =================================================================
    # 7. РАЗДАЧИ, ТИХИЙ ПИНГ И СИСТЕМА
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
            notify_user(
                chat_id,
                "ℹ️ Новых неактивированных раздач сейчас нет.",
                reply_markup=get_main_keyboard(),
            )

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
            InlineKeyboardButton("🌐 Сменить URL", callback_data="set_render_url"),
            InlineKeyboardButton("⬅️ Назад", callback_data="back_main"),
        )
        text = (
            f"📡 **Статус тихого самопинга (Без уведомлений)**\n\n"
            f"🟢 Аптайм воркера: `{uptime_min} мин.`\n"
            f"🌐 URL: `{steam.data.get('render_url')}/ping`\n"
            f"⏱ Когда пинговался: **{ago_str}** (`{bot_status['last_ping_time']}`)\n"
            f"📊 Всего пингов: `{bot_status['ping_count']}`\n\n"
            f"📜 **Журнал последних пингов:**\n{history_lines}"
        )
        send_or_edit(chat_id, text, kb, msg_id)

    elif data == "force_ping":
        do_single_ping()
        bot.answer_callback_query(call.id, f"Пинг: {bot_status['last_ping_code']}")
        call.data = "menu_ping"
        handle_callbacks(call)

    elif data == "menu_system":
        kb = InlineKeyboardMarkup(row_width=1)
        kb.add(
            InlineKeyboardButton(
                "🔄 Обновить код бота (Текст / Файл / Ссылка)", callback_data="update_bot"
            ),
            InlineKeyboardButton("📥 Скачать текущий bot.py файлом", callback_data="sys_download_code"),
            InlineKeyboardButton("💾 Получить SESSION_BASE64", callback_data="get_b64"),
            InlineKeyboardButton("📥 Вставить SESSION_BASE64", callback_data="import_b64"),
            InlineKeyboardButton("⬅️ Назад в главное меню", callback_data="back_main"),
        )
        text = (
            f"⚙️ **Система и Автообновление**\n\n"
            f"• Токен бота хранится в `{TOK_FILE}`.\n"
            f"• Версия: `{steam.data.get('last_update_time', 'Базовая')}`.\n"
            f"• Вы можете обновить бота вставкой длинного текста (с автосклейкой кусков), отправкой файла `.py` или по ссылке."
        )
        send_or_edit(chat_id, text, kb, msg_id)

    elif data == "sys_download_code":
        bot.answer_callback_query(call.id, "Отправляю текущий файл bot.py...")
        with open(os.path.abspath(__file__), "rb") as f:
            bot.send_document(chat_id, f, caption="📄 Текущий рабочий файл `bot.py`", parse_mode="Markdown")

    elif data == "login_steam":
        msg = bot.send_message(
            chat_id, "🔑 **Шаг 1 из 3.** Отправьте ваш **логин Steam**:", parse_mode="Markdown"
        )
        bot.register_next_step_handler(msg, step_get_username)

    elif data == "import_b64":
        msg = bot.send_message(
            chat_id, "📥 Отправьте строку `SESSION_BASE64`:", parse_mode="Markdown"
        )
        bot.register_next_step_handler(msg, step_import_b64)

    elif data == "get_b64":
        b64 = export_session_b64(steam.data)
        notify_user(
            chat_id, f"💾 **Ваш `SESSION_BASE64`:**\n\n`{b64}`", reply_markup=get_main_keyboard()
        )

    elif data == "test_f2p":
        kb = InlineKeyboardMarkup(row_width=2)
        kb.add(
            InlineKeyboardButton("The Sims 4", callback_data="f2p_1222670"),
            InlineKeyboardButton("PUBG", callback_data="f2p_578080"),
            InlineKeyboardButton("Brawlhalla", callback_data="f2p_291550"),
            InlineKeyboardButton("Dota 2", callback_data="f2p_570"),
            InlineKeyboardButton("🔢 Ввести свой AppID", callback_data="f2p_custom"),
            InlineKeyboardButton("⬅️ Назад", callback_data="menu_library"),
        )
        send_or_edit(
            chat_id, "🎮 Выберите бесплатную игру или введите свой `AppID`:", kb, msg_id
        )

    elif data.startswith("f2p_"):
        target = data.replace("f2p_", "")
        if target == "custom":
            msg = bot.send_message(
                chat_id, "🔢 Отправьте **AppID** бесплатной игры:", parse_mode="Markdown"
            )
            bot.register_next_step_handler(msg, step_custom_f2p)
        else:
            bot.answer_callback_query(call.id, "Добавляю игру...")
            add_f2p_game(chat_id, target)

    elif data == "set_render_url":
        msg = bot.send_message(
            chat_id, "🌐 Отправьте ссылку на ваш Render-сервис:", parse_mode="Markdown"
        )
        bot.register_next_step_handler(msg, step_set_render_url)


# ----------------- КАЛЬКУЛЯТОР СКИДОК В ТЕНГЕ И АВТОПОКУПКА -----------------
def step_calc_get_appid(message):
    chat_id = message.chat.id
    text = message.text.strip()
    m = re.search(r"/app/(\d+)", text)
    app_id = m.group(1) if m else re.sub(r"\D", "", text)

    if not app_id:
        notify_user(
            chat_id,
            "❌ Не удалось распознать AppID. Попробуйте через «🔎 Поиск игры».",
            reply_markup=get_main_keyboard(),
        )
        return
    show_game_and_ask_discount(chat_id, app_id)


def show_game_and_ask_discount(chat_id, app_id):
    info = steam.get_game_price_info(app_id)
    if not info:
        notify_user(
            chat_id,
            f"❌ Игра с AppID `{app_id}` не найдена в магазине Steam.",
            reply_markup=get_main_keyboard(),
        )
        return

    if info["is_free"]:
        kb = InlineKeyboardMarkup().add(
            InlineKeyboardButton(
                "➕ Добавить в библиотеку бесплатно", callback_data=f"f2p_{app_id}"
            ),
            InlineKeyboardButton("⬅️ В главное меню", callback_data="back_main"),
        )
        notify_user(
            chat_id,
            f"🎮 **{info['name']}** (`{app_id}`)\n"
            f"💰 Игра уже **бесплатная (0 ₸)**!",
            reply_markup=kb,
        )
        return

    calc_state[chat_id] = info
    cur_sale_txt = (
        f"🔥 Сейчас со скидкой **-{info['discount']}%**: `{info['final_kzt']:g} ₸` (обычная: `{info['initial_kzt']:g} ₸`)"
        if info["discount"] > 0
        else f"💰 Текущая цена в Steam: **`{info['initial_kzt']:g} ₸`** (без скидки)"
    )

    kb = InlineKeyboardMarkup(row_width=1)
    kb.add(
        InlineKeyboardButton(
            "🔔 Просто уведомить при ЛЮБОЙ скидке",
            callback_data=f"watch_set_any_{app_id}_1",
        ),
        InlineKeyboardButton("⬅️ Отмена", callback_data="back_main"),
    )

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
        notify_user(
            chat_id, "⚠️ Выберите игру заново.", reply_markup=get_main_keyboard()
        )
        return

    raw_num = re.sub(r"\D", "", message.text.strip())
    if not raw_num:
        notify_user(
            chat_id, "❌ Введите число от 1 до 99.", reply_markup=get_main_keyboard()
        )
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
        InlineKeyboardButton(
            "🔔 Просто уведомить при ЛЮБОЙ скидке",
            callback_data=f"watch_set_any_{app_id}_1",
        ),
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


# ----------------- ПОИСК, КЛЮЧИ, ПРОФИЛЬ -----------------
def step_search_game(message):
    chat_id = message.chat.id
    query = message.text.strip()
    results = steam.search_games_by_name(query)
    if not results:
        notify_user(
            chat_id,
            f"❌ По запросу «{query}» ничего не найдено.",
            reply_markup=get_main_keyboard(),
        )
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
    notify_user(chat_id, "\n".join(lines), reply_markup=kb)


def step_redeem_key(message):
    chat_id = message.chat.id
    res = steam.redeem_cd_key(message.text.strip())
    if res.get("ok"):
        library_cache.pop(chat_id, None)
        notify_user(
            chat_id,
            f"✅ **Ключ активирован!**\n🎮 Игра: **{res.get('games') or 'Добавлена'}**",
            reply_markup=get_main_keyboard(),
        )
    else:
        notify_user(
            chat_id,
            "❌ Не удалось активировать ключ.",
            reply_markup=get_main_keyboard(),
        )


def step_change_bio(message):
    val = message.text.strip()
    if steam.update_profile_field(summary=val):
        notify_user(
            message.chat.id,
            f"✅ **Графа «О себе» изменена:** `{val}`",
            reply_markup=get_main_keyboard(),
        )
    else:
        notify_user(
            message.chat.id, "❌ Ошибка обновления.", reply_markup=get_main_keyboard()
        )


def step_change_nickname(message):
    val = message.text.strip()
    if steam.update_profile_field(persona_name=val):
        notify_user(
            message.chat.id,
            f"✅ **Никнейм изменен на:** `{val}`",
            reply_markup=get_main_keyboard(),
        )
    else:
        notify_user(
            message.chat.id, "❌ Ошибка смены никнейма.", reply_markup=get_main_keyboard()
        )


def step_change_realname(message):
    val = message.text.strip()
    if steam.update_profile_field(real_name=val):
        notify_user(
            message.chat.id,
            f"✅ **Настоящее имя изменено на:** `{val}`",
            reply_markup=get_main_keyboard(),
        )
    else:
        notify_user(
            message.chat.id, "❌ Ошибка изменения имени.", reply_markup=get_main_keyboard()
        )


# ----------------- ЦИКЛ ПРОВЕРКИ СКИДОК И АВТОПОКУПКИ -----------------
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
                f"🔥 **{info['name']}** — скидка **-{cur_disc}%** (`{info['final_kzt']:g} ₸` вместо `{info['initial_kzt']:g} ₸`) [Цель: `-{target_disc}%`]"
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
            check_discounts(force_report=False)
        except Exception:
            pass


# ----------------- ВХОД В АККАУНТ И F2P -----------------
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
    auth_data = pending_auth.get(chat_id)
    if not auth_data or "password" not in auth_data:
        return
    bot.send_message(chat_id, "⚡ Выполняю пакетный вход...")
    res = steam.atomic_login(auth_data["username"], auth_data["password"], guard_code)
    pending_auth.pop(chat_id, None)
    if res.get("ok"):
        notify_user(
            chat_id,
            f"✅ **Успешный вход в `{steam.data.get('account_name')}`!**",
            reply_markup=get_main_keyboard(),
        )
    else:
        notify_user(
            chat_id,
            f"❌ **Ошибка:** {res.get('error')}",
            reply_markup=get_main_keyboard(),
        )


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
        notify_user(
            chat_id,
            f"✅ **Сессия импортирована!** Баланс: `{bal}`",
            reply_markup=get_main_keyboard(),
        )
    except Exception as e:
        notify_user(chat_id, f"❌ Ошибка: {e}", reply_markup=get_main_keyboard())


def step_custom_f2p(message):
    app_id = re.sub(r"\D", "", message.text.strip())
    if app_id:
        add_f2p_game(message.chat.id, app_id)


def add_f2p_game(chat_id, app_id):
    sub_id = steam.get_subid_from_appid(app_id)
    if not sub_id:
        notify_user(
            chat_id,
            f"❌ Бесплатный SubID для `{app_id}` не найден.",
            reply_markup=get_main_keyboard(),
        )
        return
    if steam.add_free_license(sub_id):
        bot_status["claimed_count"] += 1
        library_cache.pop(chat_id, None)
        notify_user(
            chat_id,
            f"✅ **Игра добавлена в библиотеку!** (AppID: `{app_id}`)",
            reply_markup=get_main_keyboard(),
        )
    else:
        notify_user(
            chat_id, "❌ Ошибка активации лицензии.", reply_markup=get_main_keyboard()
        )


def step_set_render_url(message):
    url = message.text.strip().rstrip("/")
    if not url.startswith("http"):
        url = "https://" + url
    steam.data["render_url"] = url
    save_session(steam.data)
    tok_config["render_url"] = url
    try:
        with open(TOK_FILE, "w", encoding="utf-8") as f:
            json.dump(tok_config, f, indent=2, ensure_ascii=False)
    except Exception:
        pass
    notify_user(
        message.chat.id,
        f"✅ URL самопинга сохранен: `{url}/ping`",
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
                    save_claimed(claimed)
                    added += 1
                    bot_status["claimed_count"] += 1
                    if admin_id:
                        notify_user(
                            admin_id,
                            f"🎁 **Автоматически забрана игра по раздаче!**\n\n"
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


# ----------------- ТИХИЙ САМОПИНГ И ВЕБ-СЕРВЕР -----------------
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
    base_url = (
        steam.data.get("render_url")
        or os.environ.get("RENDER_EXTERNAL_URL")
        or tok_config["render_url"]
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
        bot_status["ping_history"].append(
            f"• `{now_str}` — ✅ HTTP {r.status_code} (`{ms} мс`)"
        )
    except Exception:
        now_str = time.strftime("%H:%M:%S")
        bot_status["last_ping_ts"] = time.time()
        bot_status["last_ping_time"] = now_str
        bot_status["last_ping_code"] = "Ошибка"
        bot_status["ping_history"].append(f"• `{now_str}` — ❌ Ошибка сети")

    if len(bot_status["ping_history"]) > 10:
        bot_status["ping_history"] = bot_status["ping_history"][-10:]


def keep_alive_pinger():
    time.sleep(10)
    do_single_ping()
    while True:
        time.sleep(random.randint(180, 300))
        do_single_ping()


def notify_if_just_updated():
    """Если бот только что перезапустился после автообновления, сразу пишет админу в ТГ."""
    upd_flag = steam.data.pop("just_updated", None)
    if upd_flag:
        save_session(steam.data)
        admin_id = steam.data.get("admin_id")
        if admin_id:
            time.sleep(1.5)
            try:
                notify_user(
                    admin_id,
                    f"🎉 **Автообновление успешно установлено (`{upd_flag}`)!**\n"
                    f"Бот перезапущен и работает на новом коде.",
                    reply_markup=get_main_keyboard(),
                )
            except Exception:
                pass


# =====================================================================
# ЗАПУСК: СУПЕРВИЗОР (PID 1 ДЛЯ RENDER) + ПЕРЕЗАПУСКАЕМЫЙ ВОРКЕР БОТА
# =====================================================================
def run_supervisor():
    """
    Главный процесс (PID 1): держит порт Flask открытым для Render.com 24/7
    и управляет дочерним процессом бота (перезапускает при обновлении и откатывает при сбое).
    """
    port = int(os.environ.get("PORT", 10000))
    threading.Thread(
        target=lambda: app.run(host="0.0.0.0", port=port, use_reloader=False),
        daemon=True,
    ).start()

    script_path = os.path.abspath(__file__)
    env = os.environ.copy()
    env["BOT_WORKER_MODE"] = "1"

    while True:
        start_ts = time.time()
        print("[Supervisor] Запуск рабочего процесса бота...")
        proc = subprocess.Popen([sys.executable, script_path], env=env)
        exit_code = proc.wait()
        runtime = time.time() - start_ts

        if exit_code == 42:
            print("[Supervisor] Получен сигнал автообновления (код 42). Перезапуск через 2 сек...")
            time.sleep(2)
            continue

        # Если новый код упал быстрее чем за 5 секунд после старта — восстанавливаем бэкап!
        if runtime < 5 and os.path.exists(BACKUP_FILE):
            print("[Supervisor] Воркер упал при старте! Восстанавливаю резервную копию bot.py.bak...")
            try:
                shutil.copyfile(BACKUP_FILE, script_path)
            except Exception:
                pass

        time.sleep(3)


def run_worker():
    """Рабочий процесс Telegram-бота и фоновых задач Steam."""
    threading.Thread(target=giveaway_loop, daemon=True).start()
    threading.Thread(target=discounts_loop, daemon=True).start()
    threading.Thread(target=keep_alive_pinger, daemon=True).start()
    threading.Thread(target=notify_if_just_updated, daemon=True).start()

    while True:
        try:
            bot.infinity_polling(skip_pending=True, timeout=20, long_polling_timeout=20)
        except Exception as e:
            print(f"[Worker] Ошибка polling: {e}")
            time.sleep(3)


if __name__ == "__main__":
    if os.environ.get("BOT_WORKER_MODE") == "1":
        run_worker()
    else:
        run_supervisor()
