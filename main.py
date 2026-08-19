import os
import asyncio
import discord
from discord.ext import commands
from keep_alive import keep_alive

intents = discord.Intents.default()
intents.message_content = True
intents.guilds = True

bot = commands.Bot(command_prefix="!", intents=intents)

@bot.event
async def on_ready():
    print(f"Logged in as {bot.user} (ID: {bot.user.id})")
    try:
        if not bot.get_cog("RobloxAuditLogger"):
            await bot.load_extension("cogs.log")
        
        synced = await bot.tree.sync()
        print(f"Synced {len(synced)} slash commands globally.")
    except Exception as e:
        print(f"Error loading cogs or syncing commands: {e}")

if __name__ == "__main__":
    token = os.getenv("TOKEN")
    if not token:
        print("Error: 'TOKEN' environment variable is missing.")
    else:
        keep_alive(bot)
        bot.run(token)


