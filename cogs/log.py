import os
import asyncio
import aiohttp
import discord
from discord import app_commands
from discord.ext import commands, tasks
from datetime import datetime, timezone
from typing import Optional, Dict, Any, List

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


class RobloxAuditLogger(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.session: Optional[aiohttp.ClientSession] = None
        self.cycle_count: int = 0
        self.poll_logs_task.start()

    def cog_unload(self):
        self.poll_logs_task.cancel()

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

            self.session = aiohttp.ClientSession(headers=headers, cookies=cookies)
        return self.session

    async def _safe_fetch_json(self, url: str, retries: int = 3) -> Optional[Dict[str, Any]]:
        session = await self.get_http_session()
        for attempt in range(retries):
            try:
                async with session.get(url) as resp:
                    if resp.status == 200:
                        return await resp.json()
                    elif resp.status == 429:
                        await asyncio.sleep(2 ** attempt)
                        continue
                    else:
                        return None
            except Exception:
                if attempt == retries - 1:
                    return None
                await asyncio.sleep(1)
        return None

    async def _fetch_all_group_members(self, group_id: int) -> tuple[Dict[str, Dict[str, Any]], bool]:
        """Fetch group members. Returns (member_map, is_complete_success)."""
        roles_url = f"https://groups.roblox.com/v1/groups/{group_id}/roles"
        r_data = await self._safe_fetch_json(roles_url)
        if not r_data:
            return {}, False

        roles = r_data.get("roles", [])
        member_map = {}
        all_pages_succeeded = True

        for role in roles:
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

                m_data = await self._safe_fetch_json(members_url)
                if m_data is None:
                    # Mark batch incomplete so we abort public poll instead of spamming false joins/demotions
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

        return member_map, all_pages_succeeded

    # ====================================================================
    # Slash Commands
    # ====================================================================

    @app_commands.command(
        name="setup",
        description="Setup Roblox Group activity logging for a channel (Authorized Owners Only)."
    )
    @app_commands.describe(
        channel="The text channel where logs should be sent",
        group_id="The Roblox Group ID to track"
    )
    async def setup_cmd(
        self,
        interaction: discord.Interaction,
        channel: discord.TextChannel,
        group_id: int
    ):
        if not is_authorized_user(interaction.user.id):
            return await interaction.response.send_message(
                embed=discord.Embed(title="❌ Permission Denied", description="You are not authorized to use this command.", color=RED_COLOR),
                ephemeral=True
            )

        await interaction.response.defer(ephemeral=True)
        db = get_db()
        if db is None:
            return await interaction.followup.send(embed=discord.Embed(title="❌ Database Error", description="Could not connect to MongoDB Atlas.", color=RED_COLOR), ephemeral=True)

        try:
            group_url = f"https://groups.roblox.com/v1/groups/{group_id}"
            group_data = await self._safe_fetch_json(group_url)
            if not group_data:
                return await interaction.followup.send(embed=discord.Embed(title="❌ Invalid Roblox Group", description=f"Group ID `{group_id}` could not be found on Roblox.", color=RED_COLOR), ephemeral=True)

            group_name = group_data.get("name", f"Group {group_id}")

            # Check if Audit Logs are accessible
            audit_url = f"https://groups.roblox.com/v1/groups/{group_id}/audit-log?limit=1"
            session = await self.get_http_session()
            
            last_audit_id = None
            mode = "audit_log"

            async with session.get(audit_url) as resp:
                if resp.status == 200:
                    body = await resp.json()
                    logs = body.get("data", [])
                    if logs:
                        last_audit_id = logs[0].get("id")
                else:
                    mode = "public_polling"

            created_timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

            if mode == "public_polling":
                initial_member_cache, success = await self._fetch_all_group_members(group_id)
                if not success:
                    return await interaction.followup.send(
                        embed=discord.Embed(title="❌ Roblox API Error", description="Failed to fetch complete group member roster from Roblox.", color=RED_COLOR),
                        ephemeral=True
                    )

                await db["group_configs"].update_one(
                    {"guild_id": str(interaction.guild.id), "group_id": group_id},
                    {
                        "$set": {
                            "guild_id": str(interaction.guild.id),
                            "guild_name": interaction.guild.name,
                            "group_id": group_id,
                            "group_name": group_name,
                            "channel_id": str(channel.id),
                            "mode": mode,
                            "member_cache": initial_member_cache,
                            "created_at": created_timestamp
                        }
                    },
                    upsert=True
                )
            else:
                await db["group_configs"].update_one(
                    {"guild_id": str(interaction.guild.id), "group_id": group_id},
                    {
                        "$set": {
                            "guild_id": str(interaction.guild.id),
                            "guild_name": interaction.guild.name,
                            "group_id": group_id,
                            "group_name": group_name,
                            "channel_id": str(channel.id),
                            "mode": mode,
                            "last_audit_id": last_audit_id,
                            "created_at": created_timestamp
                        }
                    },
                    upsert=True
                )

            mode_msg = "Audit Log Stream (Direct Cookie)" if mode == "audit_log" else "Public Polling Stream (Cookie-Free Fallback)"

            embed = discord.Embed(
                title="✅ Roblox Group Logger Configured",
                description=(
                    f"**Group**: [{group_name}](https://www.roblox.com/groups/{group_id})\n"
                    f"**Group ID**: `{group_id}`\n"
                    f"**Channel**: {channel.mention}\n"
                    f"**Tracking Mode**: `{mode_msg}`"
                ),
                color=GREEN_COLOR
            )
            await interaction.followup.send(embed=embed, ephemeral=True)

        except Exception as e:
            await interaction.followup.send(
                embed=discord.Embed(title="❌ Setup Error", description=f"An unexpected error occurred during setup: `{str(e)}`", color=RED_COLOR),
                ephemeral=True
            )

    @app_commands.command(
        name="remove-setup",
        description="Remove a Roblox Group audit setup from this server (Authorized Owners Only)."
    )
    async def remove_setup_cmd(self, interaction: discord.Interaction, group_id: int):
        if not is_authorized_user(interaction.user.id):
            return await interaction.response.send_message(embed=discord.Embed(title="❌ Permission Denied", color=RED_COLOR), ephemeral=True)

        db = get_db()
        if db is None:
            return await interaction.response.send_message(embed=discord.Embed(title="❌ Database Error", color=RED_COLOR), ephemeral=True)

        res = await db["group_configs"].delete_one({"guild_id": str(interaction.guild.id), "group_id": group_id})
        if res.deleted_count > 0:
            await interaction.response.send_message(embed=discord.Embed(title="🗑️ Setup Removed", description=f"Stopped tracking Group ID `{group_id}`.", color=GREEN_COLOR), ephemeral=True)
        else:
            await interaction.response.send_message(embed=discord.Embed(title="❌ Not Found", description=f"No configuration for Group ID `{group_id}`.", color=RED_COLOR), ephemeral=True)

    @app_commands.command(
        name="list-setups",
        description="List all active group audit log configurations for this server (Authorized Owners Only)."
    )
    async def list_setups_cmd(self, interaction: discord.Interaction):
        if not is_authorized_user(interaction.user.id):
            return await interaction.response.send_message(embed=discord.Embed(title="❌ Permission Denied", color=RED_COLOR), ephemeral=True)

        db = get_db()
        if db is None:
            return await interaction.response.send_message(embed=discord.Embed(title="❌ Database Error", color=RED_COLOR), ephemeral=True)

        cursor = db["group_configs"].find({"guild_id": str(interaction.guild.id)})
        configs = await cursor.to_list(length=100)
        lines = []
        for doc in configs:
            g_name = doc.get("group_name", "Group")
            g_id = doc.get("group_id")
            ch_id = doc.get("channel_id")
            mode = doc.get("mode", "audit_log")
            lines.append(f"• [{g_name}](https://www.roblox.com/groups/{g_id}) (`{g_id}`) ➔ <#{ch_id}> [{mode}]")

        embed = discord.Embed(
            title="📋 Server Group Log Configurations",
            description="\n".join(lines) if lines else "*(No active group setups)*",
            color=BLUE_COLOR
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    # ====================================================================
    # Background Poller Loop
    # ====================================================================

    @tasks.loop(seconds=20.0)
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

                if not channel_id:
                    continue

                channel = self.bot.get_channel(int(channel_id))
                if not channel:
                    continue

                if mode == "audit_log":
                    await self.poll_audit_log_mode(db, session, cfg, channel, group_id, group_name)
                else:
                    # Public polling runs every 3rd loop cycle (~60s) to safeguard API limit
                    if self.cycle_count % 3 == 0:
                        await self.poll_public_mode(db, cfg, channel, group_id, group_name)

        except Exception:
            pass

    async def poll_audit_log_mode(self, db, session, cfg, channel, group_id, group_name):
        last_audit_id = cfg.get("last_audit_id")
        url = f"https://groups.roblox.com/v1/groups/{group_id}/audit-log?limit=5"
        try:
            async with session.get(url) as resp:
                if resp.status in [401, 403]:
                    # Switch to public fallback if cookie expires or fails
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

                await db["group_configs"].update_one({"_id": cfg["_id"]}, {"$set": {"last_audit_id": new_logs[0]["id"]}})

                for entry in reversed(new_logs):
                    await self.send_formatted_log(channel, entry, group_id, group_name)
        except Exception:
            pass

    async def poll_public_mode(self, db, cfg, channel, group_id, group_name):
        cached_members = cfg.get("member_cache", {})
        try:
            current_members, is_complete = await self._fetch_all_group_members(group_id)
            
            # Guard against partial API responses (prevents spam demote/promote/join events)
            if not is_complete or not current_members:
                return

            if not cached_members:
                await db["group_configs"].update_one({"_id": cfg["_id"]}, {"$set": {"member_cache": current_members}})
                return

            # Safety Threshold: If retrieved count drops by >15% unexpectedly, skip this cycle
            if len(cached_members) > 20 and len(current_members) < (len(cached_members) * 0.85):
                return

            now_fmt = f"<t:{int(datetime.now(timezone.utc).timestamp())}:F>"
            group_link = f"[{group_name}](https://www.roblox.com/groups/{group_id})"

            # 1. Check for Joins & Rank Changes
            for uid, info in current_members.items():
                target_user = info["username"]
                target_link = f"[{target_user}](https://www.roblox.com/users/{uid}/profile)"
                
                if uid not in cached_members:
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
                    if old_info["role_name"] != info["role_name"]:
                        old_rank_num = old_info.get("rank", 0)
                        new_rank_num = info.get("rank", 0)

                        if new_rank_num < old_rank_num:
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

            # 2. Check for Leaves / Exiles
            for uid, old_info in cached_members.items():
                if uid not in current_members:
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

    async def send_formatted_log(self, channel, entry, group_id, group_name):
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

        elif action_type in ["Accept Join Request", "Join Group"]:
            target_user = description.get("TargetHeader", description.get("TargetName", actor_name))
            target_id = description.get("TargetId", actor_id)
            target_link = f"[{target_user}](https://www.roblox.com/users/{target_id}/profile)" if target_id else f"**{target_user}**"

            role_name = description.get("NewRoleSetHeader", description.get("RoleSetName", "Member"))

            embed = discord.Embed(description=f"An individual has **joined the group**\n\n{target_user} has **joined the group**", color=GREEN_COLOR)
            embed.add_field(name="User", value=target_link, inline=False)
            embed.add_field(name="Group", value=group_link, inline=False)
            embed.add_field(name="Rank", value=role_name, inline=False)
            embed.add_field(name="\u200b", value=timestamp_fmt, inline=False)
            embed.set_footer(text=self.bot.user.name if self.bot.user else "TGE Logs Bot")

        elif action_type == "Exil Member":
            target_user = description.get("TargetHeader", description.get("TargetName", "Target User"))
            target_id = description.get("TargetId")
            target_link = f"[{target_user}](https://www.roblox.com/users/{target_id}/profile)" if target_id else f"**{target_user}**"

            embed = discord.Embed(description=f"An individual has been **exiled**\n\n{target_user} has been **exiled**", color=RED_COLOR)
            embed.add_field(name="User", value=target_link, inline=False)
            embed.add_field(name="Group", value=group_link, inline=False)
            embed.add_field(name="\u200b", value=timestamp_fmt, inline=False)
            embed.set_footer(text=self.bot.user.name if self.bot.user else "TGE Logs Bot")

        else:
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
