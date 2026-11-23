import os
import re
import json
import calendar
import time
import random
import hashlib
import logging
import asyncio
import aiohttp
import discord
from discord import app_commands
from discord.ext import commands, tasks
from datetime import datetime, timezone
from typing import Optional, Dict, Any, List, Set, Tuple

try:
    import motor.motor_asyncio
except ImportError:
    motor = None

AUTHORIZED_USER_IDS = [1219266886143967245, 1391931433521774742]

GREEN_COLOR = discord.Color.from_rgb(46, 139, 87)
ORANGE_COLOR = discord.Color.from_rgb(211, 84, 0)
RED_COLOR = discord.Color.from_rgb(192, 57, 43)
BLUE_COLOR = discord.Color.from_rgb(41, 128, 185)

FOOTER_FALLBACK = "TGE Logs Bot"
DB_NAME = "roblox_audit_logger"

AUDIT_POLL_INTERVAL = 30      # Seconds between audit-log polls per group
ROLES_CACHE_TTL = 3600        # Seconds to cache a group's role list
MAX_PAGES_PER_ROLE = 2        # 2 pages x 100 = 200 members per role (bandwidth cap)


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, default))
    except (TypeError, ValueError):
        return default


# --- Bandwidth protection ------------------------------------------------
# Render's free plan includes 5 GB of outbound data per month for the whole workspace.
# The bot counts what it sends and slows down (or pauses) before that budget runs out.
# Set BANDWIDTH_BUDGET_MB lower if several bots share one Render workspace.
BANDWIDTH_BUDGET_BYTES = int(_env_float("BANDWIDTH_BUDGET_MB", 3000) * 1024 * 1024)
BANDWIDTH_PAUSE_AT = 0.98      # Stop polling at 98% of the budget until the next month
MAX_SLOWDOWN = 12.0            # Never stretch poll intervals more than 12x
REQUEST_OVERHEAD_BYTES = 1200  # Estimated size of one outgoing HTTP request (headers)
EMBED_SEND_BYTES = 3000        # Estimated size of one Discord embed message
CONFIG_REFRESH_SECONDS = 60    # How often saved setups are re-read from MongoDB
USAGE_FLUSH_SECONDS = 600      # How often the usage counter is saved to MongoDB

# The saved setups are scanned without any old saved member list (rosters live in memory only)
CONFIG_PROJECTION = {"member_cache": 0}

# Where to warn when the bandwidth budget is used up.
# Set ALERT_USER_IDS="123,456" on Render to ping someone else (default: the authorized users).
ALERT_CHANNEL_ID = 1574543209076359188


def _alert_user_ids() -> List[int]:
    ids = [int(p) for p in re.findall(r"\d+", os.getenv("ALERT_USER_IDS", ""))]
    return ids or list(AUTHORIZED_USER_IDS)


ALERT_USER_IDS = _alert_user_ids()

log = logging.getLogger("roblox_audit")

_mongo_client = None


def get_db():
    global _mongo_client
    mongo_uri = os.getenv("MONGO_URI")
    if not mongo_uri or not motor:
        return None
    if _mongo_client is None:
        try:
            _mongo_client = motor.motor_asyncio.AsyncIOMotorClient(
                mongo_uri,
                serverSelectionTimeoutMS=5000,
                connectTimeoutMS=10000,
                socketTimeoutMS=20000,
            )
        except Exception:
            log.exception("Could not create the MongoDB client")
            return None
    try:
        return _mongo_client[DB_NAME]
    except Exception:
        return None


def is_authorized_user(user_id: int) -> bool:
    return user_id in AUTHORIZED_USER_IDS


def extract_group_id(input_val: str) -> Optional[int]:
    """Extracts a group ID from a plain integer string or a Roblox group URL."""
    clean_input = input_val.strip()
    if clean_input.isdigit():
        return int(clean_input)

    match = re.search(r"roblox\.com/(?:[a-z]{2}/)?groups/(\d+)", clean_input, re.IGNORECASE)
    if match:
        return int(match.group(1))
    return None


def audit_entry_key(entry: Dict[str, Any]) -> str:
    """Stable identifier for an audit-log entry.

    Roblox audit-log entries do not reliably include an "id", so fall back to
    a hash of the whole entry. This keeps the "what is new?" watermark working.
    """
    if entry.get("id") is not None:
        return str(entry["id"])
    raw = json.dumps(entry, sort_keys=True, default=str)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


def user_link(name: str, user_id: Any = None) -> str:
    if user_id:
        return f"[{name}](https://www.roblox.com/users/{user_id}/profile)"
    return f"**{name}**"


def error_embed(title: str, description: Optional[str] = None) -> discord.Embed:
    return discord.Embed(title=title, description=description, color=RED_COLOR)


# ==========================================
# BANDWIDTH GUARD
# ==========================================
class BandwidthGuard:
    """Counts the data this bot sends out and slows polling down before a monthly cap is reached.

    It keeps a running total for the month, compares it with how much of the month has
    passed, and returns a slow-down factor (or None when the budget is used up and
    polling should pause until next month).
    """

    def __init__(self, budget_bytes: int):
        self.budget = max(int(budget_bytes), 0)
        self.month = self._month_key()
        self.used = 0
        self.dirty = False

    @staticmethod
    def _month_key() -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m")

    def _roll(self):
        key = self._month_key()
        if key != self.month:
            self.month, self.used, self.dirty = key, 0, True

    def add(self, nbytes: int):
        self._roll()
        self.used += int(nbytes)
        self.dirty = True

    def restore(self, month: Any, used: Any):
        """Adds the total saved before a restart (only if it is for the current month)."""
        self._roll()
        if month == self.month and isinstance(used, (int, float)):
            self.used += int(used)
            self.dirty = True

    @staticmethod
    def month_progress() -> float:
        now = datetime.now(timezone.utc)
        days = calendar.monthrange(now.year, now.month)[1]
        elapsed = (now.day - 1) * 86400 + now.hour * 3600 + now.minute * 60 + now.second
        return elapsed / (days * 86400)

    def multiplier(self) -> Optional[float]:
        """1.0 = full speed, >1 = poll less often, None = budget used up (pause)."""
        self._roll()
        if self.budget <= 0:
            return 1.0
        fraction = self.used / self.budget
        if fraction >= BANDWIDTH_PAUSE_AT:
            return None
        pace = fraction / max(self.month_progress(), 0.05)  # >1 means spending faster than the month allows
        return 1.0 if pace <= 1.0 else min(pace, MAX_SLOWDOWN)

    def summary(self) -> str:
        self._roll()
        mb = 1024 * 1024
        return f"{self.used / mb:.0f} / {self.budget / mb:.0f} MB this month"


# ==========================================
# DYNAMIC POLLING TRACKER
# ==========================================
class GroupTracker:
    """Tracks polling intervals individually for each config to optimize bandwidth."""

    def __init__(self, group_id: int):
        self.group_id = group_id
        self.last_polled: float = 0.0
        self.last_changed: float = time.time()
        self.current_interval: int = 300  # Default 5-minute loop

    def update_interval(self, has_changes: bool):
        now = time.time()
        self.last_polled = now

        if has_changes:
            self.last_changed = now
            self.current_interval = 300
            return

        quiet_duration = now - self.last_changed

        if quiet_duration > 3600:      # Quiet > 1 hour -> every 9 minutes
            self.current_interval = 540
        elif quiet_duration > 1800:    # Quiet > 30 minutes -> every 7 minutes
            self.current_interval = 420
        else:                          # Active -> every 5 minutes
            self.current_interval = 300

    def is_due(self, multiplier: float = 1.0) -> bool:
        return (time.time() - self.last_polled) >= self.current_interval * multiplier


# ==========================================
# MAIN COG
# ==========================================
class RobloxAuditLogger(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.session: Optional[aiohttp.ClientSession] = None
        self.cycle_count: int = 0
        self.start_time: datetime = datetime.now(timezone.utc)
        self.roles_cache: Dict[int, Dict[str, Any]] = {}      # {group_id: {"timestamp", "roles"}}
        self.group_trackers: Dict[Any, GroupTracker] = {}     # keyed by config _id
        self.initialized_groups: Set[Tuple[str, int]] = set() # (guild_id, group_id) synced this run
        self.public_tasks: Dict[Any, asyncio.Task] = {}       # One running public poll per config
        self.last_cycle_at: float = time.time()               # Heartbeat for the watchdog
        self.guard = BandwidthGuard(BANDWIDTH_BUDGET_BYTES)
        self._cookie_len: int = len((os.getenv("ROBLOX_COOKIE") or "").strip())
        self.configs: List[Dict[str, Any]] = []               # saved setups, refreshed every minute
        self.configs_loaded_at: float = 0.0
        self.configs_dirty: bool = True
        self.member_caches: Dict[Any, Dict[str, Any]] = {}    # roster per config, memory only
        self.usage_loaded: bool = False
        self.usage_flushed_at: float = 0.0
        self.cleaned_old_rosters: bool = False
        self.guard_state: str = "normal"
        self.alerted_month: Optional[str] = None              # month the limit alert was already sent for
        self.alert_retry_at: float = 0.0
        self.poll_logs_task.start()
        self.watchdog_task.start()

    async def cog_unload(self):
        self.poll_logs_task.cancel()
        self.watchdog_task.cancel()
        for task in list(self.public_tasks.values()):
            task.cancel()
        db = get_db()
        if db is not None:
            await self._flush_usage(db, force=True)
        if self.session and not self.session.closed:
            await self.session.close()

    # ------------------------------------------------------------------
    # Small helpers
    # ------------------------------------------------------------------
    def _footer(self) -> str:
        return self.bot.user.name if self.bot.user else FOOTER_FALLBACK

    def _make_embed(self, description: str, color: discord.Color, fields: List[Tuple[str, str]]) -> discord.Embed:
        embed = discord.Embed(description=description, color=color)
        for name, value in fields:
            embed.add_field(name=name, value=value, inline=False)
        embed.set_footer(text=self._footer())
        return embed

    async def _send(self, channel, embed: discord.Embed):
        self.guard.add(EMBED_SEND_BYTES)
        try:
            await channel.send(embed=embed)
        except discord.Forbidden:
            log.warning("Missing permission to send in channel %s", getattr(channel, "id", "?"))
        except discord.HTTPException:
            log.warning("Failed to send log embed in channel %s", getattr(channel, "id", "?"), exc_info=True)

    @staticmethod
    async def _deny(interaction: discord.Interaction):
        await interaction.response.send_message(
            embed=error_embed("Permission Denied", "You are not authorized to use this command."),
            ephemeral=True,
        )

    @staticmethod
    def _rank_for(roles: List[Dict[str, Any]], role_id: Any = None, role_name: Optional[str] = None) -> Optional[int]:
        """Looks up a rank number by role ID first, then by role name."""
        if role_id is not None:
            for r in roles:
                if str(r.get("id")) == str(role_id):
                    return r.get("rank", 0)
        if role_name:
            for r in roles:
                if r.get("name") == role_name:
                    return r.get("rank", 0)
        return None

    # ------------------------------------------------------------------
    # Bandwidth and database helpers
    # ------------------------------------------------------------------
    def _count_request(self):
        self.guard.add(REQUEST_OVERHEAD_BYTES + self._cookie_len)

    async def _get_configs(self, db) -> List[Dict[str, Any]]:
        """Saved setups, re-read from MongoDB at most once a minute (or after a change)."""
        now = time.time()
        if self.configs_dirty or now - self.configs_loaded_at >= CONFIG_REFRESH_SECONDS:
            self.configs = await db["group_configs"].find({}, CONFIG_PROJECTION).to_list(length=1000)
            self.guard.add(600)
            self.configs_loaded_at = now
            self.configs_dirty = False
        return self.configs

    async def _update_config(self, db, cfg: Dict[str, Any], fields: Optional[Dict[str, Any]] = None,
                             unset: Optional[List[str]] = None):
        """Small database update that also keeps the in-memory copy in step."""
        update: Dict[str, Any] = {}
        if fields:
            update["$set"] = fields
        if unset:
            update["$unset"] = {name: "" for name in unset}
        if not update:
            return
        await db["group_configs"].update_one({"_id": cfg["_id"]}, update)
        self.guard.add(600)
        if fields:
            cfg.update(fields)
        for name in unset or []:
            cfg.pop(name, None)

    async def _flush_usage(self, db, force: bool = False):
        if not self.guard.dirty:
            return
        now = time.time()
        if not force and now - self.usage_flushed_at < USAGE_FLUSH_SECONDS:
            return
        try:
            await db["usage"].update_one(
                {"_id": "bandwidth"},
                {"$set": {"month": self.guard.month, "used": self.guard.used, "alerted": self.alerted_month}},
                upsert=True,
            )
            self.guard.dirty = False
            self.usage_flushed_at = now
        except Exception:
            log.warning("Could not save the bandwidth counter", exc_info=True)

    async def _sync_usage(self, db):
        """Loads the saved monthly total once after a restart, then saves it every few minutes."""
        if not self.usage_loaded:
            self.usage_loaded = True
            try:
                doc = await db["usage"].find_one({"_id": "bandwidth"})
                if doc:
                    self.guard.restore(doc.get("month"), doc.get("used"))
                    self.alerted_month = doc.get("alerted")
            except Exception:
                log.warning("Could not load the bandwidth counter", exc_info=True)
        if not self.cleaned_old_rosters:
            self.cleaned_old_rosters = True
            try:
                # Rosters used to be saved in MongoDB on every poll. They live in memory now.
                await db["group_configs"].update_many({"member_cache": {"$exists": True}}, {"$unset": {"member_cache": ""}})
            except Exception:
                log.warning("Could not clean up old saved rosters", exc_info=True)
        await self._flush_usage(db)

    async def _note_guard_state(self, db, multiplier: Optional[float]):
        state = "paused" if multiplier is None else ("slowed" if multiplier > 1.0 else "normal")
        if state != self.guard_state:
            log.warning("Bandwidth guard is now %s (%s)", state, self.guard.summary())
            self.guard_state = state
        if state == "paused":
            await self._alert_budget_reached(db)

    async def _alert_budget_reached(self, db):
        """Pings the owners once per month when the bandwidth budget is used up."""
        if self.alerted_month == self.guard.month or time.time() < self.alert_retry_at:
            return
        self.alert_retry_at = time.time() + 600  # If sending fails, try again in 10 minutes
        try:
            channel = self.bot.get_channel(ALERT_CHANNEL_ID) or await self.bot.fetch_channel(ALERT_CHANNEL_ID)
            mentions = " ".join(f"<@{uid}>" for uid in ALERT_USER_IDS)
            name = self.bot.user.name if self.bot.user else "The bot"
            await channel.send(
                f"{mentions}\n**{name}: bandwidth limit reached** ({self.guard.summary()}).\n"
                f"Polling is paused until the next month (UTC). The bot is still online and slash commands still work. "
                f"To keep logging sooner, raise `BANDWIDTH_BUDGET_MB` on Render and restart the service.",
                allowed_mentions=discord.AllowedMentions(users=True, everyone=False, roles=False),
            )
        except Exception:
            log.warning("Could not send the bandwidth limit alert", exc_info=True)
            return
        self.alerted_month = self.guard.month
        self.guard.dirty = True
        await self._flush_usage(db, force=True)

    # ------------------------------------------------------------------
    # Fun listener (1 in 10 chance when pinged)
    # ------------------------------------------------------------------
    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot or not self.bot.user:
            return

        if self.bot.user in message.mentions and random.randint(1, 10) == 1:
            try:
                await message.channel.send("Son why are you pinging me? \nMy job is to log not talking")
            except Exception:
                pass

    # ------------------------------------------------------------------
    # HTTP
    # ------------------------------------------------------------------
    async def get_http_session(self) -> aiohttp.ClientSession:
        if self.session is None or self.session.closed:
            headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
            }
            cookie_str = os.getenv("ROBLOX_COOKIE")
            cookies = {}
            if cookie_str:
                clean_cookie = cookie_str.strip()
                if ".ROBLOSECURITY=" in clean_cookie:
                    clean_cookie = clean_cookie.split(".ROBLOSECURITY=")[-1]
                cookies = {".ROBLOSECURITY": clean_cookie}

            timeout = aiohttp.ClientTimeout(total=10)
            self.session = aiohttp.ClientSession(headers=headers, cookies=cookies, timeout=timeout)
        return self.session

    async def _safe_fetch_json(self, url: str, max_retries: int = 3) -> Optional[Dict[str, Any]]:
        """Fetches JSON with backoff for Roblox rate limits and transient server errors."""
        session = await self.get_http_session()
        for attempt in range(max_retries):
            is_last = attempt == max_retries - 1
            self._count_request()
            try:
                async with session.get(url) as resp:
                    if resp.status == 200:
                        return await resp.json()

                    if resp.status == 429:
                        retry_after = resp.headers.get("Retry-After", "")
                        delay = float(retry_after) if retry_after.replace(".", "", 1).isdigit() else 2.0 * (attempt + 1)
                        log.warning("Rate limited (429) on %s", url)
                        if not is_last:
                            await asyncio.sleep(min(delay, 15.0))
                        continue

                    if resp.status in (500, 502, 503, 504):
                        if not is_last:
                            await asyncio.sleep(0.5 * (2 ** attempt))
                        continue

                    log.debug("GET %s returned %s", url, resp.status)
                    return None
            except Exception:
                if is_last:
                    log.warning("GET %s failed after %s attempts", url, max_retries, exc_info=True)
                    return None
                await asyncio.sleep(0.5 * (2 ** attempt))
        return None

    async def _fetch_group_roles(self, group_id: int) -> List[Dict[str, Any]]:
        """Fetches a group's roles with a 1-hour cache."""
        now = time.time()
        entry = self.roles_cache.get(group_id)
        if entry and now - entry["timestamp"] < ROLES_CACHE_TTL:
            return entry["roles"]

        data = await self._safe_fetch_json(f"https://groups.roblox.com/v1/groups/{group_id}/roles", max_retries=3)
        if data and "roles" in data:
            roles = data["roles"]
            self.roles_cache[group_id] = {"timestamp": now, "roles": roles}
            return roles
        # Fall back to stale cache rather than nothing
        return entry["roles"] if entry else []

    async def _fetch_filtered_group_members(
        self, group_id: int, min_rank_val: int = 0
    ) -> Tuple[Dict[str, Dict[str, Any]], bool, Set[str]]:
        """Returns (member_map, all_pages_succeeded, truncated_role_names).

        A role is "truncated" when it has more members than MAX_PAGES_PER_ROLE
        pages can show. Join/leave events for those roles are unreliable because
        members can slip in and out of the visible window, so callers skip them.
        """
        roles = await self._fetch_group_roles(group_id)
        if not roles:
            return {}, False, set()

        # Rank 0 is the Guest role, which is not a real membership list
        target_roles = [r for r in roles if r.get("rank", 0) >= max(min_rank_val, 1)]

        member_map: Dict[str, Dict[str, Any]] = {}
        truncated: Set[str] = set()
        all_pages_succeeded = True

        for role in target_roles:
            role_id = role.get("id")
            role_name = role.get("name")
            role_rank = role.get("rank")

            cursor = ""
            pages_fetched = 0

            while pages_fetched < MAX_PAGES_PER_ROLE:
                members_url = (
                    f"https://groups.roblox.com/v1/groups/{group_id}/roles/{role_id}/users"
                    f"?limit=100&sortOrder=Desc"
                )
                if cursor:
                    members_url += f"&cursor={cursor}"

                m_data = await self._safe_fetch_json(members_url, max_retries=2)
                if m_data is None:
                    all_pages_succeeded = False
                    break

                for u in m_data.get("data", []):
                    uid = str(u.get("userId"))
                    member_map[uid] = {
                        "username": u.get("username"),
                        "role_name": role_name,
                        "rank": role_rank,
                    }

                cursor = m_data.get("nextPageCursor")
                pages_fetched += 1
                if not cursor:
                    break

                await asyncio.sleep(0.2)

            if cursor and pages_fetched >= MAX_PAGES_PER_ROLE:
                truncated.add(role_name)

            if not all_pages_succeeded:
                break

        return member_map, all_pages_succeeded, truncated

    # ------------------------------------------------------------------
    # Setup
    # ------------------------------------------------------------------
    async def _handle_setup(
        self,
        interaction: discord.Interaction,
        channel: discord.TextChannel,
        group_input: str,
        min_rank_name: Optional[str] = None,
        visible: bool = False,
    ):
        if not is_authorized_user(interaction.user.id):
            return await self._deny(interaction)

        is_ephemeral = not visible
        await interaction.response.defer(ephemeral=is_ephemeral)

        async def reply(embed: discord.Embed):
            await interaction.followup.send(embed=embed, ephemeral=is_ephemeral)

        group_id = extract_group_id(group_input)
        if not group_id:
            return await reply(error_embed(
                "Invalid Group Input",
                "Please provide a valid numeric Group ID or a valid Roblox Group link.",
            ))

        db = get_db()
        if db is None:
            return await reply(error_embed("Database Error", "Could not connect to MongoDB Atlas."))

        perms = channel.permissions_for(interaction.guild.me)
        if not (perms.view_channel and perms.send_messages and perms.embed_links):
            return await reply(error_embed(
                "Missing Channel Permissions",
                f"I need **View Channel**, **Send Messages** and **Embed Links** in {channel.mention}.",
            ))

        try:
            group_data = await self._safe_fetch_json(f"https://groups.roblox.com/v1/groups/{group_id}", max_retries=3)
            if not group_data:
                return await reply(error_embed(
                    "Invalid Roblox Group",
                    f"Group ID `{group_id}` could not be retrieved from Roblox.",
                ))

            group_name = group_data.get("name", f"Group {group_id}")
            roles = await self._fetch_group_roles(group_id)

            min_rank_value = 0
            selected_role_name = None

            if min_rank_name:
                wanted = min_rank_name.strip().lower()
                matched_role = next((r for r in roles if r.get("name", "").strip().lower() == wanted), None)
                if not matched_role:
                    role_list_str = "\n".join(f"- `{r.get('name')}` (Rank {r.get('rank')})" for r in roles)
                    return await reply(error_embed(
                        "Rank Name Not Found",
                        f"Could not find a rank matching **`{min_rank_name}`** in **{group_name}**.\n\n"
                        f"**Available Group Ranks:**\n{role_list_str}"[:4000],
                    ))
                min_rank_value = matched_role.get("rank", 0)
                selected_role_name = matched_role.get("name")

            # Roblox only accepts limit = 10, 25, 50 or 100 (limit=1 is rejected with 400)
            audit_url = f"https://groups.roblox.com/v1/groups/{group_id}/audit-log?limit=10&sortOrder=Desc"
            session = await self.get_http_session()

            last_audit_key: Optional[str] = None
            mode = "audit_log"

            self._count_request()
            try:
                async with session.get(audit_url) as resp:
                    if resp.status == 200:
                        body = await resp.json()
                        logs = body.get("data", [])
                        # Empty string = "log is empty, anything that appears later is new"
                        last_audit_key = audit_entry_key(logs[0]) if logs else ""
                    else:
                        mode = "public_polling"
            except Exception:
                mode = "public_polling"

            guild_id = str(interaction.guild.id)

            config_payload: Dict[str, Any] = {
                "guild_id": guild_id,
                "guild_name": interaction.guild.name,
                "group_id": group_id,
                "group_name": group_name,
                "channel_id": str(channel.id),
                "mode": mode,
                "min_rank_name": selected_role_name,
                "min_rank_value": min_rank_value,
                "created_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
            }

            update_doc: Dict[str, Any] = {"$set": config_payload, "$unset": {"member_cache": ""}}

            if mode == "public_polling":
                update_doc["$unset"]["last_audit_id"] = ""
            else:
                config_payload["last_audit_id"] = last_audit_key

            await db["group_configs"].update_one(
                {"guild_id": guild_id, "group_id": group_id},
                update_doc,
                upsert=True,
            )
            self.guard.add(1500)

            # Reload the setups on the next tick. In public mode the member list is built
            # silently in memory on the first poll, so nothing extra is downloaded here.
            self.configs_dirty = True
            self.initialized_groups.add((guild_id, group_id))

            mode_msg = "Audit Log Stream (Cookie)" if mode == "audit_log" else "Public Polling Stream (Fallback)"
            rank_filter_info = (
                f"**Minimum Rank**: `{selected_role_name}` (Rank {min_rank_value}+)\n"
                if selected_role_name else "**Minimum Rank**: `All Ranks (0+)`\n"
            )

            await reply(discord.Embed(
                title="Roblox Group Logger Configured",
                description=(
                    f"**Group**: [{group_name}](https://www.roblox.com/groups/{group_id})\n"
                    f"**Group ID**: `{group_id}`\n"
                    f"**Channel**: {channel.mention}\n"
                    f"**Tracking Mode**: `{mode_msg}`\n"
                    f"{rank_filter_info}\n"
                    f"*Baseline watermark established. System is actively watching for events.*"
                ),
                color=GREEN_COLOR,
            ))

        except Exception as e:
            log.exception("Setup failed for group input %r", group_input)
            await reply(error_embed("Setup Error", f"An unexpected error occurred: `{e}`"))

    # ====================================================================
    # Slash Commands
    # ====================================================================

    @app_commands.command(
        name="ping",
        description="View system latency, uptime, database status, and tracking overview."
    )
    @app_commands.describe(visible="Set to True to make the response visible to everyone (default: False).")
    async def ping_cmd(self, interaction: discord.Interaction, visible: bool = False):
        start_time = time.perf_counter()
        await interaction.response.defer(ephemeral=not visible)
        response_ms = round((time.perf_counter() - start_time) * 1000)
        gateway_ms = round(self.bot.latency * 1000)

        db = get_db()
        db_status = "Disconnected"
        db_ms_str = "N/A"
        total_groups = 0

        if db is not None:
            try:
                db_start = time.perf_counter()
                await db.command("ping")
                db_ms_str = f"{round((time.perf_counter() - db_start) * 1000)} ms"
                db_status = "Connected"
                total_groups = await db["group_configs"].count_documents({})
            except Exception:
                db_status = "Error"

        now = datetime.now(timezone.utc)
        hours, remainder = divmod(int((now - self.start_time).total_seconds()), 3600)
        minutes, seconds = divmod(remainder, 60)
        days, hours = divmod(hours, 24)
        uptime_str = f"{days}d {hours}h {minutes}m {seconds}s" if days > 0 else f"{hours}h {minutes}m {seconds}s"

        embed = discord.Embed(title="System Status Overview", color=BLUE_COLOR, timestamp=now)
        embed.add_field(
            name="Latency",
            value=(
                f"Gateway Ping: `{gateway_ms} ms`\n"
                f"Response Time: `{response_ms} ms`\n"
                f"Database Latency: `{db_ms_str}`"
            ),
            inline=True,
        )
        embed.add_field(
            name="System Health",
            value=(
                f"Database: `{db_status}`\n"
                f"Uptime: `{uptime_str}`\n"
                f"Poll Cycles: `{self.cycle_count}`\n"
                f"Bandwidth: `{self.guard.summary()}`"
            ),
            inline=True,
        )
        embed.add_field(
            name="Tracking Stats",
            value=(
                f"Tracked Groups: `{total_groups}`\n"
                f"Total Servers: `{len(self.bot.guilds)}`\n"
                f"WebSocket Status: `{'Closed' if self.bot.is_closed() else 'Connected'}`"
            ),
            inline=False,
        )
        embed.set_footer(text=self._footer())
        await interaction.followup.send(embed=embed, ephemeral=not visible)

    @app_commands.command(
        name="log-help",
        description="View commands, usage guides, and recommended settings for optimal performance."
    )
    @app_commands.describe(visible="Set to True to make the response visible to everyone (default: False).")
    async def log_help_cmd(self, interaction: discord.Interaction, visible: bool = False):
        if not is_authorized_user(interaction.user.id):
            return await self._deny(interaction)

        embed = discord.Embed(
            title="Roblox Group Logger - Usage & Optimization Guide",
            description="Use the commands below to configure and monitor Roblox group audit events.",
            color=BLUE_COLOR,
        )
        embed.add_field(
            name="Available Commands",
            value=(
                "- **/ping**\n"
                "  View system status, ping latency, uptime, and tracking stats.\n\n"
                "- **/setup `[channel]` `[group]`**\n"
                "  Tracks all events for all ranks in the group.\n\n"
                "- **/rank-setup `[channel]` `[group]` `[min_rank_name]`**\n"
                "  Tracks events exclusively for members at or above a specific rank.\n\n"
                "- **/remove-setup `[group]`**\n"
                "  Stops logging for a specific group.\n\n"
                "- **/list-setups**\n"
                "  Displays all configured group logs in this server."
            ),
            inline=False,
        )
        embed.add_field(
            name="Optimization Advice (Large Groups 1,000+ Members)",
            value=(
                "- **Avoid using `/setup` on massive groups** without a cookie attached. Fetching thousands of bottom-rank members triggers Roblox API rate limits.\n"
                "- **Use `/rank-setup` instead**: Set `min_rank_name` to a stable staff or middle-tier rank.\n"
                "- **Case-Insensitive Exact Names**: When entering `min_rank_name`, type the exact rank title as shown on Roblox.\n"
                "- **Public polling cap**: only the first 200 members of each rank are scanned, and join/leave events are skipped for ranks larger than that."
            ),
            inline=False,
        )
        embed.set_footer(text=self._footer())
        await interaction.response.send_message(embed=embed, ephemeral=not visible)

    @app_commands.command(
        name="setup",
        description="Setup Roblox Group activity logging for ALL ranks (Authorized Owners Only)."
    )
    @app_commands.guild_only()
    @app_commands.describe(
        channel="The text channel where logs should be sent",
        group="The Roblox Group ID or full Group Link to track",
        visible="Set to True to make the response visible to everyone (default: False).",
    )
    async def setup_cmd(
        self,
        interaction: discord.Interaction,
        channel: discord.TextChannel,
        group: str,
        visible: bool = False,
    ):
        await self._handle_setup(interaction, channel, group, min_rank_name=None, visible=visible)

    @app_commands.command(
        name="rank-setup",
        description="Setup Roblox Group logging for a specific MINIMUM rank name and above."
    )
    @app_commands.guild_only()
    @app_commands.describe(
        channel="The text channel where logs should be sent",
        group="The Roblox Group ID or full Group Link to track",
        min_rank_name="The exact rank name to start logging from (e.g. Officer)",
        visible="Set to True to make the response visible to everyone (default: False).",
    )
    async def rank_setup_cmd(
        self,
        interaction: discord.Interaction,
        channel: discord.TextChannel,
        group: str,
        min_rank_name: str,
        visible: bool = False,
    ):
        await self._handle_setup(interaction, channel, group, min_rank_name=min_rank_name, visible=visible)

    @app_commands.command(
        name="remove-setup",
        description="Remove a Roblox Group audit setup from this server (Authorized Owners Only)."
    )
    @app_commands.guild_only()
    @app_commands.describe(
        group="The Roblox Group ID or full Group Link to untrack",
        visible="Set to True to make the response visible to everyone (default: False).",
    )
    async def remove_setup_cmd(self, interaction: discord.Interaction, group: str, visible: bool = False):
        if not is_authorized_user(interaction.user.id):
            return await self._deny(interaction)

        await interaction.response.defer(ephemeral=not visible)

        async def reply(embed: discord.Embed):
            await interaction.followup.send(embed=embed, ephemeral=not visible)

        group_id = extract_group_id(group)
        if not group_id:
            return await reply(error_embed("Invalid Group Input", "Please provide a valid numeric Group ID or Roblox Group link."))

        db = get_db()
        if db is None:
            return await reply(error_embed("Database Error", "Could not connect to MongoDB Atlas."))

        guild_id = str(interaction.guild.id)
        deleted = await db["group_configs"].find_one_and_delete({"guild_id": guild_id, "group_id": group_id})
        if not deleted:
            return await reply(error_embed("Not Found", f"No configuration found for Group ID `{group_id}`."))

        # Clean up in-memory state for the removed config
        key = deleted["_id"]
        self.group_trackers.pop(key, None)
        self.member_caches.pop(key, None)
        self.configs_dirty = True
        self.initialized_groups.discard((guild_id, group_id))
        task = self.public_tasks.pop(key, None)
        if task:
            task.cancel()

        await reply(discord.Embed(
            title="Setup Removed",
            description=f"Stopped tracking Group ID `{group_id}`.",
            color=GREEN_COLOR,
        ))

    @app_commands.command(
        name="list-setups",
        description="List active group configurations (Authorized Owners Only)."
    )
    @app_commands.guild_only()
    @app_commands.describe(visible="Set to True to make the response visible to everyone (default: False).")
    async def list_setups_cmd(self, interaction: discord.Interaction, visible: bool = False):
        if not is_authorized_user(interaction.user.id):
            return await self._deny(interaction)

        await interaction.response.defer(ephemeral=not visible)

        db = get_db()
        if db is None:
            return await interaction.followup.send(
                embed=error_embed("Database Error", "Could not connect to MongoDB Atlas."),
                ephemeral=not visible,
            )

        configs = await db["group_configs"].find({"guild_id": str(interaction.guild.id)}).to_list(length=100)
        lines = []
        for doc in configs:
            g_name = doc.get("group_name", "Group")
            g_id = doc.get("group_id")
            ch_id = doc.get("channel_id")
            mode = doc.get("mode", "audit_log")
            min_r = doc.get("min_rank_name") or "All"
            lines.append(
                f"- [{g_name}](https://www.roblox.com/groups/{g_id}) (`{g_id}`) -> <#{ch_id}> [{mode} | Min Rank: {min_r}]"
            )

        description = "\n".join(lines) if lines else "*(No active group setups)*"
        if len(description) > 4000:
            description = description[:3990].rsplit("\n", 1)[0] + "\n*(list truncated)*"

        await interaction.followup.send(
            embed=discord.Embed(title="Server Group Log Configurations", description=description, color=BLUE_COLOR),
            ephemeral=not visible,
        )

    # ====================================================================
    # Background Poller Loop
    # ====================================================================

    @tasks.loop(seconds=10.0)
    async def poll_logs_task(self):
        try:
            await asyncio.wait_for(self._poll_cycle(), timeout=120)
        except asyncio.TimeoutError:
            log.warning("Poll cycle took longer than 120s and was cancelled. Continuing.")
        except Exception:
            log.exception("Poll cycle failed. Will retry on the next tick.")
        finally:
            self.last_cycle_at = time.time()

    @poll_logs_task.before_loop
    async def before_poll_logs_task(self):
        await self.bot.wait_until_ready()
        self.last_cycle_at = time.time()

    @tasks.loop(seconds=60.0)
    async def watchdog_task(self):
        """Restarts the poll loop if it stopped or got stuck."""
        try:
            if not self.poll_logs_task.is_running():
                log.error("Poll loop was not running. Starting it again.")
                self.poll_logs_task.start()
            elif time.time() - self.last_cycle_at > 180:
                log.error("Poll loop looks stuck (no cycle for 180s). Restarting it.")
                self.poll_logs_task.restart()
        except Exception:
            log.exception("Watchdog failed")

    @watchdog_task.before_loop
    async def before_watchdog_task(self):
        await self.bot.wait_until_ready()
        await asyncio.sleep(90)

    async def _poll_cycle(self):
        db = get_db()
        if db is None:
            return

        await self._sync_usage(db)
        multiplier = self.guard.multiplier()
        await self._note_guard_state(db, multiplier)
        self.cycle_count += 1
        if multiplier is None:
            return  # Monthly bandwidth budget is used up: stay quiet until next month

        session = await self.get_http_session()
        configs = await self._get_configs(db)

        for cfg in configs:
            # One bad group must never stop the others
            try:
                await self._poll_one(db, session, cfg, multiplier)
            except Exception:
                log.exception("Polling failed for group %s", cfg.get("group_id"))

    async def _poll_one(self, db, session, cfg, multiplier: float = 1.0):
        group_id = cfg.get("group_id")
        group_name = cfg.get("group_name", "Roblox Group")
        channel_id = cfg.get("channel_id")
        mode = cfg.get("mode", "audit_log")
        min_rank_val = cfg.get("min_rank_value", 0)

        if not channel_id or not group_id:
            return

        channel = self.bot.get_channel(int(channel_id))
        if not channel:
            return

        # Trackers are per config (a group can be set up in more than one server)
        key = cfg["_id"]
        tracker = self.group_trackers.get(key)
        if tracker is None:
            tracker = self.group_trackers[key] = GroupTracker(group_id)

        if mode == "audit_log":
            # Throttle so many groups do not hammer the audit-log endpoint every 10s
            if time.time() - tracker.last_polled < AUDIT_POLL_INTERVAL * multiplier:
                return
            tracker.last_polled = time.time()
            await self.poll_audit_log_mode(db, session, cfg, channel, group_id, group_name, min_rank_val)
            return

        # Public polling: only when due, and never two at once for the same config
        if key in self.public_tasks or not tracker.is_due(multiplier):
            return

        tracker.last_polled = time.time()  # Claim the slot so the next tick does not start a duplicate
        task = asyncio.create_task(
            self.poll_public_mode(db, cfg, channel, group_id, group_name, min_rank_val, tracker)
        )
        self.public_tasks[key] = task  # Keep a reference so the task is not garbage collected
        task.add_done_callback(lambda t, k=key: self._on_public_task_done(k, t))

    def _on_public_task_done(self, key, task: asyncio.Task):
        self.public_tasks.pop(key, None)
        if task.cancelled():
            return
        exc = task.exception()
        if exc:
            log.error("Public poll task crashed", exc_info=exc)

    async def poll_audit_log_mode(self, db, session, cfg, channel, group_id, group_name, min_rank_val):
        url = f"https://groups.roblox.com/v1/groups/{group_id}/audit-log?limit=25&sortOrder=Desc"

        self._count_request()
        try:
            async with session.get(url) as resp:
                status = resp.status
                body = await resp.json() if status == 200 else None
        except Exception:
            log.warning("Audit log request failed for group %s", group_id, exc_info=True)
            return

        if status in (401, 403):
            log.warning("Audit log returned %s for group %s. Switching it to public polling.", status, group_id)
            await self._update_config(db, cfg, {"mode": "public_polling"}, unset=["last_audit_id"])
            return

        if status != 200 or not body:
            return

        logs = body.get("data", [])
        if not logs:
            return

        sync_key = (cfg.get("guild_id"), group_id)
        last_key = cfg.get("last_audit_id")
        latest_key = audit_entry_key(logs[0])

        # RE-DEPLOYMENT / FIRST RUN SYNC: set the watermark without logging old entries
        if sync_key not in self.initialized_groups or last_key is None:
            await self._update_config(db, cfg, {"last_audit_id": latest_key})
            self.initialized_groups.add(sync_key)
            return

        last_key = str(last_key)
        new_logs = []
        for item in logs:
            if audit_entry_key(item) == last_key:
                break
            new_logs.append(item)

        if not new_logs:
            return

        await self._update_config(db, cfg, {"last_audit_id": audit_entry_key(new_logs[0])})

        for entry in reversed(new_logs):
            try:
                await self.send_formatted_log(channel, entry, group_id, group_name, min_rank_val)
            except Exception:
                log.exception("Failed to format audit entry for group %s", group_id)

    async def poll_public_mode(self, db, cfg, channel, group_id, group_name, min_rank_val, tracker: GroupTracker):
        key = cfg["_id"]
        has_changes = False

        try:
            current_members, is_complete, truncated = await self._fetch_filtered_group_members(group_id, min_rank_val)

            # Failed or incomplete fetch: never compare, it would produce false leaves
            if not is_complete or not current_members:
                return

            entry = self.member_caches.get(key)
            cached_members: Dict[str, Dict[str, Any]] = entry["members"] if entry else {}

            # FIRST RUN / RESTART / RANK-FILTER CHANGE: silently build the baseline in memory
            if not entry or entry["min"] != min_rank_val or not cached_members:
                self.member_caches[key] = {"min": min_rank_val, "members": current_members}
                return

            now_fmt = f"<t:{int(datetime.now(timezone.utc).timestamp())}:F>"
            group_link = f"[{group_name}](https://www.roblox.com/groups/{group_id})"
            updated_cache = dict(cached_members)

            # 1. NEW JOINS & RANK CHANGES
            for uid, info in current_members.items():
                target_user = info.get("username") or f"User {uid}"
                target_link = user_link(target_user, uid)
                user_rank = info.get("rank", 0)
                role_name = info.get("role_name", "Unknown")

                if uid not in cached_members:
                    # In a truncated role, "new" members may just have slid into view
                    if role_name in truncated:
                        continue

                    has_changes = True
                    updated_cache[uid] = info
                    embed = self._make_embed(
                        f"An individual has **joined the group**\n\n{target_user} has **joined the group**",
                        GREEN_COLOR,
                        [("User", target_link), ("Group", group_link), ("Rank", role_name), ("\u200b", now_fmt)],
                    )
                    await self._send(channel, embed)
                    continue

                old_info = cached_members[uid]
                if old_info.get("role_name") == info.get("role_name"):
                    continue

                has_changes = True
                updated_cache[uid] = info

                if user_rank < old_info.get("rank", 0):
                    description = f"An individual has been **demoted**\n\n{target_user} has been **demoted**"
                    color = ORANGE_COLOR
                else:
                    description = f"An individual has been **promoted**\n\n{target_user} has been **promoted**"
                    color = GREEN_COLOR

                embed = self._make_embed(
                    description,
                    color,
                    [
                        ("User", target_link),
                        ("Group", group_link),
                        ("Old Rank", old_info.get("role_name", "Unknown")),
                        ("New Rank", role_name),
                        ("\u200b", now_fmt),
                    ],
                )
                await self._send(channel, embed)

            # 2. LEAVES / EXILES
            for uid, old_info in list(cached_members.items()):
                if uid in current_members:
                    continue
                if old_info.get("role_name") in truncated:
                    continue  # Probably just outside the visible window
                if old_info.get("rank", 0) < min_rank_val:
                    continue

                has_changes = True
                updated_cache.pop(uid, None)

                target_user = old_info.get("username") or f"User {uid}"
                embed = self._make_embed(
                    f"An individual has **left or been exiled**\n\n{target_user} has left the group",
                    RED_COLOR,
                    [
                        ("User", user_link(target_user, uid)),
                        ("Group", group_link),
                        ("Last Rank", old_info.get("role_name", "Unknown")),
                        ("\u200b", now_fmt),
                    ],
                )
                await self._send(channel, embed)

            # Remember the new roster in memory (nothing is written to the database)
            self.member_caches[key] = {"min": min_rank_val, "members": updated_cache}

        except Exception:
            log.exception("Public polling failed for group %s", group_id)
        finally:
            tracker.update_interval(has_changes)

    # ====================================================================
    # Audit-log embed formatting
    # ====================================================================

    async def send_formatted_log(self, channel, entry, group_id, group_name, min_rank_val: int = 0):
        actor_data = (entry.get("actor") or {}).get("user") or {}
        actor_name = actor_data.get("username", "System")
        actor_id = actor_data.get("userId")

        action_type = entry.get("actionType", "Audit Action")
        description = entry.get("description") or {}
        created_dt = entry.get("created")

        timestamp_fmt = "<t:0:F>"
        if created_dt:
            try:
                dt = datetime.fromisoformat(created_dt.replace("Z", "+00:00"))
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                timestamp_fmt = f"<t:{int(dt.timestamp())}:F>"
            except Exception:
                pass

        group_link = f"[{group_name}](https://www.roblox.com/groups/{group_id})"

        if action_type in ("Change Rank", "Promote Member", "Demote Member"):
            target_user = description.get("TargetHeader", description.get("TargetName", "Target User"))
            target_link = user_link(target_user, description.get("TargetId"))

            old_role = description.get("OldRoleSetHeader", description.get("OldRoleSetName", "Unknown"))
            new_role = description.get("NewRoleSetHeader", description.get("NewRoleSetName", "Unknown"))

            roles = await self._fetch_group_roles(group_id)
            old_rank = self._rank_for(roles, description.get("OldRoleSetId"), old_role)
            new_rank = self._rank_for(roles, description.get("NewRoleSetId"), new_role)

            if max(old_rank or 0, new_rank or 0) < min_rank_val:
                return

            ranks_known = old_rank is not None and new_rank is not None
            is_demotion = action_type == "Demote Member" or (ranks_known and new_rank < old_rank)
            is_promotion = action_type == "Promote Member" or (ranks_known and new_rank > old_rank)

            if is_demotion:
                text = f"An individual has been **demoted**\n\n{target_user} has been **demoted**"
                color = ORANGE_COLOR
            elif is_promotion:
                text = f"An individual has been **promoted**\n\n{target_user} has been **promoted**"
                color = GREEN_COLOR
            else:
                text = f"An individual's rank has been **changed**\n\n{target_user}'s rank has been **changed**"
                color = BLUE_COLOR

            embed = self._make_embed(
                text,
                color,
                [
                    ("User", target_link),
                    ("Group", group_link),
                    ("Old Rank", str(old_role)),
                    ("New Rank", str(new_role)),
                    ("\u200b", timestamp_fmt),
                ],
            )

        elif action_type in ("Accept Join Request", "Join Group"):
            role_name = description.get("NewRoleSetHeader", description.get("RoleSetName", "Member"))
            roles = await self._fetch_group_roles(group_id)
            role_rank = self._rank_for(roles, description.get("NewRoleSetId", description.get("RoleSetId")), role_name)

            if (role_rank or 0) < min_rank_val:
                return

            target_user = description.get("TargetHeader", description.get("TargetName", actor_name))
            target_link = user_link(target_user, description.get("TargetId", actor_id))

            embed = self._make_embed(
                f"An individual has **joined the group**\n\n{target_user} has **joined the group**",
                GREEN_COLOR,
                [("User", target_link), ("Group", group_link), ("Rank", str(role_name)), ("\u200b", timestamp_fmt)],
            )

        elif action_type in ("Remove Member", "Exile Member"):
            if min_rank_val > 0:
                return  # Cannot know the removed member's rank from the audit log

            target_user = description.get("TargetHeader", description.get("TargetName", "Target User"))
            target_link = user_link(target_user, description.get("TargetId"))

            embed = self._make_embed(
                f"An individual has been **exiled**\n\n{target_user} has been **exiled**",
                RED_COLOR,
                [("User", target_link), ("Group", group_link), ("\u200b", timestamp_fmt)],
            )

        else:
            if min_rank_val > 0:
                return

            embed = self._make_embed(
                f"An activity occurred in the group\n\n**Action**: `{action_type}`",
                BLUE_COLOR,
                [
                    ("Performer", user_link(actor_name, actor_id)),
                    ("Group", group_link),
                    ("\u200b", timestamp_fmt),
                ],
            )

        await self._send(channel, embed)


async def setup(bot: commands.Bot):
    await bot.add_cog(RobloxAuditLogger(bot))
