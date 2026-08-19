import os
import asyncio
import aiohttp
import discord
from discord import app_commands
from discord.ext import commands, tasks
from datetime import datetime
from typing import Optional, Dict

try:
    import pymongo
except ImportError:
    pymongo = None

# Authorized user IDs (You + Your Client)
AUTHORIZED_USER_IDS = [1219266886143967245, 1391931433521774742]

GREEN_COLOR = discord.Color.from_rgb(46, 139, 87)
ORANGE_COLOR = discord.Color.from_rgb(211, 84, 0)
RED_COLOR = discord.Color.from_rgb(192, 57, 43)
BLUE_COLOR = discord.Color.from_rgb(41, 128, 185)

_mongo_client = None

def get_db():
    global _mongo_client
    mongo_uri = os.getenv("MONGO_URI")
    if not mongo_uri or not pymongo:
        return None
    if _mongo_client is None:
        try:
            _mongo_client = pymongo.MongoClient(mongo_uri, serverSelectionTimeoutMS=2000)
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

        session = await self.get_http_session()

        group_url = f"https://groups.roblox.com/v1/groups/{group_id}"
        async with session.get(group_url) as resp:
            if resp.status != 200:
                return await interaction.followup.send(embed=discord.Embed(title="❌ Invalid Roblox Group", description=f"Group ID `{group_id}` does not exist on Roblox.", color=RED_COLOR), ephemeral=True)
            group_data = await resp.json()
            group_name = group_data.get("name", f"Group {group_id}")

        # Check if Audit Logs are accessible
        audit_url = f"https://groups.roblox.com/v1/groups/{group_id}/audit-log?limit=1"
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

        # Initialize public member cache if audit log mode is unavailable
        if mode == "public_polling":
            roles_url = f"https://groups.roblox.com/v1/groups/{group_id}/roles"
            initial_member_cache = {}
            async with session.get(roles_url) as r_resp:
                if r_resp.status == 200:
                    r_data = await r_resp.json()
                    roles = r_data.get("roles", [])
                    for role in roles:
                        role_id = role.get("id")
                        role_name = role.get("name")
                        role_rank = role.get("rank")
                        members_url = f"https://groups.roblox.com/v1/groups/{group_id}/roles/{role_id}/users?limit=100&sortOrder=Desc"
                        async with session.get(members_url) as m_resp:
                            if m_resp.status == 200:
                                m_data = await m_resp.json()
                                for u in m_data.get("data", []):
                                    uid = str(u.get("userId"))
                                    initial_member_cache[uid] = {
                                        "username": u.get("username"),
                                        "role_name": role_name,
                                        "rank": role_rank
                                    }

            db["group_configs"].update_one(
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
                        "created_at": datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")
                    }
                },
                upsert=True
            )
        else:
            db["group_configs"].update_one(
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
                        "created_at": datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")
                    }
                },
                upsert=True
            )

        mode_msg = "Audit Log Stream (Direct)" if mode == "audit_log" else "Public Polling Stream (Cookie-Free Fallback)"

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

        res = db["group_configs"].delete_one({"guild_id": str(interaction.guild.id), "group_id": group_id})
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
        lines = []
        for doc in cursor:
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
        db = get_db()
        if db is None:
            return

        session = await self.get_http_session()
        configs = list(db["group_configs"].find())

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
                await self.poll_public_mode(db, session, cfg, channel, group_id, group_name)

    async def poll_audit_log_mode(self, db, session, cfg, channel, group_id, group_name):
        last_audit_id = cfg.get("last_audit_id")
        url = f"https://groups.roblox.com/v1/groups/{group_id}/audit-log?limit=5"
        try:
            async with session.get(url) as resp:
                if resp.status in [401, 403]:
                    # Switch to public fallback if audit log access revokes
                    db["group_configs"].update_one({"_id": cfg["_id"]}, {"$set": {"mode": "public_polling"}})
                    return

                if resp.status != 200:
                    return

                body = await resp.json()
                logs = body.get("data", [])
                if not logs:
                    return

                if last_audit_id is None:
                    db["group_configs"].update_one({"_id": cfg["_id"]}, {"$set": {"last_audit_id": logs[0].get("id")}})
                    return

                new_logs = []
                for item in logs:
                    if item.get("id") == last_audit_id:
                        break
                    new_logs.append(item)

                if not new_logs:
                    return

                db["group_configs"].update_one({"_id": cfg["_id"]}, {"$set": {"last_audit_id": new_logs[0]["id"]}})

                for entry in reversed(new_logs):
                    await self.send_formatted_log(channel, entry, group_id, group_name)
        except Exception:
            pass

    async def poll_public_mode(self, db, session, cfg, channel, group_id, group_name):
        cached_members = cfg.get("member_cache", {})
        roles_url = f"https://groups.roblox.com/v1/groups/{group_id}/roles"
        current_members = {}

        try:
            async with session.get(roles_url) as resp:
                if resp.status != 200:
                    return
                r_data = await resp.json()
                roles = r_data.get("roles", [])

                for role in roles:
                    role_id = role.get("id")
                    role_name = role.get("name")
                    role_rank = role.get("rank")
                    members_url = f"https://groups.roblox.com/v1/groups/{group_id}/roles/{role_id}/users?limit=100&sortOrder=Desc"
                    async with session.get(members_url) as m_resp:
                        if m_resp.status == 200:
                            m_data = await m_resp.json()
                            for u in m_data.get("data", []):
                                uid = str(u.get("userId"))
                                current_members[uid] = {
                                    "username": u.get("username"),
                                    "role_name": role_name,
                                    "rank": role_rank
                                }

            if not cached_members:
                db["group_configs"].update_one({"_id": cfg["_id"]}, {"$set": {"member_cache": current_members}})
                return

            # Detect Changes
            now_fmt = f"<t:{int(datetime.utcnow().timestamp())}:F>"
            group_link = f"[{group_name}](https://www.roblox.com/groups/{group_id})"

            for uid, info in current_members.items():
                target_user = info["username"]
                target_link = f"[{target_user}](https://www.roblox.com/users/{uid}/profile)"
                
                if uid not in cached_members:
                    # Joined group
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

            db["group_configs"].update_one({"_id": cfg["_id"]}, {"$set": {"member_cache": current_members}})

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

