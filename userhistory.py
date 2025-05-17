"""
Cog for handling user management history, notes, and clearing entries for the Discord Warning Bot.
Includes /userhistory, /note, and /clear commands.
"""
import discord
from discord.ext import commands, tasks
from discord import app_commands
import asyncio
import json
from datetime import datetime, timezone
import traceback

class UserHistoryCog(commands.Cog, name="UserHistory"):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        # 尝试加载rules_data
        self.rules_data = self._load_rules_data()

    def _load_rules_data(self):
        """加载规则数据，确保在_handle_unmute_due_to_clear中可用"""
        try:
            with open(self.bot.RULES_DATA_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                # 验证规则数据的基本结构
                if not isinstance(data, dict):
                    print(f"ERROR: Rules data at {self.bot.RULES_DATA_FILE} is not a valid JSON object. Returning empty rules.")
                    return {"rules": [], "general_punishment_ladder": []}
                
                # 确保必要的键存在
                if "rules" not in data:
                    print(f"WARNING: Rules data at {self.bot.RULES_DATA_FILE} is missing 'rules' key. Adding empty rules list.")
                    data["rules"] = []
                
                if "general_punishment_ladder" not in data:
                    print(f"WARNING: Rules data at {self.bot.RULES_DATA_FILE} is missing 'general_punishment_ladder' key. Adding empty ladder.")
                    data["general_punishment_ladder"] = []
                
                return data
        except FileNotFoundError:
            print(f"ERROR: Rules data file not found at {self.bot.RULES_DATA_FILE}. Returning empty rules.")
            return {"rules": [], "general_punishment_ladder": []}
        except json.JSONDecodeError as e:
            print(f"ERROR: Could not decode JSON from {self.bot.RULES_DATA_FILE}: {e}. Returning empty rules.")
            return {"rules": [], "general_punishment_ladder": []}
        except AttributeError:
            print(f"ERROR: bot.RULES_DATA_FILE not set. Ensure RULES_DATA_FILE is passed from main.py during setup. Returning empty rules.")
            return {"rules": [], "general_punishment_ladder": []}
        except Exception as e:
            print(f"ERROR: Unexpected error loading rules data: {e}. Returning empty rules.")
            return {"rules": [], "general_punishment_ladder": []}

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
            should_unmute = True # Default to true if mute exists and we are re-evaluating
            
            # Find the relevant punishment from the ladder based on `active_warning_count`
            general_punishments_config = self.rules_data.get("general_punishment_ladder", [])
            current_punishment_level_action = None
            for pun_def in sorted(general_punishments_config, key=lambda x: x.get("threshold", 0), reverse=True):
                if active_warning_count >= pun_def.get("threshold", 0):
                    current_punishment_level_action = pun_def.get("action")
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
                    save_result = self.bot.save_data(self.bot.warning_data)
                    if not save_result:
                        print(f"[Unmute Check] Failed to save data after updating case_ids_for_mute for {member.display_name}")
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
                        save_result = self.bot.save_data(self.bot.warning_data)
                        if not save_result:
                            print(f"[Unmute Check] Failed to save data after unmuting {member.display_name}")
                            
                        await interaction_for_followup.followup.send(f"{member.mention} 的禁言已因记录清除 (Case ID: {case_id_cleared}) 而解除。他们的认证角色（如果适用）已恢复。", ephemeral=True)
                        history_channel = self.bot.get_channel(self.bot.HISTORY_CHANNEL_ID)
                        if history_channel:
                            await history_channel.send(f"{member.mention} ({member.id}) 的禁言已因管理记录 (Case ID: {case_id_cleared}) 被清除而解除。")
                    else:
                        if mute_key in self.bot.warning_data["active_mutes"]:
                            del self.bot.warning_data["active_mutes"][mute_key]
                            save_result = self.bot.save_data(self.bot.warning_data)
                            if not save_result:
                                print(f"[Unmute Check] Failed to save data after removing mute entry for {member.display_name}")
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
        """添加管理备注，不会计入警告总数，也不会通知用户或历史频道"""
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
        save_result = self.bot.save_data(self.bot.warning_data)
        
        if not save_result:
            await interaction.followup.send(f"警告：保存备注数据时发生错误。备注可能不会持久保存。", ephemeral=True)
            print(f"Error saving note data for user {member.display_name} (ID: {user_id}) in guild {interaction.guild.name} (ID: {server_id}).")
        else:
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

        # 统一大写处理case_id
        case_id = case_id.upper()

        for user_id_str, user_data in self.bot.warning_data["warnings"][server_id].items():
            for entry in user_data.get("entries", []):
                if entry.get("case_id") == case_id and entry.get("status", "active") == "active":
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

        save_result = self.bot.save_data(self.bot.warning_data)
        if not save_result:
            await interaction.followup.send(f"警告：保存清除记录时发生错误。清除操作可能不会持久保存。", ephemeral=True)
            print(f"Error saving data after clearing record (Case ID: {case_id}) for user ID {target_user_id} in guild {interaction.guild.name} (ID: {server_id}).")
        
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
        """查询用户的管理记录，包括警告和备注"""
        if not await self.bot.check_admin_role(interaction):
            return
        await interaction.response.defer(ephemeral=True, thinking=True)

        server_id = str(interaction.guild.id)
        user_id = str(member.id)

        user_data = self.bot.warning_data.get("warnings", {}).get(server_id, {}).get(user_id)

        if not user_data or not user_data.get("entries"):
            await interaction.followup.send(f"{member.mention} 没有管理记录。", ephemeral=True)
            return

        # 根据记录类型选择标题
        has_warnings = any(e.get("entry_type") == "warning" and e.get("status", "active") == "active" for e in user_data.get("entries", []))
        has_notes = any(e.get("entry_type") == "note" and e.get("status", "active") == "active" for e in user_data.get("entries", []))
        
        title = f"{member.display_name} 的管理记录"
        if has_notes and not has_warnings:
            title = f"{member.display_name} 的管理备注"
        
        history_embed = discord.Embed(
            title=title,
            color=discord.Color.blue(),
            timestamp=datetime.now(timezone.utc)
        )
        history_embed.set_thumbnail(url=member.display_avatar.url)
        
        active_entries_count = 0
        for entry in sorted(user_data["entries"], key=lambda x: x["timestamp"]):
            if entry.get("status", "active") == "cleared":
                continue # Skip cleared entries for now, per user's desire for non-destructive delete
            active_entries_count += 1

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
            
            # 按照知识模块要求格式化输出
            field_name = f"**{entry_type_str}** - <t:{entry['timestamp']}:f> (Case ID: {entry['case_id']})"
            field_value = f"操作者: {entry.get('operator_name', '系统')} ({entry.get('operator_id', 'N/A')})\n{content_label}: {content_value}"
            if entry.get("entry_type") == "warning" and entry.get("rule_id_matched"):
                field_value += f"\n涉及规则: {entry['rule_id_matched']}"
            
            if len(history_embed.fields) < 25: # Embed field limit
                 history_embed.add_field(name=field_name, value=field_value, inline=False)
            else:
                if not history_embed.footer.text or "更多记录未显示" not in history_embed.footer.text:
                    history_embed.set_footer(text=f"共 {active_entries_count} 条记录 | 更多记录未显示 (已达上限)")
                break 

        # 添加总计信息
        total_warnings = user_data.get("total_warnings", 0)
        if total_warnings > 0:
            history_embed.add_field(name="当前有效警告总数", value=str(total_warnings), inline=True)
        
        # 添加规则违反信息
        per_rule_violations = user_data.get("per_rule_violations", {})
        if per_rule_violations:
            rule_violations_text = "\n".join([f"规则 {rule_id}: {count} 次" for rule_id, count in per_rule_violations.items()])
            history_embed.add_field(name="规则违反统计", value=rule_violations_text, inline=True)
        
        # 设置页脚
        if not history_embed.footer.text:
            history_embed.set_footer(text=f"共 {active_entries_count} 条记录")
        
        await interaction.followup.send(embed=history_embed, ephemeral=True)

async def setup(bot: commands.Bot):
    await bot.add_cog(UserHistoryCog(bot))
