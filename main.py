import os
import time
import asyncio
import logging
import discord
from discord.ext import commands
from keep_alive import keep_alive, set_bot

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("main")

intents = discord.Intents.default()
intents.message_content = True
intents.guilds = True


class LogBot(commands.Bot):
    def __init__(self):
        super().__init__(command_prefix="!", intents=intents)

    async def setup_hook(self):
        # Runs once per start (not on every gateway reconnect like on_ready)
        await self.load_extension("cogs.log")
        try:
            synced = await self.tree.sync()
            log.info("Synced %d slash commands globally.", len(synced))
        except Exception:
            log.exception("Slash command sync failed (bot will keep running)")

    async def on_ready(self):
        log.info("Logged in as %s (ID: %s)", self.user, self.user.id)


async def run_forever(token: str):
    delay = 15
    while True:
        bot = LogBot()
        set_bot(bot)
        started = time.monotonic()
        try:
            async with bot:
                await bot.start(token)
            log.warning("Bot stopped without an error.")
        except discord.LoginFailure:
            log.error("Discord rejected the token. Check the 'TOKEN' variable.")
            return
        except discord.PrivilegedIntentsRequired:
            log.error("Enable the Message Content intent in the Discord Developer Portal.")
            return
        except discord.HTTPException as e:
            log.error("Discord HTTP error %s: %s", e.status, e)
        except Exception:
            log.exception("Bot crashed")

        if time.monotonic() - started > 600:
            delay = 15  # It ran for a while, so start the backoff over
        log.info("Starting the bot again in %ss", delay)
        await asyncio.sleep(delay)
        delay = min(delay * 2, 600)


if __name__ == "__main__":
    token = os.getenv("TOKEN")
    if not token:
        print("Error: 'TOKEN' environment variable is missing.")
    else:
        keep_alive()
        asyncio.run(run_forever(token))
