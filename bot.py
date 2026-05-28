"""
Discord Options Trade Alert Bot
================================
Usage:
  /trade BTO IBM 280C 06/05/2026 $2.50
  /trade BTO IBM 280P 06/05/2026 $1.20 note:earnings play
  /trade STC IBM 280C $3.10
  /trade STC IBM 280C $3.10 qty:5  (partial/trim)
  /undo
  /closeall
  /clear

Setup:
  1. pip install discord.py python-dotenv
  2. Create a .env file with:
       DISCORD_TOKEN=your_token_here
       ALERT_CHANNEL_ID=your_channel_id_here
  3. python bot.py
"""

import discord
from discord import app_commands
from discord.ext import commands
import re
import json
import os
from datetime import datetime
from dotenv import load_dotenv

load_dotenv()

ALERT_CHANNEL_ID = int(os.getenv("ALERT_CHANNEL_ID", 0))
TRADES_FILE      = "trades.json"
HISTORY_FILE     = "history.json"   # stores message IDs for /undo

intents = discord.Intents.default()
bot = commands.Bot(command_prefix="!", intents=intents)


# ── Storage helpers ───────────────────────────────────────────────────────────

def load_trades():
    if os.path.exists(TRADES_FILE):
        with open(TRADES_FILE) as f:
            return json.load(f)
    return {}

def save_trades(trades):
    with open(TRADES_FILE, "w") as f:
        json.dump(trades, f, indent=2)

def load_history():
    if os.path.exists(HISTORY_FILE):
        with open(HISTORY_FILE) as f:
            return json.load(f)
    return []

def save_history(history):
    with open(HISTORY_FILE, "w") as f:
        json.dump(history, f, indent=2)

def push_history(channel_id, message_id, trade_key_str, trade_data):
    """Save a posted alert so /undo can reverse it."""
    history = load_history()
    history.append({
        "channel_id":  channel_id,
        "message_id":  message_id,
        "trade_key":   trade_key_str,
        "trade_data":  trade_data,        # None for STC entries
        "timestamp":   datetime.now().isoformat()
    })
    save_history(history)

def trade_key(ticker, contract):
    return f"{ticker.upper()}_{contract.upper()}"


# ── Parsing helpers ───────────────────────────────────────────────────────────

def parse_contract(contract_str):
    m = re.match(r"^(\d+(?:\.\d+)?)(C|P)$", contract_str.upper())
    if not m:
        return None, None
    return m.group(1), m.group(2)

def parse_price(price_str):
    return float(price_str.replace("$", "").replace(",", ""))

def pnl_emoji(pct):
    if pct >= 50:  return "🔥"
    if pct >= 20:  return "✅"
    if pct >= 0:   return "📈"
    if pct >= -25: return "📉"
    return "🛑"


# ── Embeds ────────────────────────────────────────────────────────────────────

def build_bto_embed(ticker, strike, direction, expiry, entry, qty, note, author):
    color = 0x1D9E75 if direction == "C" else 0xE24B4A
    label = "CALL" if direction == "C" else "PUT"
    icon  = "📈" if direction == "C" else "📉"

    embed = discord.Embed(
        title=f"{icon}  BTO — {ticker} ${strike} {label}",
        color=color,
        timestamp=datetime.utcnow()
    )
    embed.add_field(name="Expiry",  value=expiry,           inline=True)
    embed.add_field(name="Entry",   value=f"${entry:.2f}",  inline=True)
    if qty:
        embed.add_field(name="Qty", value=str(qty),         inline=True)
    if note:
        embed.add_field(name="📌 Thesis", value=note,       inline=False)
    embed.set_footer(text=f"Alert by {author}")
    return embed

def build_stc_embed(ticker, strike, direction, exit_price, qty, open_trade, author):
    entry = open_trade.get("entry") if open_trade else None
    pct   = ((exit_price - entry) / entry * 100) if entry else None
    color = 0x1D9E75 if direction == "C" else 0xE24B4A
    label = "CALL" if direction == "C" else "PUT"
    trim  = qty and open_trade and qty < open_trade.get("qty", qty)
    action = "TRIM" if trim else "STC"

    title = f"{'✂️' if trim else '🏁'}  {action} — {ticker} ${strike} {label}"
    embed = discord.Embed(title=title, color=color, timestamp=datetime.utcnow())
    embed.add_field(name="Exit",  value=f"${exit_price:.2f}", inline=True)
    if entry:
        embed.add_field(name="Entry", value=f"${entry:.2f}",  inline=True)
    if pct is not None:
        sign = "+" if pct >= 0 else ""
        embed.add_field(
            name="P&L",
            value=f"{sign}{pct:.1f}% {pnl_emoji(pct)}",
            inline=True
        )
    if qty:
        embed.add_field(name="Qty closed", value=str(qty),    inline=True)
    if open_trade and open_trade.get("timestamp"):
        embed.add_field(name="Opened", value=open_trade["timestamp"], inline=True)
    embed.set_footer(text=f"Alert by {author}")
    return embed


# ── /trade ────────────────────────────────────────────────────────────────────

@bot.tree.command(name="trade", description="Post a trade alert. e.g. BTO IBM 280C 06/05/2026 $2.50")
@app_commands.describe(alert="Full trade string, e.g.  BTO IBM 280C 06/05/2026 $2.50  or  STC IBM 280C $3.10")
async def trade(interaction: discord.Interaction, alert: str):
    parts  = alert.strip().split()
    action = parts[0].upper() if parts else ""

    if action not in ("BTO", "STC"):
        await interaction.response.send_message(
            "❌ Start with **BTO** or **STC**.\n"
            "Examples:\n"
            "`BTO IBM 280C 06/05/2026 $2.50`\n"
            "`STC IBM 280C $3.10`",
            ephemeral=True
        )
        return

    kwargs      = {}
    clean_parts = []
    for p in parts[1:]:
        if ":" in p:
            k, v = p.split(":", 1)
            kwargs[k.lower()] = v
        else:
            clean_parts.append(p)

    # ── BTO ───────────────────────────────────────────────────────────────────
    if action == "BTO":
        if len(clean_parts) < 4:
            await interaction.response.send_message(
                "❌ BTO needs: `ticker contract expiry price`\n"
                "Example: `BTO IBM 280C 06/05/2026 $2.50`",
                ephemeral=True
            )
            return

        ticker, contract_str, expiry, price_str = clean_parts[:4]
        strike, direction = parse_contract(contract_str)
        if not strike:
            await interaction.response.send_message(
                "❌ Contract format should be like `280C` or `280P`.", ephemeral=True
            )
            return

        try:
            entry = parse_price(price_str)
        except ValueError:
            await interaction.response.send_message("❌ Couldn't parse price.", ephemeral=True)
            return

        qty  = int(kwargs["qty"]) if "qty" in kwargs else None
        note = kwargs.get("note", "").replace("_", " ")

        embed = build_bto_embed(ticker, strike, direction, expiry, entry, qty, note,
                                interaction.user.display_name)

        trades = load_trades()
        key    = trade_key(ticker, contract_str)
        trade_data = {
            "ticker":    ticker.upper(),
            "strike":    strike,
            "direction": direction,
            "expiry":    expiry,
            "entry":     entry,
            "qty":       qty,
            "timestamp": datetime.now().strftime("%b %d, %Y %I:%M %p")
        }
        trades[key] = trade_data
        save_trades(trades)

        channel = bot.get_channel(ALERT_CHANNEL_ID) or interaction.channel
        msg = await channel.send(embed=embed)
        push_history(channel.id, msg.id, key, trade_data)

    # ── STC ───────────────────────────────────────────────────────────────────
    elif action == "STC":
        if len(clean_parts) < 3:
            await interaction.response.send_message(
                "❌ STC needs: `ticker contract price`\n"
                "Example: `STC IBM 280C $3.10`",
                ephemeral=True
            )
            return

        ticker, contract_str, price_str = clean_parts[:3]
        strike, direction = parse_contract(contract_str)
        if not strike:
            await interaction.response.send_message(
                "❌ Contract format should be like `280C` or `280P`.", ephemeral=True
            )
            return

        try:
            exit_price = parse_price(price_str)
        except ValueError:
            await interaction.response.send_message("❌ Couldn't parse price.", ephemeral=True)
            return

        qty = int(kwargs["qty"]) if "qty" in kwargs else None

        trades     = load_trades()
        key        = trade_key(ticker, contract_str)
        open_trade = trades.get(key)

        embed = build_stc_embed(ticker, strike, direction, exit_price, qty, open_trade,
                                interaction.user.display_name)

        prev_trade_data = open_trade.copy() if open_trade else None

        if open_trade and not qty:
            del trades[key]
            save_trades(trades)
        elif open_trade and qty and open_trade.get("qty"):
            remaining = open_trade["qty"] - qty
            if remaining <= 0:
                del trades[key]
            else:
                trades[key]["qty"] = remaining
            save_trades(trades)

        channel = bot.get_channel(ALERT_CHANNEL_ID) or interaction.channel
        msg = await channel.send(embed=embed)
        push_history(channel.id, msg.id, key, prev_trade_data)

    await interaction.response.send_message("✅ Alert posted!", ephemeral=True)


# ── /positions ────────────────────────────────────────────────────────────────

@bot.tree.command(name="positions", description="Show all currently open positions")
async def positions(interaction: discord.Interaction):
    trades = load_trades()
    if not trades:
        await interaction.response.send_message("No open positions.", ephemeral=True)
        return

    embed = discord.Embed(title="📋  Open Positions", color=0x7F77DD,
                          timestamp=datetime.utcnow())
    for key, t in trades.items():
        label = "CALL" if t["direction"] == "C" else "PUT"
        val   = f"Entry: ${t['entry']:.2f}  |  Exp: {t['expiry']}"
        if t.get("qty"):
            val += f"  |  Qty: {t['qty']}"
        embed.add_field(
            name=f"{t['ticker']} ${t['strike']} {label}",
            value=val,
            inline=False
        )
    await interaction.response.send_message(embed=embed, ephemeral=True)


# ── /undo ─────────────────────────────────────────────────────────────────────

@bot.tree.command(name="undo", description="Delete the last trade alert the bot posted")
async def undo(interaction: discord.Interaction):
    history = load_history()
    if not history:
        await interaction.response.send_message("Nothing to undo.", ephemeral=True)
        return

    last    = history.pop()
    channel = bot.get_channel(last["channel_id"])

    # Delete the Discord message
    try:
        msg = await channel.fetch_message(last["message_id"])
        await msg.delete()
    except discord.NotFound:
        pass  # message already gone, that's fine

    # Restore trade state: if it was a BTO, remove from trades
    #                       if it was a STC, restore the open position
    trades = load_trades()
    key    = last["trade_key"]

    if last["trade_data"] is not None:
        # Was a BTO — remove it
        trades.pop(key, None)
    # If trade_data is None it was a STC with no prior open trade, nothing to restore

    save_trades(trades)
    save_history(history)

    await interaction.response.send_message("↩️ Last alert undone.", ephemeral=True)


# ── /closeall ─────────────────────────────────────────────────────────────────

@bot.tree.command(name="closeall", description="Wipe all open positions (testing/reset only)")
async def closeall(interaction: discord.Interaction):
    trades = load_trades()
    count  = len(trades)
    if not count:
        await interaction.response.send_message("No open positions to close.", ephemeral=True)
        return

    save_trades({})
    await interaction.response.send_message(
        f"🗑️ Cleared {count} open position{'s' if count != 1 else ''} from tracking.",
        ephemeral=True
    )


# ── /clear ────────────────────────────────────────────────────────────────────

@bot.tree.command(name="clear", description="Wipe all bot trade history and open positions")
async def clear(interaction: discord.Interaction):
    save_trades({})
    save_history([])
    await interaction.response.send_message(
        "🧹 All trade history and open positions cleared.", ephemeral=True
    )


# ── Startup ───────────────────────────────────────────────────────────────────

@bot.event
async def on_ready():
    await bot.tree.sync()
    print(f"✅ Logged in as {bot.user} — slash commands synced")

bot.run(os.getenv("DISCORD_TOKEN"))

# ── Sync ───────────────────────────────────────────────────────────────────

@bot.tree.command(name="sync", description="Force sync slash commands")
async def sync(interaction: discord.Interaction):
    await bot.tree.sync()
    await interaction.response.send_message("✅ Synced!", ephemeral=True)
