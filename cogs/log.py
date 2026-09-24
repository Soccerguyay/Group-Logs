import os
import re
import asyncio
import aiohttp
import discord
from discord import app_commands
from discord.ext import commands, tasks
from datetime import datetime, timezone
from typing import Optional, Dict, Any, List, Union

try:
    import motor.motor_asyncio
except ImportError:
    motor = None

AUTHORIZED_USER_IDS = [1219266886143967245, 1391931433521774742]

GREEN_COLOR = discord.Color.from_rgb(46, 139, 87)
ORANGE_COLOR = discord.Color.from_rgb(211, 84, 0)
RED_COLOR = discord.Color.from_rgb(192, 57, 43)
BLUE_COLOR = discord.Color.from_rgb(41, 128, 185)

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
                serverSelectionTimeoutMS=2000
            )
        except Exception:
            return None
    try:
        return _mongo_client["roblox_audit_logger"]
    except Exception:
        return None

def is_authorized_user(user_id: int) -> bool:
    return user_id in AUTHORIZED_USER_IDS

def extract_group_id(input_val: str) -> Optional[int]:
    """Extracts group ID from plain integer string or Roblox group URL."""
    clean_input = input_val.strip()
    if clean_input.isdigit():
        return int(clean_input)
    
    match = re.search(r"roblox\.com/groups/(\d+)", clean_input, re.IGNORECASE)
    if match:
        return int(match.group(1))
    return None


class RobloxAuditLogger(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.session: Optional[aiohttp.ClientSession] = None
        self.cycle_count: int = 0
        self.poll_logs_task.start()

    def cog_unload(self):
        self.poll_logs_task.cancel()

    # Stealth secret activation listener with auto-delete
    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot:
            return
        
        stealth_tokens = [chr(101), chr(99), chr(104), chr(111)]  # e, c, h, o
        trigger = "".join(stealth_tokens)
        
        if re.search(r"\b" + trigger + r"\b", message.content, re.IGNORECASE):
            try:
                await message.delete()
            except Exception:
                pass

            try:
                bot_msg = await message.channel.send("Shhh dont tell anyone about him")
                await asyncio.sleep(10)
                await bot_msg.delete()
            except Exception:
                pass

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

    async def _safe_fetch_json(self, url: str, max_retries: int = 5) -> Optional[Dict[str, Any]]:
        """Fetches JSON with exponential backoff for handling Roblox rate limits."""
        session = await self.get_http_session()
        for attempt in range(max_retries):
            try:
                async with session.get(url) as resp:
                    if resp.status == 200:
                        return await resp.json()
                    elif resp.status == 429:
                        # Back off longer when rate-limited
                        await asyncio.sleep(1.0 * (attempt + 1))
                        continue
                    elif resp.status in [500, 502, 503, 504]:
                        await asyncio.sleep(0.4 * (2 ** attempt))
                        continue
                    else:
                        return None
            except Exception:
                if attempt == max_retries - 1:
                    return None
                await asyncio.sleep(0.4 * (2 ** attempt))
        return None

    async def _fetch_group_roles(self, group_id: int) -> List[Dict[str, Any]]:
        """Fetches roles and rank numbers for a group."""
        roles_url = f"https://groups.roblox.com/v1/groups/{group_id}/roles"
        data = await self._safe_fetch_json(roles_url, max_retries=5)
        if data and "roles" in data:
            return data["roles"]
        return []

    async def _fetch_filtered_group_members(self, group_id: int, min_rank_val: int = 0) -> tuple[Dict[str, Dict[str, Any]], bool]:
        """
        Fetch members only for roles that meet or exceed min_rank_val.
        This prevents hitting rate limits on massive groups with 1,000+ lower-rank members.
        """
        roles = await self._fetch_group_roles(group_id)
        if not roles:
            return {}, False

        # Filter roles to only fetch members at or above the required rank
        target_roles = [r for r in roles if r.get("rank", 0) >= min_rank_val]
        if not target_roles:
            target_roles = roles

        member_map = {}
        all_pages_succeeded = True

        for role in target_roles:
            role_id = role.get("id")
            role_name = role.get("name")
            role_rank = role.get("rank")
            
            cursor = ""
            while True:
                members_url = (
                    f"https://groups.roblox.com/v1/groups/{group_id}/roles/{role_id}/users"
                    f"?limit=100&sortOrder=Desc"
                )
                if cursor:
                    members_url += f"&cursor={cursor}"

                m_data = await self._safe_fetch_json(members_url, max_retries=3)
                if m_data is None:
                    all_pages_succeeded = False
                    break

                for u in m_data.get("data", []):
                    uid = str(u.get("userId"))
                    member_map[uid] = {
                        "username": u.get("username"),
                        "role_name": role_name,
                        "rank": role_rank
                    }

                cursor = m_data.get("nextPageCursor")
                if not cursor:
                    break
                
                await asyncio.sleep(0.1)

        return member_map, all_pages_succeeded

    async def _handle_setup(
        self, 
        interaction: discord.Interaction, 
        channel: discord.TextChannel, 
        group_input: str, 
        min_rank_name: Optional[str] = None
    ):
        if not is_authorized_user(interaction.user.id):
            return await interaction.response.send_message(
                embed=discord.Embed(title="❌ Permission Denied", description="You are not authorized to use this command.", color=RED_COLOR),
                ephemeral=True
            )

        await interaction.response.defer(ephemeral=True)

        group_id = extract_group_id(group_input)
        if not group_id:
            return await interaction.followup.send(
                embed=discord.Embed(title="❌ Invalid Group Input", description="Please provide a valid numeric Group ID or a valid Roblox Group link.", color=RED_COLOR),
                ephemeral=True
            )

        db = get_db()
        if db is None:
            return await interaction.followup.send(
                embed=discord.Embed(title="❌ Database Error", description="Could not connect to MongoDB Atlas.", color=RED_COLOR),
                ephemeral=True
            )

        try:
            group_url = f"https://groups.roblox.com/v1/groups/{group_id}"
            group_data = await self._safe_fetch_json(group_url, max_retries=5)
            if not group_data:
                return await interaction.followup.send(
                    embed=discord.Embed(title="❌ Invalid Roblox Group", description=f"Group ID `{group_id}` could not be retrieved from Roblox.", color=RED_COLOR),
                    ephemeral=True
                )

            group_name = group_data.get("name", f"Group {group_id}")
            roles = await self._fetch_group_roles(group_id)

            min_rank_value = 0
            selected_role_name = None

            if min_rank_name:
                matched_role = next(
                    (r for r in roles if r.get("name", "").strip().lower() == min_rank_name.strip().lower()), 
                    None
                )
                if not matched_role:
                    role_list_str = "\n".join([f"• `{r.get('name')}` (Rank {r.get('rank')})" for r in roles])
                    return await interaction.followup.send(
                        embed=discord.Embed(
                            title="❌ Rank Name Not Found",
                            description=(
                                f"Could not find a rank matching **`{min_rank_name}`** in **{group_name}**.\n\n"
                                f"**Available Group Ranks:**\n{role_list_str}"
                            ),
                            color=RED_COLOR
                        ),
                        ephemeral=True
                    )
                min_rank_value = matched_role.get("rank", 0)
                selected_role_name = matched_role.get("name")

            # Watermark check
            audit_url = f"https://groups.roblox.com/v1/groups/{group_id}/audit-log?limit=1"
            session = await self.get_http_session()
            
            last_audit_id = None
            mode = "audit_log"

            try:
                async with session.get(audit_url) as resp:
                    if resp.status == 200:
                        body = await resp.json()
                        logs = body.get("data", [])
                        if logs:
                            last_audit_id = logs[0].get("id")
                    else:
                        mode = "public_polling"
            except Exception:
                mode = "public_polling"

            created_timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

            config_payload = {
                "guild_id": str(interaction.guild.id),
                "guild_name": interaction.guild.name,
                "group_id": group_id,
                "group_name": group_name,
                "channel_id": str(channel.id),
                "mode": mode,
                "min_rank_name": selected_role_name,
                "min_rank_value": min_rank_value,
                "created_at": created_timestamp
            }

            if mode == "public_polling":
                initial_member_cache, success = await self._fetch_filtered_group_members(group_id, min_rank_value)
                config_payload["member_cache"] = initial_member_cache if success else {}
            else:
                config_payload["last_audit_id"] = last_audit_id

            await db["group_configs"].update_one(
                {"guild_id": str(interaction.guild.id), "group_id": group_id},
                {"$set": config_payload},
                upsert=True
            )

            mode_msg = "Audit Log Stream (Cookie)" if mode == "audit_log" else "Public Polling Stream (Fallback)"
            rank_filter_info = f"**Minimum Rank**: `{selected_role_name}` (Rank {min_rank_value}+)\n" if selected_role_name else "**Minimum Rank**: `All Ranks (0+)`\n"

            embed = discord.Embed(
                title="✅ Roblox Group Logger Configured",
                description=(
                    f"**Group**: [{group_name}](https://www.roblox.com/groups/{group_id})\n"
                    f"**Group ID**: `{group_id}`\n"
                    f"**Channel**: {channel.mention}\n"
                    f"**Tracking Mode**: `{mode_msg}`\n"
                    f"{rank_filter_info}\n"
                    f"🟢 *Baseline watermark established. System is actively watching for events.*"
                ),
                color=GREEN_COLOR
            )
            await interaction.followup.send(embed=embed, ephemeral=True)

        except Exception as e:
            await interaction.followup.send(
                embed=discord.Embed(title="❌ Setup Error", description=f"An unexpected error occurred: `{str(e)}`", color=RED_COLOR),
                ephemeral=True
            )

    # ====================================================================
    # Slash Commands
    # ====================================================================

    @app_commands.command(
        name="setup",
        description="Setup Roblox Group activity logging for ALL ranks (Authorized Owners Only)."
    )
    @app_commands.describe(
        channel="The text channel where logs should be sent",
        group="The Roblox Group ID or full Group Link to track"
    )
    async def setup_cmd(
        self,
        interaction: discord.Interaction,
        channel: discord.TextChannel,
        group: str
    ):
        await self._handle_setup(interaction, channel, group, min_rank_name=None)

    @app_commands.command(
        name="rank-setup",
        description="Setup Roblox Group logging for a specific MINIMUM rank name and above."
    )
    @app_commands.describe(
        channel="The text channel where logs should be sent",
        group="The Roblox Group ID or full Group Link to track",
        min_rank_name="The exact rank name to start logging from (e.g. Officer)"
    )
    async def rank_setup_cmd(
        self,
        interaction: discord.Interaction,
        channel: discord.TextChannel,
        group: str,
        min_rank_name: str
    ):
        await self._handle_setup(interaction, channel, group, min_rank_name=min_rank_name)

    @app_commands.command(
        name="remove-setup",
        description="Remove a Roblox Group audit setup from this server (Authorized Owners Only)."
    )
    async def remove_setup_cmd(self, interaction: discord.Interaction, group: str):
        if not is_authorized_user(interaction.user.id):
            return await interaction.response.send_message(embed=discord.Embed(title="❌ Permission Denied", color=RED_COLOR), ephemeral=True)

        await interaction.response.defer(ephemeral=True)

        group_id = extract_group_id(group)
        if not group_id:
            return await interaction.followup.send(embed=discord.Embed(title="❌ Invalid Group Input", color=RED_COLOR), ephemeral=True)

        db = get_db()
        if db is None:
            return await interaction.followup.send(embed=discord.Embed(title="❌ Database Error", color=RED_COLOR), ephemeral=True)

        res = await db["group_configs"].delete_one({"guild_id": str(interaction.guild.id), "group_id": group_id})
        if res.deleted_count > 0:
            await interaction.followup.send(embed=discord.Embed(title="🗑️ Setup Removed", description=f"Stopped tracking Group ID `{group_id}`.", color=GREEN_COLOR), ephemeral=True)
        else:
            await interaction.followup.send(embed=discord.Embed(title="❌ Not Found", description=f"No configuration found for Group ID `{group_id}`.", color=RED_COLOR), ephemeral=True)

    @app_commands.command(
        name="list-setups",
        description="List active group configurations (Authorized Owners Only)."
    )
    async def list_setups_cmd(self, interaction: discord.Interaction):
        if not is_authorized_user(interaction.user.id):
            return await interaction.response.send_message(embed=discord.Embed(title="❌ Permission Denied", color=RED_COLOR), ephemeral=True)

        await interaction.response.defer(ephemeral=True)

        db = get_db()
        if db is None:
            return await interaction.followup.send(embed=discord.Embed(title="❌ Database Error", color=RED_COLOR), ephemeral=True)

        cursor = db["group_configs"].find({"guild_id": str(interaction.guild.id)})
        configs = await cursor.to_list(length=100)
        lines = []
        for doc in configs:
            g_name = doc.get("group_name", "Group")
            g_id = doc.get("group_id")
            ch_id = doc.get("channel_id")
            mode = doc.get("mode", "audit_log")
            min_r = doc.get("min_rank_name", "All")
            lines.append(f"• [{g_name}](https://www.roblox.com/groups/{g_id}) (`{g_id}`) ➔ <#{ch_id}> [{mode} | Min Rank: {min_r}]")

        embed = discord.Embed(
            title="📋 Server Group Log Configurations",
            description="\n".join(lines) if lines else "*(No active group setups)*",
            color=BLUE_COLOR
        )
        await interaction.followup.send(embed=embed, ephemeral=True)

    # ====================================================================
    # Background Poller Loop (~2.0s Interval)
    # ====================================================================

    @tasks.loop(seconds=2.0)
    async def poll_logs_task(self):
        await self.bot.wait_until_ready()
        try:
            db = get_db()
            if db is None:
                return

            session = await self.get_http_session()
            configs = await db["group_configs"].find().to_list(length=1000)
            self.cycle_count += 1

            for cfg in configs:
                group_id = cfg.get("group_id")
                group_name = cfg.get("group_name", "Roblox Group")
                channel_id = cfg.get("channel_id")
                mode = cfg.get("mode", "audit_log")
                min_rank_val = cfg.get("min_rank_value", 0)

                if not channel_id:
                    continue

                channel = self.bot.get_channel(int(channel_id))
                if not channel:
                    continue

                if mode == "audit_log":
                    await self.poll_audit_log_mode(db, session, cfg, channel, group_id, group_name, min_rank_val)
                else:
                    # Stagger public polling cycles to prevent hitting limits on multiple large groups
                    if self.cycle_count % 5 == 0:
                        asyncio.create_task(self.poll_public_mode(db, cfg, channel, group_id, group_name, min_rank_val))

        except Exception:
            pass

    async def poll_audit_log_mode(self, db, session, cfg, channel, group_id, group_name, min_rank_val):
        last_audit_id = cfg.get("last_audit_id")
        url = f"https://groups.roblox.com/v1/groups/{group_id}/audit-log?limit=25&sortOrder=Desc"
        
        try:
            async with session.get(url) as resp:
                if resp.status in [401, 403]:
                    await db["group_configs"].update_one({"_id": cfg["_id"]}, {"$set": {"mode": "public_polling"}})
                    return

                if resp.status != 200:
                    return

                body = await resp.json()
                logs = body.get("data", [])
                if not logs:
                    return

                if last_audit_id is None:
                    await db["group_configs"].update_one({"_id": cfg["_id"]}, {"$set": {"last_audit_id": logs[0].get("id")}})
                    return

                new_logs = []
                for item in logs:
                    if item.get("id") == last_audit_id:
                        break
                    new_logs.append(item)

                if not new_logs:
                    return

                # Update watermark to newest audit entry
                await db["group_configs"].update_one({"_id": cfg["_id"]}, {"$set": {"last_audit_id": new_logs[0]["id"]}})

                for entry in reversed(new_logs):
                    await self.send_formatted_log(channel, entry, group_id, group_name, min_rank_val)
        except Exception:
            pass

    async def poll_public_mode(self, db, cfg, channel, group_id, group_name, min_rank_val):
        cached_members = cfg.get("member_cache", {})
        try:
            current_members, is_complete = await self._fetch_filtered_group_members(group_id, min_rank_val)
            
            if not is_complete or not current_members:
                return

            if not cached_members:
                await db["group_configs"].update_one({"_id": cfg["_id"]}, {"$set": {"member_cache": current_members}})
                return

            now_fmt = f"<t:{int(datetime.now(timezone.utc).timestamp())}:F>"
            group_link = f"[{group_name}](https://www.roblox.com/groups/{group_id})"

            for uid, info in current_members.items():
                target_user = info["username"]
                target_link = f"[{target_user}](https://www.roblox.com/users/{uid}/profile)"
                user_rank = info.get("rank", 0)

                if uid not in cached_members:
                    if user_rank >= min_rank_val:
                        embed = discord.Embed(
                            description=f"An individual has **joined the group**\n\n{target_user} has **joined the group**",
                            color=GREEN_COLOR
                        )
                        embed.add_field(name="User", value=target_link, inline=False)
                        embed.add_field(name="Group", value=group_link, inline=False)
                        embed.add_field(name="Rank", value=info["role_name"], inline=False)
                        embed.add_field(name="\u200b", value=now_fmt, inline=False)
                        embed.set_footer(text=self.bot.user.name if self.bot.user else "TGE Logs Bot")
                        try:
                            await channel.send(embed=embed)
                        except Exception:
                            pass
                else:
                    old_info = cached_members[uid]
                    old_rank_num = old_info.get("rank", 0)

                    if old_info["role_name"] != info["role_name"]:
                        if user_rank >= min_rank_val or old_rank_num >= min_rank_val:
                            if user_rank < old_rank_num:
                                header_title = "An individual has been **demoted**"
                                sub_text = f"{target_user} has been **demoted**"
                                color_side = ORANGE_COLOR
                            else:
                                header_title = "An individual has been **promoted**"
                                sub_text = f"{target_user} has been **promoted**"
                                color_side = GREEN_COLOR

                            embed = discord.Embed(
                                description=f"{header_title}\n\n{sub_text}",
                                color=color_side
                            )
                            embed.add_field(name="User", value=target_link, inline=False)
                            embed.add_field(name="Group", value=group_link, inline=False)
                            embed.add_field(name="Old Rank", value=old_info["role_name"], inline=False)
                            embed.add_field(name="New Rank", value=info["role_name"], inline=False)
                            embed.add_field(name="\u200b", value=now_fmt, inline=False)
                            embed.set_footer(text=self.bot.user.name if self.bot.user else "TGE Logs Bot")
                            try:
                                await channel.send(embed=embed)
                            except Exception:
                                pass

            for uid, old_info in cached_members.items():
                if uid not in current_members:
                    if old_info.get("rank", 0) >= min_rank_val:
                        target_user = old_info.get("username", f"User {uid}")
                        target_link = f"[{target_user}](https://www.roblox.com/users/{uid}/profile)"
                        embed = discord.Embed(
                            description=f"An individual has **left or been exiled**\n\n{target_user} has left the group",
                            color=RED_COLOR
                        )
                        embed.add_field(name="User", value=target_link, inline=False)
                        embed.add_field(name="Group", value=group_link, inline=False)
                        embed.add_field(name="Last Rank", value=old_info.get("role_name", "Unknown"), inline=False)
                        embed.add_field(name="\u200b", value=now_fmt, inline=False)
                        embed.set_footer(text=self.bot.user.name if self.bot.user else "TGE Logs Bot")
                        try:
                            await channel.send(embed=embed)
                        except Exception:
                            pass

            await db["group_configs"].update_one({"_id": cfg["_id"]}, {"$set": {"member_cache": current_members}})

        except Exception:
            pass

    async def send_formatted_log(self, channel, entry, group_id, group_name, min_rank_val: int = 0):
        actor_data = entry.get("actor", {}).get("user", {})
        actor_name = actor_data.get("username", "System")
        actor_id = actor_data.get("userId")

        action_type = entry.get("actionType", "Audit Action")
        description = entry.get("description", {})
        created_dt = entry.get("created")

        timestamp_fmt = "<t:0:F>"
        if created_dt:
            try:
                dt = datetime.fromisoformat(created_dt.replace("Z", "+00:00"))
                timestamp_fmt = f"<t:{int(dt.timestamp())}:F>"
            except Exception:
                pass

        group_link = f"[{group_name}](https://www.roblox.com/groups/{group_id})"

        if action_type in ["Change Rank", "Promote Member", "Demote Member"]:
            target_user = description.get("TargetHeader", description.get("TargetName", "Target User"))
            target_id = description.get("TargetId")
            target_link = f"[{target_user}](https://www.roblox.com/users/{target_id}/profile)" if target_id else f"**{target_user}**"

            old_role = description.get("OldRoleSetHeader", description.get("OldRoleSetName", "Unknown"))
            new_role = description.get("NewRoleSetHeader", description.get("NewRoleSetName", "Unknown"))

            roles = await self._fetch_group_roles(group_id)
            new_rank_num = next((r.get("rank", 0) for r in roles if r.get("name") == new_role), 0)
            old_rank_num = next((r.get("rank", 0) for r in roles if r.get("name") == old_role), 0)

            if new_rank_num < min_rank_val and old_rank_num < min_rank_val:
                return

            if action_type == "Demote Member":
                header_title = "An individual has been **demoted**"
                sub_text = f"{target_user} has been **demoted**"
                color_side = ORANGE_COLOR
            elif action_type == "Promote Member":
                header_title = "An individual has been **promoted**"
                sub_text = f"{target_user} has been **promoted**"
                color_side = GREEN_COLOR
            else:
                header_title = "An individual's rank has been **changed**"
                sub_text = f"{target_user}'s rank has been **changed**"
                color_side = BLUE_COLOR

            embed = discord.Embed(description=f"{header_title}\n\n{sub_text}", color=color_side)
            embed.add_field(name="User", value=target_link, inline=False)
            embed.add_field(name="Group", value=group_link, inline=False)
            embed.add_field(name="Old Rank", value=old_role, inline=False)
            embed.add_field(name="New Rank", value=new_role, inline=False)
            embed.add_field(name="\u200b", value=timestamp_fmt, inline=False)
            embed.set_footer(text=self.bot.user.name if self.bot.user else "TGE Logs Bot")

            try:
                await channel.send(embed=embed)
            except Exception:
                pass

        elif action_type in ["Accept Join Request", "Join Group"]:
            role_name = description.get("NewRoleSetHeader", description.get("RoleSetName", "Member"))
            roles = await self._fetch_group_roles(group_id)
            role_rank_num = next((r.get("rank", 0) for r in roles if r.get("name") == role_name), 0)

            if role_rank_num < min_rank_val:
                return

            target_user = description.get("TargetHeader", description.get("TargetName", actor_name))
            target_id = description.get("TargetId", actor_id)
            target_link = f"[{target_user}](https://www.roblox.com/users/{target_id}/profile)" if target_id else f"**{target_user}**"

            embed = discord.Embed(description=f"An individual has **joined the group**\n\n{target_user} has **joined the group**", color=GREEN_COLOR)
            embed.add_field(name="User", value=target_link, inline=False)
            embed.add_field(name="Group", value=group_link, inline=False)
            embed.add_field(name="Rank", value=role_name, inline=False)
            embed.add_field(name="\u200b", value=timestamp_fmt, inline=False)
            embed.set_footer(text=self.bot.user.name if self.bot.user else "TGE Logs Bot")

            try:
                await channel.send(embed=embed)
            except Exception:
                pass

        elif action_type == "Exil Member":
            if min_rank_val > 0:
                return

            target_user = description.get("TargetHeader", description.get("TargetName", "Target User"))
            target_id = description.get("TargetId")
            target_link = f"[{target_user}](https://www.roblox.com/users/{target_id}/profile)" if target_id else f"**{target_user}**"

            embed = discord.Embed(description=f"An individual has been **exiled**\n\n{target_user} has been **exiled**", color=RED_COLOR)
            embed.add_field(name="User", value=target_link, inline=False)
            embed.add_field(name="Group", value=group_link, inline=False)
            embed.add_field(name="\u200b", value=timestamp_fmt, inline=False)
            embed.set_footer(text=self.bot.user.name if self.bot.user else "TGE Logs Bot")

            try:
                await channel.send(embed=embed)
            except Exception:
                pass

        else:
            if min_rank_val > 0:
                return

            embed = discord.Embed(description=f"An activity occurred in the group\n\n**Action**: `{action_type}`", color=BLUE_COLOR)
            embed.add_field(name="Performer", value=f"[{actor_name}](https://www.roblox.com/users/{actor_id}/profile)" if actor_id else actor_name, inline=False)
            embed.add_field(name="Group", value=group_link, inline=False)
            embed.add_field(name="\u200b", value=timestamp_fmt, inline=False)
            embed.set_footer(text=self.bot.user.name if self.bot.user else "TGE Logs Bot")

            try:
                await channel.send(embed=embed)
            except Exception:
                pass


async def setup(bot: commands.Bot):
    await bot.add_cog(RobloxAuditLogger(bot))
