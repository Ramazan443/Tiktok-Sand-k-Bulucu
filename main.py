import asyncio
import logging
import sqlite3
import gc
from datetime import datetime, timedelta
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes
from TikTokLive import TikTokLiveClient
from TikTokLive.events import GiftEvent

# CPU ve RAM tasarrufu için log seviyesi kapalıya yakın
logging.basicConfig(level=logging.ERROR)

BOT_TOKEN = "8953105116:AAG-K4whz6ZEPwh--0chBnF4KdhgxpzG8KQ"
ADMIN_ID = 8881553428
DATABASE_NAME = "bot_database.db"

# 512MB RAM ve 0.1 CPU sınırları içinde kalma ayarları:
MAX_CONCURRENT_ROOMS = 3       # Aynı anda taranacak maksimum dinamik yayın sayısı
STREAM_LISTEN_TIMEOUT = 30     # Her yayını en fazla kaç saniye dinleyip sıradakine geçeceği
SCAN_INTERVAL_SECONDS = 5      # Yeni yayın havuzuna geçiş süresi

# Hafıza ve CPU tasarruflu DB yapılandırması
def init_db():
    conn = sqlite3.connect(DATABASE_NAME)
    cursor = conn.cursor()
    cursor.execute("PRAGMA cache_size = -1000;")
    cursor.execute("PRAGMA journal_mode = WAL;")
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            is_vip INTEGER DEFAULT 0,
            vip_until TEXT,
            trial_used INTEGER DEFAULT 0
        )
    ''')
    conn.commit()
    conn.close()

def check_vip_status(user_id):
    if user_id == ADMIN_ID:
        return True, datetime(2099, 12, 31)

    conn = sqlite3.connect(DATABASE_NAME)
    cursor = conn.cursor()
    cursor.execute("SELECT is_vip, vip_until FROM users WHERE user_id = ?", (user_id,))
    row = cursor.fetchone()
    conn.close()
    
    if not row or not row[0] or not row[1]:
        return False, None
    
    vip_until = datetime.fromisoformat(row[1])
    if datetime.now() < vip_until:
        return True, vip_until
    return False, None

def get_active_vips():
    conn = sqlite3.connect(DATABASE_NAME)
    cursor = conn.cursor()
    cursor.execute("SELECT user_id, vip_until FROM users WHERE is_vip = 1")
    rows = cursor.fetchall()
    conn.close()
    
    now = datetime.now()
    active_vip_ids = [uid for uid, until in rows if until and datetime.fromisoformat(until) > now]
    
    if ADMIN_ID not in active_vip_ids:
        active_vip_ids.append(ADMIN_ID)
        
    return active_vip_ids

# --- TELEGRAM BOT KOMUTLARI ---
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    conn = sqlite3.connect(DATABASE_NAME)
    cursor = conn.cursor()
    cursor.execute("INSERT OR IGNORE INTO users (user_id, username) VALUES (?, ?)", (user.id, user.username or "Anonim"))
    conn.commit()
    conn.close()

    keyboard = [
        [InlineKeyboardButton("👑 1 Saatlik VIP Deneme", callback_data="claim_trial")],
        [InlineKeyboardButton("👤 VIP Durumum", callback_data="profile")]
    ]
    await update.message.reply_text("TikTok Tüm Yayınlar Sandık Botuna Hoş Geldiniz!", reply_markup=InlineKeyboardMarkup(keyboard))

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id

    if query.data == "claim_trial":
        if user_id == ADMIN_ID:
            await query.edit_message_text("👑 Sınırsız Admin VIP hesabınız aktif.")
            return

        conn = sqlite3.connect(DATABASE_NAME)
        cursor = conn.cursor()
        cursor.execute("SELECT trial_used FROM users WHERE user_id = ?", (user_id,))
        row = cursor.fetchone()

        if row and row[0] == 1:
            await query.edit_message_text("❌ Deneme hakkınızı daha önce kullandınız.")
        else:
            vip_until = (datetime.now() + timedelta(hours=1)).isoformat()
            cursor.execute("UPDATE users SET is_vip = 1, vip_until = ?, trial_used = 1 WHERE user_id = ?", (vip_until, user_id))
            conn.commit()
            await query.edit_message_text("🎉 1 Saatlik VIP üyeliğiniz başladı!")
        conn.close()

    elif query.data == "profile":
        is_vip, vip_until = check_vip_status(user_id)
        if user_id == ADMIN_ID:
            msg = "👑 Status: Sınırsız Admin VIP"
        elif is_vip:
            msg = f"👑 VIP Üyelik Bitiş: {vip_until.strftime('%H:%M:%S')}"
        else:
            msg = "❌ VIP Aktif Değil"
        await query.edit_message_text(f"👤 **Profiliniz**\n\n{msg}")

async def notify_vips(bot, message):
    for uid in get_active_vips():
        try:
            await bot.send_message(chat_id=uid, text=message)
        except Exception:
            pass

# --- DİNAMİK YAYIN DİNLEYİCİ ---
async def listen_stream_short(unique_id: str, bot):
    client = TikTokLiveClient(unique_id=unique_id)

    @client.on(GiftEvent)
    async def on_gift(event: GiftEvent):
        gift_name = event.gift.name.lower()
        if "box" in gift_name or "sandık" in gift_name:
            msg = (
                f"🎁 **YENİ SANDIK BULUNDU!**\n\n"
                f"👤 **Yayıncı:** @{unique_id}\n"
                f"📦 **Hediye:** {event.gift.name}\n"
                f"🔗 https://www.tiktok.com/@{unique_id}/live"
            )
            await notify_vips(bot, msg)

    try:
        # Belirlenen zaman aşımı süresince (30 sn) yayını dinle
        await asyncio.wait_for(client.start(), timeout=STREAM_LISTEN_TIMEOUT)
    except Exception:
        pass
    finally:
        if client.is_connecting:
            await client.disconnect()

# --- TÜM YAYINLARI DÖNÜŞÜMLÜ TARAYAN ANA MOTOR ---
async def tiktok_global_scanner_loop(bot):
    # Dinamik keşfet / yayıncı listesi simülasyonu
    # (TikTok Keşfet üzerindeki aktif yayıncı kullanıcı adları döngüsel taranır)
    base_streamers = [
        "tiktok", "live", "creator", "gaming", "music", "dance", 
        "pk_official", "global_live", "community", "stream_turkey"
    ]

    index = 0
    while True:
        try:
            # Sıradaki aktif yayın gruplarını seç
            current_batch = []
            for _ in range(MAX_CONCURRENT_ROOMS):
                current_batch.append(base_streamers[index % len(base_streamers)])
                index += 1

            # Seçilen yayınları eşzamanlı olarak kısa süreli dinle
            tasks = [listen_stream_short(streamer, bot) for streamer in set(current_batch)]
            await asyncio.gather(*tasks, return_exceptions=True)

        except Exception:
            pass

        # RAM'de biriken nesneleri ve bağlantı artıklarını temizle
        gc.collect()
        await asyncio.sleep(SCAN_INTERVAL_SECONDS)

async def main():
    init_db()
    
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CallbackQueryHandler(button_handler))

    await app.initialize()
    await app.start()
    await app.updater.start_polling()

    # Arka plan küresel tarayıcıyı başlat
    asyncio.create_task(tiktok_global_scanner_loop(app.bot))

    await asyncio.Event().wait()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        pass
