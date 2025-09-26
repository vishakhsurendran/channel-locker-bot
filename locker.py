import discord
from discord.ext import commands, tasks
import asyncio
import os
import json
import re
from dotenv import load_dotenv
from datetime import datetime, timedelta
from collections import defaultdict

# Setup   
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
LOG_CHANNEL_ID = int(os.getenv("LOG_CHANNEL_ID"))         # Lock/unlock logs
CATCH_LOG_CHANNEL_ID = int(os.getenv("CATCH_LOG_CHANNEL_ID"))  # Pokétwo catch logs

intents = discord.Intents.default()
intents.message_content = True
intents.reactions = True
intents.guilds = True
intents.members = True 
intents.presences = True

bot = commands.Bot(command_prefix="!", intents=intents)

# Config
PING_BOT_IDS = {
    854233015475109888,  # P2 Assistant
}
POKETWO_ID = 716390085896962058  # Pokétwo bot ID

# Track locked channels
locked_channels = {}  # {channel_id: {"unlock_time": datetime, "message_id": int}}

# Catch tracking / persistence
CATCH_DATA_FILE = "catch_data.json"
catch_counts = defaultdict(int)        # All-time counts {user_id: total}
daily_catch_counts = defaultdict(int)  # Today's counts {user_id: daily}
last_reset_date = None                 # Track last reset day
should_backfill = False                # Flag for conditional backfill

# Persistence helpers
def load_catch_data():
    """Load persisted catch data (all-time + daily + last reset date)."""
    global catch_counts, daily_catch_counts, last_reset_date, should_backfill
    try:
        with open(CATCH_DATA_FILE, "r", encoding="utf-8") as f:
            content = f.read().strip()
            if content:
                data = json.loads(content)
                catch_counts = defaultdict(int, {int(k): int(v) for k, v in data.get("all_time", {}).items()})
                daily_catch_counts = defaultdict(int, {int(k): int(v) for k, v in data.get("daily", {}).items()})
                last_reset_date = data.get("last_reset")
                should_backfill = False
            else:
                # if file exists but empty = mark for backfill
                catch_counts = defaultdict(int)
                daily_catch_counts = defaultdict(int)
                last_reset_date = None
                should_backfill = True
    except (FileNotFoundError, json.JSONDecodeError):
        catch_counts = defaultdict(int)
        daily_catch_counts = defaultdict(int)
        last_reset_date = None
        should_backfill = True

def save_catch_data():
    """Persist current catch data to disk."""
    data = {
        "all_time": {str(k): v for k, v in catch_counts.items()},
        "daily": {str(k): v for k, v in daily_catch_counts.items()},
        "last_reset": last_reset_date
    }
    with open(CATCH_DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)

# Logging helpers
async def log_action(guild: discord.Guild, message: str):
    log_channel = guild.get_channel(LOG_CHANNEL_ID)
    if log_channel:
        try:
            await log_channel.send(message)
        except Exception as e:
            print(f"[log_action] failed to send to log channel: {e}")

async def log_catch(guild: discord.Guild, message: str = None, embed: discord.Embed = None):
    catch_channel = guild.get_channel(CATCH_LOG_CHANNEL_ID)
    if catch_channel:
        try:
            await catch_channel.send(content=message, embed=embed)
        except Exception as e:
            print(f"[log_catch] failed to send to catch channel: {e}")


# Lock / unlock helpers
async def lock_channel(channel: discord.TextChannel, auto=False, reason="ping"):
    overwrite = channel.overwrites_for(channel.guild.default_role)
    overwrite.send_messages = False
    await channel.set_permissions(channel.guild.default_role, overwrite=overwrite)

    msg = await channel.send(
        f"🔒 {channel.mention} locked! React with 🔓 to unlock."
    )
    await msg.add_reaction("🔓")

    locked_channels[channel.id] = {
        "unlock_time": datetime.utcnow() + timedelta(hours=24),
        "message_id": msg.id
    }

    await log_action(channel.guild, f"🔒 {channel.mention} locked ({reason}).")

async def unlock_channel(channel: discord.TextChannel, reason="manual"):
    overwrite = channel.overwrites_for(channel.guild.default_role)
    overwrite.send_messages = None
    await channel.set_permissions(channel.guild.default_role, overwrite=overwrite)

    await channel.send(f"🔓 Channel unlocked! ({reason})")
    await log_action(channel.guild, f"🔓 {channel.mention} unlocked ({reason}).")

    locked_channels.pop(channel.id, None)

# Catch processing
async def resolve_mention_to_member(guild: discord.Guild, combined_text: str):
    # Try to resolve a mention or ID in the text to a Member object; returns as Member or None.
    id_match = re.search(r"<@!?(\d+)>", combined_text)
    if id_match:
        uid = int(id_match.group(1))
        member = guild.get_member(uid)
        if member:
            return member
        try:
            member = await guild.fetch_member(uid)
            return member
        except Exception:
            return None

    # 2) Fallback: try to capture a username string from Poketwo bot's catch message 
    name_match = re.search(r"Congratulations\s+([^\s!,:]+)", combined_text, re.I)
    if name_match:
        raw_name = name_match.group(1).strip()
        for member in guild.members:
            if member.name.lower() == raw_name.lower() or (member.display_name and member.display_name.lower() == raw_name.lower()):
                return member
    return None

def extract_pokemon_name(combined_text: str):
    # Extract the Pokémon name from the combined text (message + embeds); Returns cleaned pokemon name string.
    match = re.search(r"caught(?: a)?(?: Level \d+)?\s+([^\n:(!<]+)", combined_text, re.I)
    if match:
        raw = match.group(1).strip()
        # remove any trailing emoji markup, unicode gender symbols, and stray chars
        raw = re.sub(r"<[^>]*>", "", raw)                 # remove <...> markup
        raw = re.sub(r":[^:\s>]+:", "", raw)             # remove :emoji: markup
        raw = re.sub(r"[^\w\s\-\']+", "", raw)           # remove punctuation except hyphen/apostrophe
        return raw.strip()
    return "a Pokémon"

async def process_catch(message: discord.Message):
    """Process a Pokétwo catch message into counts and logs."""
    # Build combined text from content + embed titles/descriptions/fields
    combined_text = (message.content or "")
    for e in message.embeds:
        if e.title:
            combined_text += " " + str(e.title)
        if e.description:
            combined_text += " " + str(e.description)
        for f in getattr(e, "fields", []):
            combined_text += " " + str(f.name) + " " + str(f.value)

    # Prefer direct mentions first
    catcher = message.mentions[0] if message.mentions else None
    if not catcher and message.guild:
        catcher = await resolve_mention_to_member(message.guild, combined_text)

    if not catcher:
        # no mention and cannot resolve a member (fail)
        print("[process_catch] could not resolve catcher in message:", message.id)
        return

    pokemon = extract_pokemon_name(combined_text)

    # Update counts
    catch_counts[catcher.id] += 1
    daily_catch_counts[catcher.id] += 1
    save_catch_data()

    # Build embed for log channel
    embed = discord.Embed(
        title="🐾 Catch Update",
        description=f"{catcher.display_name} caught **{pokemon}**!",
        color=discord.Color.red()
    )
    embed.add_field(
        name="Totals",
        value=f"Today: {daily_catch_counts[catcher.id]}\nAll-Time: {catch_counts[catcher.id]}",
        inline=False
    )

    # Send to catch log
    await log_catch(message.guild, embed=embed)
    # Console debug
    print(f"[process_catch] {catcher} -> {pokemon} (today {daily_catch_counts[catcher.id]}, all {catch_counts[catcher.id]})")

# Events
@bot.event
async def on_ready():
    global last_reset_date
    # Cross-platform logic
    try:
        print(f"✅ Logged in as {bot.user} (id={bot.user.id})")
    except UnicodeEncodeError:
        print(f"Logged in as {bot.user} (id={bot.user.id})")
    load_catch_data()
    if not last_reset_date:
        last_reset_date = datetime.utcnow().date().isoformat()

    # Set bot presence/status
    try:
        activity = discord.Activity(type=discord.ActivityType.playing, name="a game! 🌹")
        await bot.change_presence(status=discord.Status.online, activity=activity)
    except Exception as e:
        print(f"[on_ready] failed to set presence: {e}")

    # Start background tasks
    auto_unlock_channels.start()
    reset_daily_counts.start()

    # Conditional backfill (only if data file is missing/empty)
    if should_backfill:
        await backfill_catches()

@bot.event
async def on_message(message: discord.Message):
    if message.author == bot.user:
        return

    # Locking logic: only trigger when monitored bot actually mentions a user
    if message.author.id in PING_BOT_IDS and message.mentions:
        await lock_channel(message.channel, reason=f"ping from {message.author.name}")

    # Catch detection: look for Pokétwo messages (text + embeds)
    if message.author.id == POKETWO_ID:
        combined_text = (message.content or "")
        for e in message.embeds:
            if e.title:
                combined_text += " " + str(e.title)
            if e.description:
                combined_text += " " + str(e.description)
            for f in getattr(e, "fields", []):
                combined_text += " " + str(f.name) + " " + str(f.value)

        # If we see a 'caught' or 'congratulations' string in message — process catch
        if re.search(r"\b(caught|congratulations)\b", combined_text, re.I):
            await process_catch(message)

    await bot.process_commands(message)

# Commands
@bot.command()
async def ping(ctx):
    await ctx.send("Pong!")

@bot.command()
@commands.has_permissions(manage_channels=True)
async def lock(ctx):
    await lock_channel(ctx.channel, reason="manual")
    await ctx.send("🔒 Channel manually locked.")

@bot.command()
@commands.has_permissions(manage_channels=True)
async def unlock(ctx):
    await unlock_channel(ctx.channel, reason="manual")

@bot.command()
@commands.has_permissions(manage_channels=True)
async def status(ctx):
    if not locked_channels:
        await ctx.send("✅ No channels are currently locked.")
        return

    now = datetime.utcnow()
    lines = []
    for cid, data in locked_channels.items():
        channel = bot.get_channel(cid)
        if channel:
            delta = data["unlock_time"] - now
            hours, remainder = divmod(int(delta.total_seconds()), 3600)
            minutes, _ = divmod(remainder, 60)
            time_left = f"{hours}h {minutes}m" if delta.total_seconds() > 0 else "soon"
            lines.append(f"🔒 {channel.mention} → unlocks in **{time_left}**")
    await ctx.send("\n".join(lines))

@bot.command()
async def catches(ctx, member: discord.Member = None):
    # Check your or someone else's catch totals.
    member = member or ctx.author
    total = catch_counts.get(member.id, 0)
    daily = daily_catch_counts.get(member.id, 0)
    await ctx.send(f"📊 {member.mention} → Today: **{daily}**, All-Time: **{total}**")

@bot.command()
async def catchlog(ctx, scope: str = "daily"):
    # View the catch leaderboard. Usage: !catchlog [daily|all]
    if scope.lower() == "all":
        counts = catch_counts
        title = "🏆 All-Time Catch Leaderboard"
    else:
        counts = daily_catch_counts
        title = "📅 Daily Catch Leaderboard"

    if not counts:
        await ctx.send("No catches recorded yet.")
        return

    sorted_counts = sorted(counts.items(), key=lambda x: x[1], reverse=True)[:10]
    desc = ""
    for idx, (uid, total) in enumerate(sorted_counts, start=1):
        member = ctx.guild.get_member(uid)
        name = member.display_name if member else f"User {uid}"
        desc += f"**{idx}. {name}** → {total}\n"

    embed = discord.Embed(title=title, description=desc, color=discord.Color.gold())
    await ctx.send(embed=embed)

@bot.command()
@commands.has_permissions(administrator=True)
async def backfill(ctx, limit: int = 1000):
    # Admin command to force a backfill for the current guild. May double-count.
    await ctx.send("Starting backfill for this server (this may double-count if data already exists)...")
    processed = 0
    for channel in ctx.guild.text_channels:
        try:
            async for msg in channel.history(limit=limit, oldest_first=True):
                combined_text = (msg.content or "")
                for e in msg.embeds:
                    if e.title:
                        combined_text += " " + str(e.title)
                    if e.description:
                        combined_text += " " + str(e.description)
                    for f in getattr(e, "fields", []):
                        combined_text += " " + str(f.name) + " " + str(f.value)
                if msg.author.id == POKETWO_ID and re.search(r"\b(caught|congratulations)\b", combined_text, re.I):
                    await process_catch(msg)
                    processed += 1
        except discord.Forbidden:
            continue
    await ctx.send(f"Backfill complete. Processed ~{processed} catches (approx).")

# Reaction unlock
@bot.event
async def on_reaction_add(reaction, user):
    if user.bot:
        return

    channel = reaction.message.channel
    if (
        channel.id in locked_channels
        and reaction.message.id == locked_channels[channel.id]["message_id"]
        and str(reaction.emoji) == "🔓"
    ):
        await unlock_channel(channel, reason=f"reaction by {user.mention}")

# Background tasks
@tasks.loop(minutes=1)
async def auto_unlock_channels():
    now = datetime.utcnow()
    to_unlock = [cid for cid, data in locked_channels.items() if now >= data["unlock_time"]]

    for cid in to_unlock:
        channel = bot.get_channel(cid)
        if channel:
            await unlock_channel(channel, reason="auto-timeout")
            await channel.send("⏰ Auto-unlocked after 24 hours.")

@tasks.loop(minutes=1)
async def reset_daily_counts():
    global last_reset_date
    now_date = datetime.utcnow().date().isoformat()
    if last_reset_date != now_date:
        daily_catch_counts.clear()
        last_reset_date = now_date
        save_catch_data()

# Backfill function (startup)
async def backfill_catches():
    # Scan recent history in all text channels for Pokétwo catches, only if no data exists.
    print("🔄 Backfilling catches from history...")
    for guild in bot.guilds:
        for channel in guild.text_channels:
            try:
                async for msg in channel.history(limit=1000, oldest_first=True):
                    combined_text = (msg.content or "")
                    for e in msg.embeds:
                        if e.title:
                            combined_text += " " + str(e.title)
                        if e.description:
                            combined_text += " " + str(e.description)
                        for f in getattr(e, "fields", []):
                            combined_text += " " + str(f.name) + " " + str(f.value)
                    if msg.author.id == POKETWO_ID and re.search(r"\b(caught|congratulations)\b", combined_text, re.I):
                        await process_catch(msg)
            except discord.Forbidden:
                continue
    print("✅ Backfill complete.")

# Run the bot
bot.run(BOT_TOKEN)
