"""
Cog for handling warning-related commands and logic for the Discord Warning Bot.
Includes /warn command, user context menu for warning, reason modal, and mute handling.
"""
import discord
from discord.ext import commands, tasks
from discord import app_commands
import asyncio
import json
from datetime import datetime, timedelta, timezone
import traceback

class ReasonModal(discord.ui.Modal, title="警告理由"):
    reason_input = discord.ui.TextInput(
        label="请输入警告理由或规则编号",
        style=discord.TextStyle.long,
        placeholder="例如: 屡次发送无关内容 或输入规则编号如 1, 2 等.",
        required=True,
        max_length=512
    )

    def __init__(self, original_command_interaction: discord.Interaction, target_user: discord.Member, target_channel: discord.TextChannel, cog_instance):
        super().__init__()
        self.original_command_interaction = original_command_interaction
        self.target_user = target_user
        self.target_channel = target_channel
        self.cog_instance = cog_instance

    async def on_submit(self, modal_submission_interaction: discord.Interaction):
        raw_reason_input = self.reason_input.value
        # Defer the modal submission interaction first
        await modal_submission_interaction.response.defer(ephemeral=True, thinking=False)
        # Then call _handle_warning, which will use the original_command_interaction for its followups
        await self.cog_instance._handle_warning(self.original_command_interaction, self.target_user, raw_reason_input, self.target_channel)

    async def on_error(self, modal_submission_interaction: discord.Interaction, error: Exception):
        print(f"Error in ReasonModal: {error}")
        traceback.print_exc()
        if not modal_submission_interaction.response.is_done():
            await modal_submission_interaction.response.send_message("提交理由时发生错误，请重试。", ephemeral=True)
        else:
            # This case might happen if defer() succeeded but _handle_warning failed and original_command_interaction already responded.
            # However, on_submit defers modal_submission_interaction, so this followup should be on modal_submission_interaction.
            await modal_submission_interaction.followup.send("提交理由时发生后续处理错误，请联系管理员。", ephemeral=True)

class WarningsCog(commands.Cog, name="Warnings"):
    @tasks.loop(seconds=60)
    async def unmute_task_loop(self):
        if not hasattr(self, 'bot') or not self.bot.is_ready():
            # print("[Unmute Task] Bot not ready or self.bot not set, skipping current loop iteration.") # Too verbose for normal operation
            return
        await self.bot.wait_until_ready()
        now = datetime.now(timezone.utc)
        mutes_to_remove = []
        active_mutes = self.bot.warning_data.get("active_mutes", {})
        if not active_mutes:
            return

        for key, mute_info in list(active_mutes.items()):
            try:
                # 修复unmute_at类型兼容性问题
                unmute_at_value = mute_info.get("unmute_at")
                unmute_at = None
                
                # 处理字符串格式的ISO时间戳
                if isinstance(unmute_at_value, str):
                    try:
                        unmute_at = datetime.fromisoformat(unmute_at_value)
                    except ValueError:
                        print(f"[Unmute Task] Error parsing ISO timestamp for key {key}: '{unmute_at_value}' is not a valid ISO format. Skipping entry.")
                        continue
                
                # 处理数字格式的UNIX时间戳（历史数据兼容）
                elif isinstance(unmute_at_value, (int, float)):
                    try:
                        unmute_at = datetime.fromtimestamp(unmute_at_value, tz=timezone.utc)
                        # 更新为标准格式以避免未来再次出现此问题
                        mute_info["unmute_at"] = unmute_at.isoformat()
                        save_result = self.bot.save_data(self.bot.warning_data)
                        if not save_result:
                            print(f"[Unmute Task] Failed to save data after converting timestamp for key {key}")
                        else:
                            print(f"[Unmute Task] Converted numeric timestamp {unmute_at_value} to ISO format for key {key}")
                    except (ValueError, OSError, OverflowError) as e:
                        print(f"[Unmute Task] Error converting numeric timestamp for key {key}: {e}. Skipping entry.")
                        continue
                
                # 无法处理的类型
                else:
                    print(f"[Unmute Task] Error processing unmute for key {key}: 'unmute_at' has unsupported type {type(unmute_at_value)} (value: {unmute_at_value}). Skipping entry.")
                    continue
                
                if now >= unmute_at:
                    guild = self.bot.get_guild(mute_info["guild_id"])
                    if guild:
                        member_obj = guild.get_member(mute_info["user_id"])
                        muted_role_obj = await self.bot.get_muted_role(guild)
                        if member_obj and muted_role_obj and muted_role_obj in member_obj.roles:
                            try:
                                await member_obj.remove_roles(muted_role_obj, reason="Mute duration expired")
                                print(f"[Unmute Task] Unmuted {member_obj.display_name} in {guild.name}.")
                                verified_role = guild.get_role(self.bot.VERIFIED_ROLE_ID)
                                if verified_role:
                                    await member_obj.add_roles(verified_role, reason="Mute expired, restoring verified role")
                                history_channel = self.bot.get_channel(self.bot.HISTORY_CHANNEL_ID)
                                if history_channel: await history_channel.send(f"{member_obj.mention} ({member_obj.id}) 的禁言已到期并自动解除。")
                            except discord.Forbidden:
                                print(f"[Unmute Task] Failed to unmute {member_obj.display_name} or restore role in {guild.name} due to permissions.")
                            except discord.HTTPException as e:
                                print(f"[Unmute Task] HTTP error while unmuting {member_obj.display_name}: {e}")
                        elif member_obj and muted_role_obj and muted_role_obj not in member_obj.roles:
                             print(f"[Unmute Task] User {member_obj.display_name} was in unmute list but did not have Muted role.")
                        elif not member_obj:
                            print(f"[Unmute Task] Member with ID {mute_info['user_id']} not found in guild {guild.name} for unmute.")
                    mutes_to_remove.append(key)
            except Exception as e:
                print(f"[Unmute Task] Error processing unmute for key {key}: {e}")
                traceback.print_exc()
        
        if mutes_to_remove:
            for key in mutes_to_remove:
                if key in self.bot.warning_data["active_mutes"]: del self.bot.warning_data["active_mutes"][key]
            save_result = self.bot.save_data(self.bot.warning_data)
            if not save_result:
                print(f"[Unmute Task] Failed to save data after removing {len(mutes_to_remove)} expired mutes.")
            else:
                print(f"[Unmute Task] Removed {len(mutes_to_remove)} expired mutes from data.")

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.rules_data = self._load_rules_data()
        if hasattr(self.unmute_task_loop, 'start') and callable(getattr(self.unmute_task_loop, 'start')):
            if not self.unmute_task_loop.is_running():
                self.unmute_task_loop.start()
                print("[WarningsCog __init__] unmute_task_loop started.")
        else:
            print("[WarningsCog __init__] ERROR: self.unmute_task_loop does not have a callable 'start' method or does not exist as expected.")

    def _load_rules_data(self):
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

    def cog_unload(self):
        if hasattr(self.unmute_task_loop, 'cancel') and callable(getattr(self.unmute_task_loop, 'cancel')):
            self.unmute_task_loop.cancel()
            print("[WarningsCog cog_unload] unmute_task_loop cancelled.")

    async def _handle_warning(self, original_interaction: discord.Interaction, member: discord.Member, raw_reason_input: str, target_channel: discord.TextChannel):
        server_id = str(original_interaction.guild.id)
        user_id = str(member.id)
        case_id = self.bot.generate_case_id()
        timestamp = int(datetime.now(timezone.utc).timestamp())
        operator_id = str(original_interaction.user.id)
        operator_name = original_interaction.user.display_name

        displayed_reason = raw_reason_input
        matched_rule_id = None 
        rule_specific_actions = None 

        if raw_reason_input.strip().isdigit():
            rule_id_str = raw_reason_input.strip()
            rule_definition = next((rule for rule in self.rules_data.get("rules", []) if rule.get("id") == rule_id_str), None)
            if rule_definition:
                rule_text = rule_definition.get("text", "规则描述未找到。")
                displayed_reason = f"规则 {rule_id_str}: {rule_text}"
                matched_rule_id = rule_id_str
                if rule_definition.get("action_type") == "specific_action":
                    rule_specific_actions = rule_definition.get("actions")
        
        history_channel = self.bot.get_channel(self.bot.HISTORY_CHANNEL_ID)
        if not history_channel or not isinstance(history_channel, discord.TextChannel):
            # Ensure interaction is not already responded to before sending followup
            if not original_interaction.response.is_done():
                 await original_interaction.response.send_message("错误：未找到或配置错误的历史频道。请联系机器人管理员。", ephemeral=True)
            else:
                await original_interaction.followup.send("错误：未找到或配置错误的历史频道。请联系机器人管理员。", ephemeral=True)
            return

        warning_entry = {
            "entry_type": "warning",
            "status": "active",
            "case_id": case_id, "timestamp": timestamp, "operator_id": operator_id,
            "operator_name": operator_name, "reason_displayed": displayed_reason,
            "rule_id_matched": matched_rule_id, "original_input": raw_reason_input,
            "message_id_history_channel": None, "message_id_notification_channel": None
        }

        if server_id not in self.bot.warning_data["warnings"]: self.bot.warning_data["warnings"][server_id] = {}
        if user_id not in self.bot.warning_data["warnings"][server_id]:
            self.bot.warning_data["warnings"][server_id][user_id] = {"entries": [], "total_warnings": 0, "per_rule_violations": {}}
        if "per_rule_violations" not in self.bot.warning_data["warnings"][server_id][user_id]:
            self.bot.warning_data["warnings"][server_id][user_id]["per_rule_violations"] = {}
        if "entries" not in self.bot.warning_data["warnings"][server_id][user_id]: # Ensure entries list exists
            self.bot.warning_data["warnings"][server_id][user_id]["entries"] = []

        self.bot.warning_data["warnings"][server_id][user_id]["entries"].append(warning_entry)
        # Recalculate total_warnings based on active warning entries
        user_entries = self.bot.warning_data["warnings"][server_id][user_id]["entries"]
        self.bot.warning_data["warnings"][server_id][user_id]["total_warnings"] = sum(1 for e in user_entries if e.get("entry_type") == "warning" and e.get("status") == "active")
        total_warnings_overall = self.bot.warning_data["warnings"][server_id][user_id]["total_warnings"]

        if matched_rule_id:
            # Recalculate per_rule_violations
            current_violations = sum(1 for e in user_entries if e.get("entry_type") == "warning" and e.get("status") == "active" and e.get("rule_id_matched") == matched_rule_id)
            self.bot.warning_data["warnings"][server_id][user_id]["per_rule_violations"][matched_rule_id] = current_violations

        history_embed = discord.Embed(title=f"用户警告记录 (Case ID: {case_id})", color=discord.Color.orange(), timestamp=datetime.fromtimestamp(timestamp, timezone.utc))
        history_embed.add_field(name="用户", value=f"{member.mention} ({member.id})", inline=False)
        history_embed.add_field(name="操作者", value=f"{original_interaction.user.mention} ({operator_id})", inline=False)
        history_embed.add_field(name="理由", value=displayed_reason, inline=False)
        if matched_rule_id: history_embed.add_field(name="涉及规则编号", value=matched_rule_id, inline=True)
        history_embed.add_field(name="当前有效警告总数", value=str(total_warnings_overall), inline=True)
        history_embed.set_footer(text=f"Case ID: {case_id}")
        try:
            history_msg = await history_channel.send(embed=history_embed)
            warning_entry["message_id_history_channel"] = history_msg.id
        except discord.Forbidden:
            # Rollback counts if history message fails
            self.bot.warning_data["warnings"][server_id][user_id]["entries"].pop()
            self.bot.warning_data["warnings"][server_id][user_id]["total_warnings"] = sum(1 for e in self.bot.warning_data["warnings"][server_id][user_id]["entries"] if e.get("entry_type") == "warning" and e.get("status") == "active")
            if matched_rule_id:
                 self.bot.warning_data["warnings"][server_id][user_id]["per_rule_violations"][matched_rule_id] = sum(1 for e in self.bot.warning_data["warnings"][server_id][user_id]["entries"] if e.get("entry_type") == "warning" and e.get("status") == "active" and e.get("rule_id_matched") == matched_rule_id)
                 if self.bot.warning_data["warnings"][server_id][user_id]["per_rule_violations"][matched_rule_id] == 0:
                     del self.bot.warning_data["warnings"][server_id][user_id]["per_rule_violations"][matched_rule_id]
            self.bot.save_data(self.bot.warning_data) # Save after rollback
            if not original_interaction.response.is_done(): await original_interaction.response.send_message("错误：机器人无权限在历史频道发送消息。警告未完全记录。", ephemeral=True) 
            else: await original_interaction.followup.send("错误：机器人无权限在历史频道发送消息。警告未完全记录。", ephemeral=True)
            return
        except discord.HTTPException as e:
            # Rollback counts
            self.bot.warning_data["warnings"][server_id][user_id]["entries"].pop()
            self.bot.warning_data["warnings"][server_id][user_id]["total_warnings"] = sum(1 for e in self.bot.warning_data["warnings"][server_id][user_id]["entries"] if e.get("entry_type") == "warning" and e.get("status") == "active")
            if matched_rule_id:
                 self.bot.warning_data["warnings"][server_id][user_id]["per_rule_violations"][matched_rule_id] = sum(1 for e in self.bot.warning_data["warnings"][server_id][user_id]["entries"] if e.get("entry_type") == "warning" and e.get("status") == "active" and e.get("rule_id_matched") == matched_rule_id)
                 if self.bot.warning_data["warnings"][server_id][user_id]["per_rule_violations"][matched_rule_id] == 0:
                     del self.bot.warning_data["warnings"][server_id][user_id]["per_rule_violations"][matched_rule_id]
            self.bot.save_data(self.bot.warning_data) # Save after rollback
            if not original_interaction.response.is_done(): await original_interaction.response.send_message(f"错误：发送历史消息时发生HTTP错误: {e}。警告未完全记录。", ephemeral=True)
            else: await original_interaction.followup.send(f"错误：发送历史消息时发生HTTP错误: {e}。警告未完全记录。", ephemeral=True)
            return

        # Save data after successful history message
        save_result = self.bot.save_data(self.bot.warning_data)
        if not save_result:
            await original_interaction.followup.send(f"警告：保存警告数据时发生错误。警告已记录但可能不会持久保存。", ephemeral=True)
            print(f"Error saving warning data for user {member.display_name} (ID: {user_id}) in guild {original_interaction.guild.name} (ID: {server_id}).")

        # Notify the user about the warning
        try:
            user_embed = discord.Embed(title=f"您收到了一条警告", color=discord.Color.red(), timestamp=datetime.fromtimestamp(timestamp, timezone.utc))
            user_embed.add_field(name="服务器", value=original_interaction.guild.name, inline=False)
            user_embed.add_field(name="理由", value=displayed_reason, inline=False)
            user_embed.add_field(name="警告ID", value=case_id, inline=True)
            user_embed.add_field(name="当前有效警告总数", value=str(total_warnings_overall), inline=True)
            user_embed.set_footer(text=f"如有疑问，请联系管理员")
            
            await member.send(embed=user_embed)
            await original_interaction.followup.send(f"已成功警告用户 {member.mention} (Case ID: {case_id})，并已通过私信通知。", ephemeral=True)
        except discord.Forbidden:
            await original_interaction.followup.send(f"已成功警告用户 {member.mention} (Case ID: {case_id})，但无法通过私信通知（可能已关闭私信）。", ephemeral=True)
        except discord.HTTPException as e:
            await original_interaction.followup.send(f"已成功警告用户 {member.mention} (Case ID: {case_id})，但通知私信发送失败: {e}", ephemeral=True)

        # Check if punishment is needed based on warning count
        await self._check_and_apply_punishment(original_interaction, member, total_warnings_overall, matched_rule_id, rule_specific_actions, case_id)

    async def _check_and_apply_punishment(self, interaction: discord.Interaction, member: discord.Member, warning_count: int, rule_id: str = None, rule_actions = None, case_id: str = None):
        """Checks if punishment should be applied based on warning count and rule."""
        server_id = str(interaction.guild.id)
        user_id = str(member.id)
        
        # First check rule-specific actions if available
        if rule_id and rule_actions:
            # Handle rule-specific actions here
            # This would depend on the structure of rule_actions
            # For now, we'll just log it
            print(f"Rule-specific actions for rule {rule_id} would be applied here.")
            # Example: if "permanent_remove_from_group" in rule_actions...
            return

        # Otherwise, check general punishment ladder
        general_punishments = self.rules_data.get("general_punishment_ladder", [])
        if not general_punishments:
            return
        
        # Find the highest applicable punishment level
        applicable_punishment = None
        for punishment in sorted(general_punishments, key=lambda x: x.get("threshold", 0), reverse=True):
            if warning_count >= punishment.get("threshold", 0):
                applicable_punishment = punishment
                break
        
        if not applicable_punishment:
            return
            
        action = applicable_punishment.get("action")
        if not action:
            return
            
        if action == "mute":
            # Handle mute action
            duration_minutes = applicable_punishment.get("duration_minutes", 0)
            duration_hours = applicable_punishment.get("duration_hours", 0)
            total_minutes = duration_minutes + (duration_hours * 60)
            
            if total_minutes <= 0:
                print(f"Invalid mute duration: {total_minutes} minutes")
                return
                
            await self._apply_mute(interaction, member, total_minutes, case_id)
        elif action == "remove_temporary":
            # Handle temporary removal
            try:
                reason = applicable_punishment.get("description_template", "违反群规").format(count=warning_count)
                await member.kick(reason=reason)
                await interaction.followup.send(f"已将 {member.mention} 移出服务器 (原因: {reason})。", ephemeral=True)
                history_channel = interaction.guild.get_channel(self.bot.HISTORY_CHANNEL_ID)
                if history_channel:
                    await history_channel.send(f"{member.mention} ({member.id}) 已被移出服务器。原因: {reason}")
            except discord.Forbidden:
                await interaction.followup.send(f"无权限将 {member.mention} 移出服务器。", ephemeral=True)
            except discord.HTTPException as e:
                await interaction.followup.send(f"尝试将 {member.mention} 移出服务器时发生错误: {e}", ephemeral=True)
        elif action == "ban_permanent":
            # Handle permanent ban
            try:
                reason = applicable_punishment.get("description_template", "违反群规").format(count=warning_count)
                await member.ban(reason=reason)
                await interaction.followup.send(f"已将 {member.mention} 永久封禁 (原因: {reason})。", ephemeral=True)
                history_channel = interaction.guild.get_channel(self.bot.HISTORY_CHANNEL_ID)
                if history_channel:
                    await history_channel.send(f"{member.mention} ({member.id}) 已被永久封禁。原因: {reason}")
            except discord.Forbidden:
                await interaction.followup.send(f"无权限将 {member.mention} 永久封禁。", ephemeral=True)
            except discord.HTTPException as e:
                await interaction.followup.send(f"尝试将 {member.mention} 永久封禁时发生错误: {e}", ephemeral=True)

    async def _apply_mute(self, interaction: discord.Interaction, member: discord.Member, duration_minutes: int, case_id: str = None):
        """Applies a mute to a member for the specified duration."""
        if duration_minutes <= 0:
            return
            
        server_id = str(interaction.guild.id)
        user_id = str(member.id)
        mute_key = f"{server_id}-{user_id}"
        
        # Get or create muted role
        muted_role = await self.bot.get_muted_role(interaction.guild)
        if not muted_role:
            await interaction.followup.send("无法创建或获取禁言角色。", ephemeral=True)
            return
            
        # Calculate unmute time
        now = datetime.now(timezone.utc)
        unmute_at = now + timedelta(minutes=duration_minutes)
        
        # Store mute info
        mute_info = {
            "user_id": int(user_id),
            "guild_id": int(server_id),
            "muted_at": now.isoformat(),
            "unmute_at": unmute_at.isoformat(),
            "duration_minutes": duration_minutes,
            "muted_by": str(interaction.user.id),
            "case_ids_for_mute": [case_id] if case_id else []
        }
        
        # Apply mute
        try:
            # Remove verified role if applicable
            verified_role = interaction.guild.get_role(self.bot.VERIFIED_ROLE_ID)
            if verified_role and verified_role in member.roles:
                await member.remove_roles(verified_role, reason=f"Muted for {duration_minutes} minutes")
                
            # Add muted role
            await member.add_roles(muted_role, reason=f"Muted for {duration_minutes} minutes")
            
            # Save mute info
            self.bot.warning_data["active_mutes"][mute_key] = mute_info
            save_result = self.bot.save_data(self.bot.warning_data)
            
            if not save_result:
                await interaction.followup.send(f"已禁言 {member.mention} {duration_minutes} 分钟，但保存禁言数据时发生错误。", ephemeral=True)
                print(f"Error saving mute data for user {member.display_name} (ID: {user_id}) in guild {interaction.guild.name} (ID: {server_id}).")
            else:
                await interaction.followup.send(f"已禁言 {member.mention} {duration_minutes} 分钟。将在 <t:{int(unmute_at.timestamp())}:f> 解除。", ephemeral=True)
                
            # Notify history channel
            history_channel = interaction.guild.get_channel(self.bot.HISTORY_CHANNEL_ID)
            if history_channel:
                await history_channel.send(f"{member.mention} ({member.id}) 已被禁言 {duration_minutes} 分钟。将在 <t:{int(unmute_at.timestamp())}:f> 解除。")
                
            # Try to notify the user
            try:
                user_embed = discord.Embed(title=f"您已被禁言", color=discord.Color.red(), timestamp=now)
                user_embed.add_field(name="服务器", value=interaction.guild.name, inline=False)
                user_embed.add_field(name="持续时间", value=f"{duration_minutes} 分钟", inline=True)
                user_embed.add_field(name="解除时间", value=f"<t:{int(unmute_at.timestamp())}:f>", inline=True)
                user_embed.set_footer(text=f"如有疑问，请联系管理员")
                
                await member.send(embed=user_embed)
            except (discord.Forbidden, discord.HTTPException):
                # Silently fail if we can't DM the user
                pass
                
        except discord.Forbidden:
            await interaction.followup.send(f"无权限禁言 {member.mention}。", ephemeral=True)
        except discord.HTTPException as e:
            await interaction.followup.send(f"尝试禁言 {member.mention} 时发生错误: {e}", ephemeral=True)

    @app_commands.command(name="warn", description="警告一个用户")
    @app_commands.describe(member="要警告的用户")
    async def warn_slash_command(self, interaction: discord.Interaction, member: discord.Member):
        """Slash command to warn a user."""
        if not await self.bot.check_admin_role(interaction):
            return
            
        # Prevent warning the bot itself
        if member.id == self.bot.user.id:
            await interaction.response.send_message("我不能警告自己！", ephemeral=True)
            return
            
        # Prevent warning other bots
        if member.bot:
            await interaction.response.send_message("不能警告机器人用户。", ephemeral=True)
            return
            
        # Prevent warning yourself
        if member.id == interaction.user.id:
            await interaction.response.send_message("你不能警告自己！", ephemeral=True)
            return
            
        # Defer the response to allow time for the modal
        await interaction.response.defer(ephemeral=True, thinking=True)
        
        # Show the reason modal
        modal = ReasonModal(interaction, member, interaction.channel, self)
        await interaction.followup.send_modal(modal)

    @app_commands.context_menu(name="警告用户")
    async def warn_context_menu(self, interaction: discord.Interaction, member: discord.Member):
        """Context menu command to warn a user."""
        if not await self.bot.check_admin_role(interaction):
            return
            
        # Prevent warning the bot itself
        if member.id == self.bot.user.id:
            await interaction.response.send_message("我不能警告自己！", ephemeral=True)
            return
            
        # Prevent warning other bots
        if member.bot:
            await interaction.response.send_message("不能警告机器人用户。", ephemeral=True)
            return
            
        # Prevent warning yourself
        if member.id == interaction.user.id:
            await interaction.response.send_message("你不能警告自己！", ephemeral=True)
            return
            
        # Defer the response to allow time for the modal
        await interaction.response.defer(ephemeral=True, thinking=True)
        
        # Show the reason modal
        modal = ReasonModal(interaction, member, interaction.channel, self)
        await interaction.followup.send_modal(modal)

async def setup(bot: commands.Bot):
    await bot.add_cog(WarningsCog(bot))
