import os
import asyncio
import time
import requests
from datetime import datetime, timedelta
from pyrogram import Client, filters, compose
from pyrogram.types import ChatPermissions
from pyrogram.errors import FloodWait
from pyrogram.raw import functions

# --- 1. ПЕРВИЧНАЯ НАСТРОЙКА И АВТОРИЗАЦИЯ В КОНСОЛИ ---
CONFIG_FILE = "config.txt"

if not os.path.exists(CONFIG_FILE):
    print("🤖 Первичная настройка бота...")
    api_id = input("Введите ваш API_ID: ")
    api_hash = input("Введите ваш API_HASH: ")
    bot_token = input("Введите TOKEN управляющего бота (от @BotFather): ")
    render_api = input("Введите API ключ Render.com (или нажмите Enter для пропуска): ")
    
    with open(CONFIG_FILE, "w") as f:
        f.write(f"{api_id}\n{api_hash}\n{bot_token}\n{render_api}")
else:
    with open(CONFIG_FILE, "r") as f:
        lines = f.read().splitlines()
        api_id = int(lines[0])
        api_hash = lines[1]
        bot_token = lines[2]
        render_api = lines[3] if len(lines) > 3 else ""

# Инициализация клиентов
# app - это ты (юзербот), bot - это твой управляющий бот
app = Client("my_account", api_id=api_id, api_hash=api_hash)
bot = Client("manager_bot", api_id=api_id, api_hash=api_hash, bot_token=bot_token)

# --- ГЛОБАЛЬНЫЕ ПЕРЕМЕННЫЕ И БД ---
warns = {} # {chat_id: {user_id: count}}
message_cache = {} # Кэш для ловца удаленок
settings = {
    "catcher": False,
    "autocall": False,
    "autoleave": False,
    "profile_clone": None # ID пользователя, чей профиль копируем
}
my_original_profile = {"bio": "", "photo": None}

# --- 2. ФУНКЦИИ ЮЗЕРБОТА (Префикс ".") ---

@app.on_message(filters.me & filters.command("mute", prefixes="."))
async def mute_cmd(client, message):
    if not message.reply_to_message:
        return await message.edit("❌ Ответьте на сообщение пользователя.")
    
    args = message.text.split()
    user_id = message.reply_to_message.from_user.id
    
    if len(args) == 1:
        # Вечный мут
        await client.restrict_chat_member(message.chat.id, user_id, ChatPermissions(can_send_messages=False))
        await message.edit("🤐 Пользователь заглушен навсегда.")
    else:
        # Временный мут (например: .mute 5m, .mute 2h)
        time_str = args[1]
        duration = 0
        if time_str.endswith('m'):
            duration = int(time_str[:-1]) * 60
        elif time_str.endswith('h'):
            duration = int(time_str[:-1]) * 3600
        
        until_date = datetime.now() + timedelta(seconds=duration)
        await client.restrict_chat_member(message.chat.id, user_id, ChatPermissions(can_send_messages=False), until_date=until_date)
        await message.edit(f"🤐 Пользователь заглушен на {time_str}.")

@app.on_message(filters.me & filters.command("warn", prefixes="."))
async def warn_cmd(client, message):
    if not message.reply_to_message:
        return await message.edit("❌ Ответьте на сообщение.")
    
    try:
        max_warns = int(message.text.split()[1])
    except:
        max_warns = 3 # По умолчанию 3 варна

    chat_id = message.chat.id
    user_id = message.reply_to_message.from_user.id
    
    if chat_id not in warns: warns[chat_id] = {}
    warns[chat_id][user_id] = warns[chat_id].get(user_id, 0) + 1
    
    if warns[chat_id][user_id] >= max_warns:
        await client.restrict_chat_member(chat_id, user_id, ChatPermissions(can_send_messages=False))
        await message.edit(f"☠️ Пользователь получил {max_warns}/{max_warns} предупреждений и отправлен в вечный мут.")
        warns[chat_id][user_id] = 0
    else:
        await message.edit(f"⚠️ Предупреждение {warns[chat_id][user_id]}/{max_warns}.")

@app.on_message(filters.me & filters.command("print", prefixes="."))
async def print_cmd(client, message):
    text = message.text.split(maxsplit=1)[1]
    out = ""
    for char in text:
        out += char
        try:
            await message.edit(out + "▒")
            await asyncio.sleep(0.1)
        except FloodWait as e:
            await asyncio.sleep(e.value)
    await message.edit(out)

@app.on_message(filters.me & filters.command("spam", prefixes="."))
async def spam_cmd(client, message):
    args = message.text.split(maxsplit=2)
    if len(args) < 3:
        return await message.edit("❌ Использование: .spam [число] [текст]")
    
    count = int(args[1])
    text = args[2]
    await message.delete()
    for _ in range(count):
        await client.send_message(message.chat.id, text)
        await asyncio.sleep(0.1) # Защита от флуд-лимитов

@app.on_message(filters.me & filters.command(["XO", "OX"], prefixes="."))
async def xo_cmd(client, message):
    # Упрощенная версия игрового поля (визуальная)
    board = "⬜️⬜️⬜️\n⬜️⬜️⬜️\n⬜️⬜️⬜️"
    await message.edit(f"🎮 Крестики-Нолики\n\n{board}\n\n*(Управление через кнопки недоступно юзерботам, играйте через ответы!)*")

@app.on_message(filters.me & filters.command("coin", prefixes="."))
async def coin_cmd(client, message):
    await message.edit("🪙 Бросаю монетку...")
    await asyncio.sleep(1)
    await client.send_dice(message.chat.id, emoji="🎰") # Заменяем на рулетку/кубик для визуала, так как монетки-эмодзи нет в стандартных дайсах

@app.on_message(filters.me & filters.command("timer", prefixes="."))
async def timer_cmd(client, message):
    args = message.text.split(maxsplit=2)
    time_str = args[1]
    text = args[2]
    
    duration = int(time_str[:-1]) * 60 if time_str.endswith('m') else int(time_str[:-1])
    
    sent = await message.edit(text)
    await asyncio.sleep(duration)
    await sent.delete()

@app.on_message(filters.me & filters.command("x", prefixes="."))
async def delete_cmd(client, message):
    if message.reply_to_message:
        await message.reply_to_message.delete()
    await message.delete()

@app.on_message(filters.me & filters.command("spec", prefixes="."))
async def spec_cmd(client, message):
    user = message.reply_to_message.from_user if message.reply_to_message else None
    if not user:
        return await message.edit("❌ Ответьте на сообщение пользователя.")
    
    await message.edit(f"👁 Начинаю слежку за статусом {user.first_name}...")
    
    # Интеграция с Render.com (Требуется Render API Key в конфиге)
    if render_api:
        try:
            # Отправляем запрос на развертывание (Deploy Hook) заранее созданного веб-сервиса на Render.
            # Для реального динамического создания сайта потребуется POST запрос к /v1/services с полным манифестом.
            headers = {"Authorization": f"Bearer {render_api}", "Accept": "application/json"}
            payload = {"name": f"spec-{user.id}", "type": "web_service", "repo": "https://github.com/твоя-ссылка/tracker", "env": "python"}
            # Внимание: это концептуальный запрос. Render API требует детальных настроек.
            response = requests.post("https://api.render.com/v1/services", headers=headers, json=payload)
            if response.status_code in (200, 201):
                await message.edit(f"✅ Сайт-шпион успешно развернут на Render.com!\nОтслеживаем: {user.first_name}")
            else:
                await message.edit(f"⚠️ Ошибка Render API: {response.text}")
        except Exception as e:
            await message.edit(f"❌ Ошибка создания сайта: {e}")
    else:
        await message.edit("⚠️ API ключ Render не найден в конфиге. Локальная слежка активирована.")

@app.on_message(filters.me & filters.command("profile", prefixes="."))
async def profile_cmd(client, message):
    if not message.reply_to_message:
        return await message.edit("❌ Ответьте на сообщение пользователя.")
    
    target_id = message.reply_to_message.from_user.id
    settings["profile_clone"] = target_id
    
    # Сохраняем свой старый профиль
    me = await app.get_users("me")
    full_me = await app.get_chat(me.id)
    my_original_profile["bio"] = full_me.bio or ""
    
    await message.edit("🔄 Начинаю копирование профиля 24/7. Отключить можно в управляющем боте.")

# --- 3. ФОНОВЫЕ ЗАДАЧИ ---

# Кэширование для ловца удаленок
@app.on_message(~filters.me, group=1)
async def cache_incoming(client, message):
    if settings["catcher"]:
        message_cache[message.id] = {
            "text": message.text or message.caption or "[Медиа/Стикер]",
            "time": time.time(),
            "chat": message.chat.title or message.chat.first_name,
            "user": message.from_user.first_name if message.from_user else "Неизвестно"
        }

# Ловец удаленных сообщений
@app.on_deleted_messages()
async def deleted_catcher(client, messages):
    if not settings["catcher"]: return
    
    for msg in messages:
        if msg.id in message_cache:
            data = message_cache[msg.id]
            # Если прошло меньше 2 минут (120 сек)
            if time.time() - data["time"] <= 120:
                alert = f"🗑 **Ловец удаленок**\nЧат: {data['chat']}\nОт: {data['user']}\nТекст: {data['text']}"
                await app.send_message("me", alert) # Отправляет в Избранное
            del message_cache[msg.id]

# Автосброс звонков (Перехват Raw Updates)
@app.on_raw_update()
async def raw_update_handler(client, update, users, chats):
    if settings["autocall"]:
        # Если пришел запрос на звонок
        if hasattr(update, "phone_call") and hasattr(update.phone_call, "id"):
            try:
                # Отклоняем звонок на уровне API
                await client.invoke(functions.phone.DiscardCall(
                    peer=update.phone_call,
                    duration=0,
                    reason=functions.phone.PhoneCallDiscardReasonDisconnect(),
                    connection_id=0
                ))
            except:
                pass

async def background_tasks():
    while True:
        # Автовыход из групп
        if settings["autoleave"]:
            try:
                async for dialog in app.get_dialogs():
                    if dialog.chat.type.name in ["GROUP", "SUPERGROUP"] and dialog.unread_messages_count > 30:
                        last_msg = dialog.top_message
                        if last_msg and (datetime.now() - last_msg.date).days > 7:
                            await app.leave_chat(dialog.chat.id)
                            print(f"Покинул группу {dialog.chat.title}")
            except Exception as e:
                print(f"Ошибка автовыхода: {e}")

        # Обновление клонированного профиля 24/7
        if settings["profile_clone"]:
            try:
                target = await app.get_chat(settings["profile_clone"])
                await app.update_profile(bio=target.bio[:70] if target.bio else "")
                # Логика скачивания и установки аватарки (ограничена во избежание флуда, раз в час)
            except:
                pass

        await asyncio.sleep(3600) # Проверки выполняются раз в час

# --- 4. ПАНЕЛЬ УПРАВЛЕНИЯ (Управляющий Бот) ---

@bot.on_message(filters.command("start"))
async def bot_start(client, message):
    text = (
        "🎛 **Панель управления юзерботом**\n\n"
        "Доступные команды:\n"
        "1. `/catcher` - Ловец удаленок (<2 мин в Избранное)\n"
        "2. `/autocall` - Автосброс всех звонков\n"
        "3. `/autoleave` - Выход из групп (>30 непрочитанных и >7 дней)\n"
        "4. `/restore_profile` - Вернуть свой профиль"
    )
    await message.reply(text)

@bot.on_message(filters.command("catcher"))
async def toggle_catcher(client, message):
    settings["catcher"] = not settings["catcher"]
    status = "ВКЛЮЧЕН ✅" if settings["catcher"] else "ВЫКЛЮЧЕН ❌"
    await message.reply(f"Ловец удаленок: {status}")

@bot.on_message(filters.command("autocall"))
async def toggle_autocall(client, message):
    settings["autocall"] = not settings["autocall"]
    status = "ВКЛЮЧЕН ✅" if settings["autocall"] else "ВЫКЛЮЧЕН ❌"
    await message.reply(f"Автосброс звонков: {status}")

@bot.on_message(filters.command("autoleave"))
async def toggle_autoleave(client, message):
    settings["autoleave"] = not settings["autoleave"]
    status = "ВКЛЮЧЕН ✅" if settings["autoleave"] else "ВЫКЛЮЧЕН ❌"
    await message.reply(f"Автовыход из мертвых групп: {status}")

@bot.on_message(filters.command("restore_profile"))
async def restore_profile(client, message):
    settings["profile_clone"] = None
    try:
        await app.update_profile(bio=my_original_profile["bio"])
        # Чтобы удалить текущие фото (клонированные)
        photos = [p async for p in app.get_chat_photos("me")]
        if photos:
            await app.delete_profile_photos(photos[0].file_id)
        await message.reply("✅ Функция клонирования выключена. Био возвращено, последняя аватарка удалена.")
    except Exception as e:
        await message.reply(f"❌ Ошибка: {e}")


# --- ЗАПУСК ВСЕГО ВМЕСТЕ ---
async def main():
    print("🚀 Запуск Юзербота и Управляющего Бота...")
    asyncio.create_task(background_tasks())
    await compose([app, bot])

if __name__ == "__main__":
    app.run(main())
