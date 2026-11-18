import asyncio
import json
import logging
import httpx

# Настройка логирования
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

# Загружаем конфигурацию из tok.json
try:
    with open("tok.json", "r", encoding="utf-8") as f:
        config = json.load(f)
except Exception as e:
    logging.error(f"Не удалось загрузить tok.json: {e}")
    exit(1)

TG_BOT_TOKEN = config.get("tg_bot_token")
TG_CHAT_ID = config.get("tg_chat_id")

# Превращаем массив кук из Cookie-Editor в словарь для httpx
raw_cookies = config.get("playerok_cookies", [])
COOKIES = {c["name"]: c["value"] for c in raw_cookies}

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Linux; Android 10) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Mobile Safari/537.36",
    "Referer": "https://playerok.com/",
    "Accept": "application/json",
}

# Функция отправки уведомлений в Telegram
async def send_telegram_notification(text: str):
    url = f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TG_CHAT_ID,
        "text": text,
        "parse_mode": "HTML"
    }
    async with httpx.AsyncClient() as client:
        try:
            await client.post(url, json=payload, timeout=10.0)
        except Exception as e:
            logging.error(f"Ошибка отправки в Telegram: {e}")

# Основной цикл проверки Плейрока
async def check_playerok_loop():
    logging.info("Бот мониторинга Playerok запущен...")
    
    # Множества для хранения уже обработанных ID (чтобы не спамить повторно)
    seen_orders = set()
    seen_messages = set()

    async with httpx.AsyncClient(cookies=COOKIES, headers=HEADERS, timeout=15.0) as client:
        while True:
            try:
                # 1. Проверка заказов (пример эндпоинта)
                orders_resp = await client.get("https://playerok.com/api/v1/orders") # Замени на актуальный эндпоинт при необходимости
                if orders_resp.status_code == 200:
                    data = orders_resp.json()
                    # Предполагаем, что сервер возвращает список заказов в каком-то ключе, например data['items'] или data
                    orders = data if isinstance(data, list) else data.get("items", [])
                    
                    for order in orders:
                        order_id = str(order.get("id"))
                        if order_id and order_id not in seen_orders:
                            seen_orders.add(order_id)
                            # Первое появление в памяти не триггерит уведомление старых заказов
                            if len(seen_orders) > 1: 
                                text = f"🚨 <b>Новый заказ на Playerok!</b>\n\nID: {order_id}\nСтатус: {order.get('status', 'Неизвестно')}"
                                await send_telegram_notification(text)

                # 2. Проверка чатов/сообщений (пример эндпоинта)
                chats_resp = await client.get("https://playerok.com/api/v1/chat/chats")
                if chats_resp.status_code == 200:
                    chats_data = chats_resp.json()
                    chats = chats_data if isinstance(chats_data, list) else chats_data.get("items", [])
                    
                    for chat in chats:
                        last_msg = chat.get("lastMessage", {})
                        msg_id = str(last_msg.get("id"))
                        if msg_id and msg_id not in seen_messages:
                            seen_messages.add(msg_id)
                            if len(seen_messages) > 1:
                                sender = last_msg.get("senderName", "Покупатель")
                                msg_text = last_msg.get("text", "Без текста")
                                text = f"💬 <b>Новое сообщение в чате!</b>\n\n<b>{sender}:</b> {msg_text}"
                                await send_telegram_notification(text)

                elif chats_resp.status_code == 401:
                    logging.warning("Токен авторизации устарел (401 Unauthorized). Обнови куки в tok.json!")
                    await send_telegram_notification("⚠️ <b>Внимание!</b> Токен Playerok устарел, требуется обновить куки в tok.json.")
                    await asyncio.sleep(300) # Подождать 5 минут перед след. попыткой при ошибке авторизации

            except Exception as e:
                logging.error(f- "Ошибка при запросе к Playerok: {e}")

            # Интервал проверки (30 секунд) — оптимально, чтобы не получить бан
            await asyncio.sleep(30)

if __name__ == "__main__":
    asyncio.run(check_playerok_loop())
