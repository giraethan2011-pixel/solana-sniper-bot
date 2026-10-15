import time
import requests
import sqlite3
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler

# Dummy server to pass Render's port check
class SimpleHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-type", "text/plain")
        self.end_headers()
        self.wfile.write(b"Bot is active")

def run_server():
    server = HTTPServer(('0.0.0.0', 10000), SimpleHandler)
    server.serve_forever()

threading.Thread(target=run_server, daemon=True).start()

DB_PATH = "trading_signals.db"
DISCORD_WEBHOOK = "https://discord.com/api/webhooks/1560005057355849930/mZ9eZDL521L3rERi8izuiB_4hBxNNmUonV6LcMSUR62b-J5nK7V504xIsqEHh4cG53iR"

MIN_LIQ = 10000
MIN_VOL = 15000
MIN_M5_CHANGE = 2.0
MIN_M5_TXNS = 15

conn = sqlite3.connect(DB_PATH, check_same_thread=False)
cursor = conn.cursor()

cursor.execute("""CREATE TABLE IF NOT EXISTS signals (
    id INTEGER PRIMARY KEY AUTOINCREMENT, 
    timestamp DATETIME DEFAULT CURRENT_TIMESTAMP, 
    token_name TEXT, 
    symbol TEXT, 
    price REAL, 
    volume REAL, 
    liquidity REAL, 
    pair_address TEXT, 
    token_address TEXT
)""")

cursor.execute("""CREATE TABLE IF NOT EXISTS active_positions (
    token_address TEXT PRIMARY KEY,
    symbol TEXT,
    entry_price REAL,
    highest_price REAL,
    status TEXT DEFAULT 'ACTIVE'
)""")
conn.commit()

def send_discord(content):
    try:
        requests.post(DISCORD_WEBHOOK, json={"content": content})
    except Exception as e:
        print(f"Discord error: {e}")

def send_discord_embed(title, description, color, fields):
    try:
        embed = {"title": title, "description": description, "color": color, "fields": fields}
        requests.post(DISCORD_WEBHOOK, json={"embeds": [embed]})
    except Exception as e:
        print(f"Discord embed error: {e}")

print("[Axiom Sniper + Position Tracker] Initialized and Running...")

while True:
    try:
        url = "https://api.dexscreener.com/latest/dex/search/?q=solana"
        response = requests.get(url, timeout=10)
        data = response.json()
        pairs = data.get("pairs", [])

        for pair in pairs:
            if pair.get("chainId") != "solana":
                continue

            liq = pair.get("liquidity", {}).get("usd", 0) or 0
            vol = pair.get("volume", {}).get("h24", 0) or 0
            price_change = pair.get("priceChange", {})
            m5 = price_change.get("m5", 0) or 0
            
            txns = pair.get("txns", {}).get("m5", {})
            total_txns = txns.get("buys", 0) + txns.get("sells", 0)
            
            base_token = pair.get("baseToken", {})
            sym = base_token.get("symbol", "UNKNOWN")
            name = base_token.get("name", "Unknown Token")
            mint = base_token.get("address", "")
            price = float(pair.get("priceUsd", 0) or 0)

            if not mint or price <= 0:
                continue

            cursor.execute("SELECT token_address FROM active_positions WHERE token_address = ?", (mint,))
            if cursor.fetchone():
                continue

            if liq >= MIN_LIQ and vol >= MIN_VOL and m5 >= MIN_M5_CHANGE and total_txns >= MIN_M5_TXNS:
                rc_url = f"https://api.rugcheck.xyz/v1/tokens/{mint}/report"
                rc_resp = requests.get(rc_url, timeout=5)
                is_safe = True
                if rc_resp.status_code == 200:
                    rc_data = rc_resp.json()
                    if rc_data.get("score", 0) > 200:
                        is_safe = False

                if is_safe:
                    cursor.execute(
                        "INSERT INTO signals (token_name, symbol, price, volume, liquidity, pair_address, token_address) VALUES (?, ?, ?, ?, ?, ?, ?)",
                        (name, sym, price, vol, liq, pair.get("pairUrl", ""), mint)
                    )
                    cursor.execute(
                        "INSERT OR IGNORE INTO active_positions (token_address, symbol, entry_price, highest_price) VALUES (?, ?, ?, ?)",
                        (mint, sym, price, price)
                    )
                    conn.commit()

                    axiom_link = f"https://axiom.trade/token/{mint}"
                    fields = [
                        {"name": "Name", "value": f"{name} (${sym})", "inline": True},
                        {"name": "5M Surge", "value": f"+{m5:.2f}%", "inline": True},
                        {"name": "Entry Price", "value": f"${price:.8f}", "inline": True},
                        {"name": "Liquidity", "value": f"${liq:,.2f}", "inline": True},
                        {"name": "Action", "value": f"[BUY NOW ON AXIOM]({axiom_link})", "inline": False}
                    ]
                    send_discord_embed("⚡ AXIOM SIGNAL: $" + sym, f"Contract: `{mint}`", 3066993, fields)
                    print(f"AXIOM READY: {sym} | Liq: ${liq:,.0f} | 5M: {m5:+.2f}%")

        cursor.execute("SELECT token_address, symbol, entry_price, highest_price FROM active_positions WHERE status = 'ACTIVE'")
        active_trades = cursor.fetchall()

        for trade in active_trades:
            mint, sym, entry, highest = trade
            p_url = f"https://api.dexscreener.com/latest/dex/tokens/{mint}"
            p_resp = requests.get(p_url, timeout=5)
            if p_resp.status_code != 200:
                continue
            p_data = p_resp.json()
            p_pairs = p_data.get("pairs", [])
            if not p_pairs:
                continue
            
            current_price = float(p_pairs[0].get("priceUsd", 0) or 0)
            if current_price <= 0:
                continue

            pnl_pct = ((current_price - entry) / entry) * 100

            if current_price > highest:
                highest = current_price
                cursor.execute("UPDATE active_positions SET highest_price = ? WHERE token_address = ?", (highest, mint))
                conn.commit()

            if pnl_pct >= 50.0:
                send_discord(f"🎯 **TAKE PROFIT HIT!**\nToken: **${sym}** hit **+{pnl_pct:.1f}%** profit! Time to secure gains on Axiom: https://axiom.trade/token/{mint}")
                cursor.execute("UPDATE active_positions SET status = 'CLOSED_TP' WHERE token_address = ?", (mint,))
                conn.commit()
            elif pnl_pct <= -15.0:
                send_discord(f"🛑 **STOP LOSS HIT!**\nToken: **${sym}** dropped to **{pnl_pct:.1f}%**. Cutting losses.")
                cursor.execute("UPDATE active_positions SET status = 'CLOSED_SL' WHERE token_address = ?", (mint,))
                conn.commit()

    except Exception as e:
        print(f"Loop error: {e}")

    print("[Axiom Sniper] Scanning & Tracking...")
    time.sleep(15)
