"""
Cog for handling user management history, notes, and clearing entries for the Discord Warning Bot.
Includes /userhistory, /note, and /clear commands.
"""
import discord
from discord.ext import commands, tasks
from discord import app_commands
import asyncio
import json # Added import json
from datetime import datetime, timezone
import traceback

class UserHistoryCog(commands.Cog, name="UserHistory"):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def _handle_unmute_due_to_clear(self, guild: discord.Guild, member: discord.Member, interaction_for_followup: discord.Interaction, case_id_cleared: str):
        """Handles unmuting a user if a cleared warning drops them below the threshold."""
        server_id = str(guild.id)
        user_id = str(member.id)
        mute_key = f"{server_id}-{user_id}"

        user_warnings_data = self.bot.warning_data.get("warnings", {}).get(server_id, {}).get(user_id, {})
        active_warning_count = sum(1 for entry in user_warnings_data.get("entries", []) if entry.get("entry_type") == "warning" and entry.get("status") != "cleared")
        
        print(f"[Unmute Check for {member.display_name}] Active warnings after clear: {active_warning_count}")

        if mute_key in self.bot.warning_data.get("active_mutes", {}):
            active_mute_entry = self.bot.warning_data["active_mutes"][mute_key]
            # Check if the specific case ID that triggered the mute is among those cleared or if total count drops
            # For simplicity, we'll re-evaluate based on current active warning count vs punishment ladder.
            # This assumes the punishment ladder is accessible or the logic is self-contained here.
            # The original logic used new_total_warnings < 2. We'll use active_warning_count.
            
            # Determine the current punishment level based on active_warning_count
            # This requires access to the punishment ladder logic, which is in WarningsCog.
            # For now, let's assume a simple threshold (e.g., < 2 active warnings means unmute if muted for general reasons)
            # A more robust solution would be to re-evaluate against the actual punishment ladder.
            should_unmute = True # Default to true if mute exists and we are re-evaluating
            
            # Find the relevant punishment from the ladder based on `active_warning_count`
            general_punishments_config = self.bot.rules_data.get("general_punishment_ladder", [])
            current_punishment_level_action = None
            for pun_def in sorted(general_punishments_config, key=lambda x: x["threshold"], reverse=True):
                if active_warning_count >= pun_def["threshold"]:
                    current_punishment_level_action = pun_def["action"]
                    break
            
            if current_punishment_level_action == "mute":
                print(f"[Unmute Check for {member.display_name}] Still meets mute criteria based on {active_warning_count} active warnings.")
                should_unmute = False
                # Update case_ids for the mute if this one was part of it
                if case_id_cleared in active_mute_entry.get("case_ids_for_mute", []):
                    active_mute_entry["case_ids_for_mute"].remove(case_id_cleared)
                    if not active_mute_entry["case_ids_for_mute"]:
                        # If this was the only warning causing the mute, and now it's cleared, but they still meet criteria by count, this is tricky.
                        # The mute should ideally be tied to specific warning instances or a re-evaluation of the mute duration.
                        # For now, if case_ids_for_mute becomes empty, but they still qualify for a mute, the mute remains.
                        pass # Mute remains based on count
                    self.bot.save_data(self.bot.warning_data)
            else:
                print(f"[Unmute Check for {member.display_name}] No longer meets mute criteria based on {active_warning_count} active warnings.")
                should_unmute = True

            if should_unmute:
                muted_role = await self.bot.get_muted_role(guild)
                if not muted_role:
                    await interaction_for_followup.followup.send(f"记录已清除，但无法找到Muted角色以执行自动解禁。", ephemeral=True)
                    return
                try:
                    if muted_role in member.roles:
                        await member.remove_roles(muted_role, reason=f"Mute lifted due to record clear (Case ID: {case_id_cleared})")
                        verified_role = guild.get_role(self.bot.VERIFIED_ROLE_ID)
                        if verified_role:
                            await member.add_roles(verified_role, reason="Mute lifted, restoring verified role")
                        
                        del self.bot.warning_data["active_mutes"][mute_key]
                        self.bot.save_data(self.bot.warning_data)
                        await interaction_for_followup.followup.send(f"{member.mention} 的禁言已因记录清除 (Case ID: {case_id_cleared}) 而解除。他们的认证角色（如果适用）已恢复。", ephemeral=True)
                        history_channel = self.bot.get_channel(self.bot.HISTORY_CHANNEL_ID)
                        if history_channel:
                            await history_channel.send(f"{member.mention} ({member.id}) 的禁言已因管理记录 (Case ID: {case_id_cleared}) 被清除而解除。")
                    else:
                        if mute_key in self.bot.warning_data["active_mutes"]:
                            del self.bot.warning_data["active_mutes"][mute_key]
                            self.bot.save_data(self.bot.warning_data)
                        await interaction_for_followup.followup.send(f"记录已清除。用户已不在禁言状态或数据不一致。", ephemeral=True)
                except discord.Forbidden:
                    await interaction_for_followup.followup.send(f"记录已清除，但机器人权限不足以解除 {member.mention} 的禁言或恢复角色。", ephemeral=True)
                except discord.HTTPException as e:
                    await interaction_for_followup.followup.send(f"记录已清除，但在尝试解除 {member.mention} 禁言时发生HTTP错误: {e}", ephemeral=True)
            else:
                 await interaction_for_followup.followup.send(f"记录 Case ID `{case_id_cleared}` 已为用户 {member.mention} 清除。他们当前仍处于禁言状态 (剩余 {active_warning_count} 次有效警告)。", ephemeral=True)
        else:
            # Not currently muted by the bot's active_mutes system
            pass

    @app_commands.command(name="note", description="为用户添加一条管理备注。")
    @app_commands.describe(member="要添加备注的用户", text="备注内容")
    async def note_slash_command(self, interaction: discord.Interaction, member: discord.Member, text: str):
        if not await self.bot.check_admin_role(interaction):
            return
        await interaction.response.defer(ephemeral=True, thinking=True)

        server_id = str(interaction.guild.id)
        user_id = str(member.id)
        case_id = self.bot.generate_case_id()
        timestamp = int(datetime.now(timezone.utc).timestamp())
        operator_id = str(interaction.user.id)
        operator_name = interaction.user.display_name

        note_entry = {
            "entry_type": "note",
            "case_id": case_id,
            "timestamp": timestamp,
            "operator_id": operator_id,
            "operator_name": operator_name,
            "text": text,
            "status": "active" # active, cleared
        }

        if server_id not in self.bot.warning_data["warnings"]: self.bot.warning_data["warnings"][server_id] = {}
        if user_id not in self.bot.warning_data["warnings"][server_id]:
            self.bot.warning_data["warnings"][server_id][user_id] = {"entries": [], "total_warnings": 0, "per_rule_violations": {}}
        
        self.bot.warning_data["warnings"][server_id][user_id]["entries"].append(note_entry)
        self.bot.save_data(self.bot.warning_data)

        await interaction.followup.send(f"已为用户 {member.mention} 添加备注 (Case ID: {case_id})。", ephemeral=True)

    @app_commands.command(name="clear", description="清除一条用户的管理记录 (警告或备注)。")
    @app_commands.describe(case_id="要清除的记录的Case ID")
    async def clear_slash_command(self, interaction: discord.Interaction, case_id: str):
        if not await self.bot.check_admin_role(interaction):
            return
        await interaction.response.defer(ephemeral=True, thinking=True)

        server_id = str(interaction.guild.id)
        found_entry = False
        target_user_id = None
        entry_to_clear = None
        original_message_id_history = None
        entry_type = None

        if server_id not in self.bot.warning_data.get("warnings", {}):
            await interaction.followup.send(f"Case ID `{case_id}` 不存在 (服务器无记录)。", ephemeral=True)
            return

        for user_id_str, user_data in self.bot.warning_data["warnings"][server_id].items():
            for entry in user_data.get("entries", []):
                if entry.get("case_id") == case_id.upper() and entry.get("status", "active") == "active":
                    entry_to_clear = entry
                    target_user_id = user_id_str
                    entry_type = entry.get("entry_type", "unknown")
                    original_message_id_history = entry.get("message_id_history_channel") # For warnings
                    
                    entry["status"] = "cleared"
                    entry["cleared_timestamp"] = int(datetime.now(timezone.utc).timestamp())
                    entry["cleared_by_operator_id"] = str(interaction.user.id)
                    entry["cleared_by_operator_name"] = interaction.user.display_name
                    found_entry = True
                    break
            if found_entry:
                break

        if not found_entry or not target_user_id:
            await interaction.followup.send(f"有效的 Case ID `{case_id}` 未找到或已被清除。", ephemeral=True)
            return

        # If it was a warning, update counts
        if entry_type == "warning":
            user_data = self.bot.warning_data["warnings"][server_id][target_user_id]
            user_data["total_warnings"] = sum(1 for e in user_data.get("entries", []) if e.get("entry_type") == "warning" and e.get("status", "active") == "active")
            
            # Update per_rule_violations
            if "rule_id_matched" in entry_to_clear and entry_to_clear["rule_id_matched"]:
                rule_id = entry_to_clear["rule_id_matched"]
                user_data["per_rule_violations"][rule_id] = user_data["per_rule_violations"].get(rule_id, 0) -1
                if user_data["per_rule_violations"][rule_id] <= 0:
                    del user_data["per_rule_violations"][rule_id]
            
            # Edit message in history channel for warnings
            history_channel = self.bot.get_channel(self.bot.HISTORY_CHANNEL_ID)
            if history_channel and original_message_id_history:
                try:
                    history_msg = await history_channel.fetch_message(original_message_id_history)
                    edited_embed = history_msg.embeds[0] if history_msg.embeds else discord.Embed(description="原始消息无Embed")
                    description_suffix = f"\n**此警告已由 {interaction.user.mention} 于 <t:{entry['cleared_timestamp']}:f> 清除。**"
                    if edited_embed.description:
                        edited_embed.description += description_suffix
                    else:
                        edited_embed.description = description_suffix
                    edited_embed.color = discord.Color.dark_grey()
                    await history_msg.edit(embed=edited_embed)
                except discord.NotFound:
                    print(f"History message for case {case_id} (ID: {original_message_id_history}) not found.")
                except discord.Forbidden:
                    print(f"Forbidden to edit history message for case {case_id}.")
                except Exception as e:
                    print(f"Error editing history message for case {case_id}: {e}")

        self.bot.save_data(self.bot.warning_data)
        
        target_member = interaction.guild.get_member(int(target_user_id))
        if not target_member:
            await interaction.followup.send(f"记录 Case ID `{case_id}` ({entry_type}) 已清除。但无法在当前服务器找到相关用户对象。", ephemeral=True)
            return

        if entry_type == "warning":
            await self._handle_unmute_due_to_clear(interaction.guild, target_member, interaction, case_id)
            # If _handle_unmute_due_to_clear sends its own followup, we might not need another one here.
            # However, it's safer to have a general confirmation if no specific unmute message was sent.
            if not interaction.response.is_done(): # Check if a response hasn't been sent by unmute logic
                 await interaction.followup.send(f"警告记录 Case ID `{case_id}` 已为用户 {target_member.mention} 清除。", ephemeral=True)
        else: # It was a note or other type
            await interaction.followup.send(f"备注记录 Case ID `{case_id}` 已为用户 {target_member.mention} 清除。", ephemeral=True)

    @app_commands.command(name="userhistory", description="查询用户的管理记录 (警告和备注)。")
    @app_commands.describe(member="要查询历史的用户")
    async def userhistory_slash_command(self, interaction: discord.Interaction, member: discord.Member):
        if not await self.bot.check_admin_role(interaction):
            return
        await interaction.response.defer(ephemeral=True, thinking=True)

        server_id = str(interaction.guild.id)
        user_id = str(member.id)

        user_data = self.bot.warning_data.get("warnings", {}).get(server_id, {}).get(user_id)

        if not user_data or not user_data.get("entries"):
            await interaction.followup.send(f"{member.mention} 没有管理记录。", ephemeral=True)
            return

        history_embed = discord.Embed(
            title=f"{member.display_name} 的管理记录",
            color=discord.Color.blue(),
            timestamp=datetime.now(timezone.utc)
        )
        history_embed.set_thumbnail(url=member.display_avatar.url)
        
        active_entries_count = 0
        for entry in sorted(user_data["entries"], key=lambda x: x["timestamp"]):
            if entry.get("status", "active") == "cleared":
                continue # Skip cleared entries for now, per user's desire for non-destructive delete
            active_entries_count +=1

            entry_type_str = "未知类型"
            content_label = "内容"
            content_value = entry.get("text", "N/A") # For notes

            if entry.get("entry_type") == "warning":
                entry_type_str = "警告"
                content_label = "理由"
                content_value = entry.get("reason_displayed", entry.get("reason", "N/A")) # reason_displayed from new warn, reason from old
            elif entry.get("entry_type") == "note":
                entry_type_str = "备注"
            elif entry.get("entry_type") == "join_event":
                entry_type_str = "加入服务器"
                content_value = f"用户加入了服务器。"
            elif entry.get("entry_type") == "leave_event":
                entry_type_str = "离开服务器"
                content_value = f"用户离开了服务器。"
            
            field_name = f"**{entry_type_str}** - <t:{entry['timestamp']}:f> (Case ID: {entry['case_id']})"
            field_value = f"操作者: {entry.get('operator_name', '系统')} ({entry.get('operator_id', 'N/A')})\n{content_label}: {content_value}"
            if entry.get("entry_type") == "warning" and entry.get("rule_id_matched"):
                field_value += f"\n涉及规则: {entry['rule_id_matched']}"
            
            if len(history_embed.fields) < 25: # Embed field limit
                 history_embed.add_field(name=field_name, value=field_value, inline=False)
            else:
                if not history_embed.footer.text or "更多记录未显示" not in history_embed.footer.text:
                    history_embed.set_footer(text=history_embed.footer.text + " | 更多记录未显示 (已达上限)")
                break 

        total_active_warnings = sum(1 for e in user_data.get("entries", []) if e.get("entry_type") == "warning" and e.get("status", "active") == "active")
        footer_text = f"当前有效警告总数: {total_active_warnings}"
        
        per_rule_violations_active = {k: v for k,v in user_data.get("per_rule_violations", {}).items() if v > 0}
        if per_rule_violations_active:
            rules_violated_str = ", ".join([f"规则{k}: {v}次" for k,v in per_rule_violations_active.items()])
            footer_text += f" | 规则违反统计: {rules_violated_str}"
        history_embed.set_footer(text=footer_text)

        if active_entries_count == 0:
             await interaction.followup.send(f"{member.mention} 没有有效的管理记录。", ephemeral=True)
             return

        try:
            await interaction.followup.send(embed=history_embed, ephemeral=True)
        except discord.HTTPException as e:
            await interaction.followup.send(f"无法发送完整的历史记录，可能过长。错误: {e}", ephemeral=True)

    @note_slash_command.error
    @clear_slash_command.error
    @userhistory_slash_command.error
    async def on_userhistory_related_command_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError):
        print(f"Error in UserHistoryCog command: {error}")
        traceback.print_exc()
        error_message = f"执行命令时发生未知错误: {error}"
        if isinstance(error, app_commands.CommandInvokeError) and error.original:
            error_message = f"执行命令时发生内部错误: {error.original}"
        elif isinstance(error, app_commands.CheckFailure):
            error_message = "您没有权限使用此命令。"
        
        if interaction.response.is_done():
            await interaction.followup.send(error_message, ephemeral=True)
        else:
            await interaction.response.send_message(error_message, ephemeral=True)

async def setup(bot: commands.Bot):
    from main import ADMIN_ROLE_ID, HISTORY_CHANNEL_ID, MUTED_ROLE_NAME, VERIFIED_ROLE_ID, DATA_FILE, RULES_DATA_FILE
    from main import load_data, save_data, generate_case_id, check_admin_role, get_muted_role

    bot.ADMIN_ROLE_ID = ADMIN_ROLE_ID
    bot.HISTORY_CHANNEL_ID = HISTORY_CHANNEL_ID
    bot.MUTED_ROLE_NAME = MUTED_ROLE_NAME
    bot.VERIFIED_ROLE_ID = VERIFIED_ROLE_ID
    bot.DATA_FILE = DATA_FILE
    bot.RULES_DATA_FILE = RULES_DATA_FILE # For _handle_unmute_due_to_clear

    bot.load_data = load_data
    bot.save_data = save_data
    bot.generate_case_id = generate_case_id
    bot.check_admin_role = check_admin_role
    bot.get_muted_role = get_muted_role
    
    if not hasattr(bot, 'warning_data'):
        bot.warning_data = bot.load_data()
    if not hasattr(bot, 'rules_data'): # Load rules data if not present, needed for unmute logic
        try:
            with open(bot.RULES_DATA_FILE, "r", encoding="utf-8") as f:
                bot.rules_data = json.load(f)
        except Exception as e:
            print(f"Critical error: Could not load rules_database.json in UserHistoryCog setup: {e}")
            bot.rules_data = {"rules": [], "general_punishment_ladder": []} # Fallback

    await bot.add_cog(UserHistoryCog(bot))
    print("UserHistoryCog loaded with /note and /clear commands.")

