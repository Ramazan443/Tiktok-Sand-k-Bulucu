import sqlite3
import asyncio
from datetime import datetime, timedelta
import aiohttp
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes
from TikTokLive import TikTokLiveClient
from TikTokLive.types.events import EnvelopeEvent

# --- AYARLAR ---
BOT_TOKEN = "8953105116:AAG-K4whz6ZEPwh--0chBnF4KdhgxpzG8KQ"
ADMIN_ID = 8881553428
MAX_CONCURRENT_ROOMS = 50  # Aynı anda taranacak canlı yayın sayısı

# Taranan yayınların tekrar tekrar eklenmemesi için set
monitored_users = set()

# --- VERİTABANI KURULUMU ---
def init_db():
    conn = sqlite3.connect("bot_database.db")
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            balance INTEGER DEFAULT 0,
            is_vip INTEGER DEFAULT 0,
            vip_until TIMESTAMP,
            last_free_vip TIMESTAMP
        )
    ''')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS chests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            chest_name TEXT,
            purchased_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    conn.commit()
    conn.close()

init_db()

def check_vip_status(user_id):
    conn = sqlite3.connect("bot_database.db")
    cursor = conn.cursor()
    cursor.execute("SELECT is_vip, vip_until FROM users WHERE user_id = ?", (user_id,))
    row = cursor.fetchone()
    
    if row and row[0] == 1 and row[1]:
        vip_until = datetime.strptime(row[1], "%Y-%m-%d %H:%M:%S")
        if datetime.now() > vip_until:
            cursor.execute("UPDATE users SET is_vip = 0, vip_until = NULL WHERE user_id = ?", (user_id,))
            conn.commit()
            conn.close()
            return False, None
        conn.close()
        return True, vip_until
    conn.close()
    return False, None

# --- TELEGRAM BOTU ARAYÜZÜ ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_data = update.effective_user
    
    conn = sqlite3.connect("bot_database.db")
    cursor = conn.cursor()
    cursor.execute("INSERT OR IGNORE INTO users (user_id, username) VALUES (?, ?)", (user_data.id, user_data.username or "Bilinmiyor"))
    conn.commit()
    conn.close()
    
    keyboard = [
        [InlineKeyboardButton("🎁 Günlük 1 Saat Ücretsiz VIP Al", callback_data="claim_free_vip")],
        [InlineKeyboardButton("📦 Sandıklarım", callback_data="view_chests"), InlineKeyboardButton("🛒 Bakiye/Paket Al", callback_data="buy_menu")],
        [InlineKeyboardButton("👤 Profilim", callback_data="profile")]
    ]
    
    await update.message.reply_text(
        f"👋 Merhaba {user_data.first_name}!\n\n"
        f"🤖 **Otomatik TikTok Hazine Kutusi Avcısı** aktif.\n"
        f"Sistem arka planda yayınları gezerek sandık arar ve VIP üyelere bildirim atar.",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="Markdown"
    )

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    
    conn = sqlite3.connect("bot_database.db")
    cursor = conn.cursor()
    
    if query.data == "claim_free_vip":
        cursor.execute("SELECT last_free_vip FROM users WHERE user_id = ?", (user_id,))
        row = cursor.fetchone()
        last_used = row[0] if row else None
        
        can_claim = True
        if last_used:
            last_date = datetime.strptime(last_used, "%Y-%m-%d %H:%M:%S")
            if datetime.now() - last_date < timedelta(days=1):
                can_claim = False
                await query.edit_message_text("❌ bugünlük 1 saatlik ücretsiz VIP hakkınızı zaten kullandınız!")
                
        if can_claim:
            now = datetime.now()
            vip_expire = now + timedelta(hours=1)
            cursor.execute('''
                UPDATE users SET is_vip = 1, vip_until = ?, last_free_vip = ? WHERE user_id = ?
            ''', (vip_expire.strftime("%Y-%m-%d %H:%M:%S"), now.strftime("%Y-%m-%d %H:%M:%S"), user_id))
            conn.commit()
            await query.edit_message_text(f"🎉 **1 SAATLİK ÜCRETSİZ VIP AKTİF!**\n\nBitiş: `{vip_expire.strftime('%H:%M:%S')}`\nSistem yayınları gezdikçe bulduğu sandıkları size iletecek.")

    elif query.data == "profile":
        is_vip, vip_until = check_vip_status(user_id)
        cursor.execute("SELECT balance FROM users WHERE user_id = ?", (user_id,))
        balance = cursor.fetchone()[0]
        status = f"👑 VIP Üye (Bitiş: {vip_until.strftime('%H:%M:%S')})" if is_vip else "👤 Standart Üye"
        await query.edit_message_text(f"👤 **PROFİL**\n\n🆔 ID: `{user_id}`\n⭐ Durum: {status}\n💰 Bakiye: {balance} Kredi", parse_mode="Markdown")

    conn.close()

# --- VIP ÜYELERE BİLDİRİM GÖNDERİCİ ---
async def notify_vip_users(app: Application, username: str, coins: int):
    live_url = f"https://www.tiktok.com/@{username}/live"
    msg = (
        f"🎁 **YENİ HAZİNE KUTUSU BULUNDU!**\n\n"
        f"👤 **Yayıncı:** @{username}\n"
        f"🪙 **Miktar:** {coins} Jeton\n"
        f"🔗 [Tıkla ve Yayına Git]({live_url})"
    )
    
    conn = sqlite3.connect("bot_database.db")
    cursor = conn.cursor()
    cursor.execute("SELECT user_id FROM users WHERE is_vip = 1")
    vip_users = cursor.fetchall()
    conn.close()

    for user in vip_users:
        is_active, _ = check_vip_status(user[0])
        if is_active:
            try:
                await app.bot.send_message(chat_id=user[0], text=msg, parse_mode="Markdown")
            except Exception:
                pass

# --- YAYIN TARAMA MOTORU (SCANNER) ---
async def scan_single_stream(app: Application, username: str):
    if username in monitored_users:
        return
    monitored_users.add(username)
    
    client = TikTokLiveClient(unique_id=username)

    @client.on("envelope")
    async def on_envelope(event: EnvelopeEvent):
        await notify_vip_users(app, username, event.treasure_box_coins)

    try:
        # Yayına bağlan ve maksimum 5 dakika boyunca kutu var mı dinle
        await asyncio.wait_for(client.start(), timeout=300)
    except Exception:
        pass
    finally:
        monitored_users.remove(username)

# --- CANLI YAYINLARI KEŞFETTEN OTOMATİK TOPLAYAN DÖNGÜ ---
async def fetch_and_scan_lives(app: Application):
    """TikTok üzerindeki aktif yayıncıları sürekli çekip taramaya atar"""
    async with aiohttp.ClientSession() as session:
        while True:
            try:
                # TikTok Canlı Yayın Öneri Servisi API Endpoint
                url = "https://www.tiktok.com/api/live/user/room/collect/"
                async with session.get(url, headers={"User-Agent": "Mozilla/5.0"}) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        # Örnek listeden yayıncı adlarını çek
                        rooms = data.get("data", [])
                        for room in rooms:
                            username = room.get("owner", {}).get("display_id")
                            if username and len(monitored_users) < MAX_CONCURRENT_ROOMS:
                                asyncio.create_task(scan_single_stream(app, username))
            except Exception:
                pass
            
            # 15 saniyede bir yeni canlı yayın havuzunu güncelle
            await asyncio.sleep(15)

# --- ANA BAŞLATICI ---
def main():
    app = Application.builder().token(BOT_TOKEN).build()
    
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(button_handler))
    
    # Arka planda otomatik yayın keşfet motorunu çalıştır
    loop = asyncio.get_event_loop()
    loop.create_task(fetch_and_scan_lives(app))
    
    print("Bot ve Otomatik Keşfet Taraması Aktif!")
    app.run_polling()

if __name__ == "__main__":
    main()
