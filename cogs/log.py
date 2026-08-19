import os
import asyncio
import aiohttp
import discord
from discord import app_commands
from discord.ext import commands, tasks
from datetime import datetime
from typing import Optional

try:
    import pymongo
except ImportError:
    pymongo = None

# Authorized user IDs (You + Your Client)
AUTHORIZED_USER_IDS = [1219266886143967245, 1391931433521774742]

EMBED_COLOR = discord.Color.blue()
ERROR_COLOR = discord.Color.red()
SUCCESS_COLOR = discord.Color.green()

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
            self.session = aiohttp.ClientSession()
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
                embed=discord.Embed(
                    title="❌ Permission Denied",
                    description="You are not authorized to use this command.",
                    color=ERROR_COLOR
                ),
                ephemeral=True
            )

        await interaction.response.defer(ephemeral=True)
        db = get_db()
        if db is None:
            return await interaction.followup.send(
                embed=discord.Embed(
                    title="❌ Database Error",
                    description="Could not connect to MongoDB Atlas.",
                    color=ERROR_COLOR
                ),
                ephemeral=True
            )

        session = await self.get_http_session()

        # Fetch Roblox Group Info
        group_url = f"https://groups.roblox.com/v1/groups/{group_id}"
        async with session.get(group_url) as resp:
            if resp.status != 200:
                return await interaction.followup.send(
                    embed=discord.Embed(
                        title="❌ Invalid Roblox Group",
                        description=f"Group ID `{group_id}` does not exist on Roblox.",
                        color=ERROR_COLOR
                    ),
                    ephemeral=True
                )
            group_data = await resp.json()
            group_name = group_data.get("name", f"Group {group_id}")

        # Fetch latest Audit Log ID so historical logs aren't spammed
        audit_url = f"https://groups.roblox.com/v1/groups/{group_id}/audit-logs?limit=1"
        last_audit_id = None
        async with session.get(audit_url) as resp:
            if resp.status == 200:
                body = await resp.json()
                logs = body.get("data", [])
                if logs:
                    last_audit_id = logs[0].get("id")

        # Save configuration
        db["group_configs"].update_one(
            {"guild_id": str(interaction.guild.id), "group_id": group_id},
            {
                "$set": {
                    "guild_id": str(interaction.guild.id),
                    "guild_name": interaction.guild.name,
                    "group_id": group_id,
                    "group_name": group_name,
                    "channel_id": str(channel.id),
                    "last_audit_id": last_audit_id,
                    "created_at": datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")
                }
            },
            upsert=True
        )

        embed = discord.Embed(
            title="✅ Roblox Group Logger Configured",
            description=(
                f"**Roblox Group**: [{group_name}](https://www.roblox.com/groups/{group_id})\n"
                f"**Group ID**: `{group_id}`\n"
                f"**Logging Channel**: {channel.mention}\n"
                f"**Server**: `{interaction.guild.name}`"
            ),
            color=SUCCESS_COLOR
        )
        await interaction.followup.send(embed=embed, ephemeral=True)

    @app_commands.command(
        name="remove-setup",
        description="Remove a Roblox Group audit setup from this server (Authorized Owners Only)."
    )
    async def remove_setup_cmd(self, interaction: discord.Interaction, group_id: int):
        if not is_authorized_user(interaction.user.id):
            return await interaction.response.send_message(
                embed=discord.Embed(title="❌ Permission Denied", description="You are not authorized to run this command.", color=ERROR_COLOR),
                ephemeral=True
            )

        db = get_db()
        if db is None:
            return await interaction.response.send_message(embed=discord.Embed(title="❌ Database Error", color=ERROR_COLOR), ephemeral=True)

        res = db["group_configs"].delete_one({"guild_id": str(interaction.guild.id), "group_id": group_id})
        if res.deleted_count > 0:
            await interaction.response.send_message(embed=discord.Embed(title="🗑️ Setup Removed", description=f"Stopped tracking Group ID `{group_id}` for this server.", color=SUCCESS_COLOR), ephemeral=True)
        else:
            await interaction.response.send_message(embed=discord.Embed(title="❌ Not Found", description=f"No configuration found for Group ID `{group_id}` in this server.", color=ERROR_COLOR), ephemeral=True)

    @app_commands.command(
        name="list-setups",
        description="List all active group audit log configurations for this server (Authorized Owners Only)."
    )
    async def list_setups_cmd(self, interaction: discord.Interaction):
        if not is_authorized_user(interaction.user.id):
            return await interaction.response.send_message(embed=discord.Embed(title="❌ Permission Denied", description="You are not authorized to run this command.", color=ERROR_COLOR), ephemeral=True)

        db = get_db()
        if db is None:
            return await interaction.response.send_message(embed=discord.Embed(title="❌ Database Error", color=ERROR_COLOR), ephemeral=True)

        cursor = db["group_configs"].find({"guild_id": str(interaction.guild.id)})
        lines = []
        for doc in cursor:
            g_name = doc.get("group_name", "Group")
            g_id = doc.get("group_id")
            ch_id = doc.get("channel_id")
            lines.append(f"• [{g_name}](https://www.roblox.com/groups/{g_id}) (`{g_id}`) ➔ <#{ch_id}>")

        embed = discord.Embed(
            title="📋 Server Group Log Configurations",
            description="\n".join(lines) if lines else "*(No active group setups for this server)*",
            color=EMBED_COLOR
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    # ====================================================================
    # Background Audit Log Poller
    # ====================================================================

    @tasks.loop(seconds=25.0)
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
            last_audit_id = cfg.get("last_audit_id")

            if not channel_id:
                continue

            channel = self.bot.get_channel(int(channel_id))
            if not channel:
                continue

            url = f"https://groups.roblox.com/v1/groups/{group_id}/audit-logs?limit=10"
            try:
                async with session.get(url) as resp:
                    if resp.status != 200:
                        continue

                    body = await resp.json()
                    logs = body.get("data", [])
                    if not logs:
                        continue

                    new_logs = []
                    for item in logs:
                        if last_audit_id and item.get("id") == last_audit_id:
                            break
                        new_logs.append(item)

                    if not new_logs:
                        continue

                    # Save the ID of the newest audit entry
                    db["group_configs"].update_one(
                        {"_id": cfg["_id"]},
                        {"$set": {"last_audit_id": new_logs[0]["id"]}}
                    )

                    # Process log entries from oldest to newest
                    for entry in reversed(new_logs):
                        actor_data = entry.get("actor", {}).get("user", {})
                        actor_name = actor_data.get("username", "System")
                        actor_id = actor_data.get("userId")

                        action_type = entry.get("actionType", "Audit Action")
                        description = entry.get("description", {})
                        created_dt = entry.get("created")

                        # Format Unix timestamp for Discord
                        timestamp_fmt = "<t:0:F>"
                        if created_dt:
                            try:
                                dt = datetime.fromisoformat(created_dt.replace("Z", "+00:00"))
                                timestamp_fmt = f"<t:{int(dt.timestamp())}:F>"
                            except Exception:
                                pass

                        # Link definitions
                        group_link = f"[{group_name}](https://www.roblox.com/groups/{group_id})"
                        actor_link = f"[{actor_name}](https://www.roblox.com/users/{actor_id}/profile)" if actor_id else f"`{actor_name}`"

                        embed = discord.Embed(
                            title=f"🚨 Roblox Group Activity: {action_type}",
                            color=EMBED_COLOR
                        )
                        embed.add_field(name="Group", value=group_link, inline=True)
                        embed.add_field(name="Performer", value=actor_link, inline=True)
                        embed.add_field(name="Timestamp", value=timestamp_fmt, inline=False)

                        # Rank Change Detection (Promotions, Demotions, Manual Rank Shifts)
                        if action_type in ["Change Rank", "Promote Member", "Demote Member"]:
                            target_user = description.get("TargetHeader", description.get("TargetName", "Target User"))
                            target_id = description.get("TargetId")
                            target_link = f"[{target_user}](https://www.roblox.com/users/{target_id}/profile)" if target_id else f"`{target_user}`"

                            old_role = description.get("OldRoleSetHeader", description.get("OldRoleSetName", "Unknown"))
                            new_role = description.get("NewRoleSetHeader", description.get("NewRoleSetName", "Unknown"))

                            old_rank = description.get("OldRank", "")
                            new_rank = description.get("NewRank", "")

                            old_display = f"{old_role} (`{old_rank}`)" if old_rank != "" else f"{old_role}"
                            new_display = f"{new_role} (`{new_rank}`)" if new_rank != "" else f"{new_role}"

                            # Determine status message
                            status_msg = "A user's rank has been modified."
                            if action_type == "Demote Member":
                                status_msg = "⚠️ A user has been demoted."
                                embed.color = discord.Color.orange()
                            elif action_type == "Promote Member":
                                status_msg = "🎉 A user has been promoted."
                                embed.color = SUCCESS_COLOR

                            embed.description = status_msg
                            embed.add_field(name="Target User", value=target_link, inline=False)
                            embed.add_field(name="Old Rank", value=f"`{old_display}`", inline=True)
                            embed.add_field(name="New Rank", value=f"`{new_display}`", inline=True)

                        elif action_type == "Exil Member":
                            target_user = description.get("TargetHeader", description.get("TargetName", "Target User"))
                            target_id = description.get("TargetId")
                            target_link = f"[{target_user}](https://www.roblox.com/users/{target_id}/profile)" if target_id else f"`{target_user}`"

                            embed.description = "❌ A user has been exiled from the group."
                            embed.color = ERROR_COLOR
                            embed.add_field(name="Exiled User", value=target_link, inline=False)

                        elif action_type in ["Accept Join Request", "Decline Join Request"]:
                            target_user = description.get("TargetHeader", description.get("TargetName", "Target User"))
                            target_id = description.get("TargetId")
                            target_link = f"[{target_user}](https://www.roblox.com/users/{target_id}/profile)" if target_id else f"`{target_user}`"

                            embed.description = f"📩 Join request {action_type.lower()}ed."
                            embed.add_field(name="User", value=target_link, inline=False)

                        elif action_type == "Post Shout":
                            shout_text = description.get("Text", "*(Empty Shout)*")
                            embed.description = "📢 Group shout updated."
                            embed.add_field(name="Shout Content", value=f"else:
# Generic Activity Handler
details = ""
if isinstance(description, dict):
for k, v in description.items():
details += f"{k}: ⁠{v}⁠\n"
else:
details = str(description)
embed.description = f"Activity recorded for {group_link}."
if details:
embed.add_field(name="Details", value=details[:1024], inline=False)
try:
await channel.send(embed=embed)
except Exception:
pass
except Exception:
pass
async def setup(bot: commands.Bot):
await bot.add_cog(RobloxAuditLogger(bot))

