# Simple testing file to check if bot is online.
import os
import discord
from discord.ext import commands
from dotenv import load_dotenv

load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")

intents = discord.Intents.default()
intents.message_content = True  # Allow reading message content

# Basic bot for testing
bot = commands.Bot(command_prefix="!", intents=intents)

@bot.event
async def on_ready():
    print(f"✅ Logged in as {bot.user} (ID: {bot.user.id})")

@bot.command()
async def ping(ctx):
    await ctx.send("Pong!")

if __name__ == "__main__":
    bot.run(BOT_TOKEN)
