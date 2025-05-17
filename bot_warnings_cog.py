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
                        self.bot.save_data(self.bot.warning_data)
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
            self.bot.save_data(self.bot.warning_data)
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
                return json.load(f)
        except FileNotFoundError:
            print(f"ERROR: Rules data file not found at {self.bot.RULES_DATA_FILE}. Returning empty rules.")
            return {"rules": [], "general_punishment_ladder": []}
        except json.JSONDecodeError as e:
            print(f"ERROR: Could not decode JSON from {self.bot.RULES_DATA_FILE}: {e}. Returning empty rules.")
            return {"rules": [], "general_punishment_ladder": []}
        except AttributeError:
            print(f"ERROR: bot.RULES_DATA_FILE not set. Ensure RULES_DATA_FILE is passed from main.py during setup. Returning empty rules.")
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

        notification_embed = discord.Embed(title="用户警告", description=f"{member.mention} 您已被警告。", color=discord.Color.red(), timestamp=datetime.fromtimestamp(timestamp, timezone.utc))
        notification_embed.add_field(name="理由", value=displayed_reason, inline=False)
        notification_embed.add_field(name=f"这是您第 {total_warnings_overall} 次被记录的有效警告。", value="请注意您的言行，遵守服务器规则。", inline=False)
        if matched_rule_id: notification_embed.add_field(name="涉及规则编号", value=matched_rule_id, inline=True)
        notification_embed.set_footer(text=f"Case ID: {case_id}")
        try:
            notif_msg = await target_channel.send(embed=notification_embed)
            warning_entry["message_id_notification_channel"] = notif_msg.id
            # Followup for the original interaction (slash command or context menu)
            if not original_interaction.response.is_done(): await original_interaction.response.send_message(f"已在 {target_channel.mention} 中警告 {member.mention} (Case ID: {case_id}).", ephemeral=True)
            else: await original_interaction.followup.send(f"已在 {target_channel.mention} 中警告 {member.mention} (Case ID: {case_id}).", ephemeral=True)
        except discord.Forbidden:
            if not original_interaction.response.is_done(): await original_interaction.response.send_message(f"警告已记录 (Case ID: {case_id})，但无法在 {target_channel.mention} 发送通知 (权限不足)。", ephemeral=True)
            else: await original_interaction.followup.send(f"警告已记录 (Case ID: {case_id})，但无法在 {target_channel.mention} 发送通知 (权限不足)。", ephemeral=True)
        except discord.HTTPException as e:
            if not original_interaction.response.is_done(): await original_interaction.response.send_message(f"警告已记录 (Case ID: {case_id})，但发送通知时发生HTTP错误: {e}。", ephemeral=True)
            else: await original_interaction.followup.send(f"警告已记录 (Case ID: {case_id})，但发送通知时发生HTTP错误: {e}。", ephemeral=True)

        self.bot.save_data(self.bot.warning_data)
        await self._apply_punishment_based_on_rules(original_interaction, member, matched_rule_id, total_warnings_overall, rule_specific_actions, case_id)

    async def _apply_punishment_based_on_rules(self, interaction: discord.Interaction, member: discord.Member,
                                           matched_rule_id: str | None,
                                           total_overall_warnings: int,
                                           specific_actions_from_rule_definition: list | None,
                                           case_id: str):
        guild = interaction.guild
        punishment_applied_messages = [] 
        action_taken = False
        followup_interaction = interaction # Use the original interaction for followups here

        if specific_actions_from_rule_definition:
            print(f"[DEBUG] Applying specific actions for rule {matched_rule_id}: {specific_actions_from_rule_definition}")
            action_taken = True 
            for action_def in specific_actions_from_rule_definition:
                action_type = action_def.get("type")
                reason_template = action_def.get("reason_template", f"违反规则 {matched_rule_id}")
                details_for_reason = action_def.get("details", f"违反规则 {matched_rule_id}")
                reason_for_action = reason_template.format(member_mention=member.mention, case_id=case_id, details=details_for_reason)

                if action_type == "permanent_remove_from_group":
                    try:
                        await member.ban(reason=f"{reason_for_action} (Case ID: {case_id})")
                        punishment_applied_messages.append(f"由于 {details_for_reason}, {member.mention} 已被永久移出服务器 (Case ID: {case_id}).")
                    except discord.Forbidden:
                        punishment_applied_messages.append(f"机器人权限不足，无法将 {member.mention} 永久移出服务器 (Case ID: {case_id}).")
                    except discord.HTTPException as e:
                        punishment_applied_messages.append(f"永久移出 {member.mention} 时发生HTTP错误: {e} (Case ID: {case_id}).")
                
                elif action_type == "revoke_admin_role":
                    punishment_applied_messages.append(f"管理员 {member.mention} 因 {details_for_reason} (规则 {matched_rule_id}, Case ID: {case_id})，其职位已被记录为撤销。实际操作可能需要进一步配置或手动进行。")

                elif action_type == "monitor_nickname_compliance":
                    print(f"[INFO] Rule {matched_rule_id} (monitor_nickname_compliance) for {member.mention} noted. No immediate punitive action from this warning.")
                    action_taken = False 
                else:
                    print(f"[WARNING] Unknown specific action type: {action_type} for rule {matched_rule_id}")
                    punishment_applied_messages.append(f"规则 {matched_rule_id} 定义了未知类型的特定操作 \'{action_type}\'.")

        apply_general_ladder = False
        if not action_taken: 
            if not specific_actions_from_rule_definition: 
                apply_general_ladder = True
            elif matched_rule_id:
                matched_rule_def = next((rule for rule in self.rules_data.get("rules", []) if rule.get("id") == matched_rule_id), None)
                if matched_rule_def and matched_rule_def.get("action_type") == "general_violation":
                    apply_general_ladder = True

        if apply_general_ladder:
            print(f"[DEBUG] Applying general punishment ladder for {member.mention}. Total overall warnings: {total_overall_warnings}")
            general_punishments_config = self.rules_data.get("general_punishment_ladder", [])
            applicable_punishment_def = None
            for pun_def in sorted(general_punishments_config, key=lambda x: x["threshold"], reverse=True):
                if total_overall_warnings >= pun_def["threshold"]:
                    applicable_punishment_def = pun_def
                    break

            if applicable_punishment_def:
                action_taken = True 
                action = applicable_punishment_def["action"]
                description_template = applicable_punishment_def.get("description_template", "已达到处罚阈值 ({count}次警告)。")
                punishment_reason_text = description_template.format(count=total_overall_warnings, member_mention=member.mention, case_id=case_id)

                if action == "mute":
                    duration_minutes = applicable_punishment_def.get("duration_minutes", 0)
                    duration_hours = applicable_punishment_def.get("duration_hours", 0)
                    mute_duration = timedelta(minutes=duration_minutes, hours=duration_hours)

                    if mute_duration.total_seconds() > 0:
                        muted_role = await self.bot.get_muted_role(guild)
                        if not muted_role:
                            punishment_applied_messages.append(f"无法获取或创建 Muted 角色。对 {member.mention} 的禁言失败 ({punishment_reason_text}; Case ID: {case_id}).")
                        else:
                            unmute_at = datetime.now(timezone.utc) + mute_duration
                            try:
                                verified_role = guild.get_role(self.bot.VERIFIED_ROLE_ID)
                                if verified_role and verified_role in member.roles:
                                    await member.remove_roles(verified_role, reason=f"禁言处罚 (Case ID: {case_id})")
                                await member.add_roles(muted_role, reason=f"禁言处罚 (Case ID: {case_id})")
                                
                                server_id_str = str(guild.id)
                                user_id_str = str(member.id)
                                self.bot.warning_data["active_mutes"][f"{server_id_str}-{user_id_str}"] = {
                                    "unmute_at": unmute_at.isoformat(),  # 确保始终存储为ISO格式字符串
                                    "guild_id": guild.id,
                                    "user_id": member.id,
                                    "case_ids_for_mute": [case_id] 
                                }
                                self.bot.save_data(self.bot.warning_data)
                                punishment_applied_messages.append(f"{member.mention} 已被禁言直到 <t:{int(unmute_at.timestamp())}:F> ({punishment_reason_text}; Case ID: {case_id}).")
                            except discord.Forbidden:
                                punishment_applied_messages.append(f"机器人权限不足，无法为 {member.mention} 添加 Muted 角色或移除 Verified 角色 ({punishment_reason_text}; Case ID: {case_id}).")
                            except discord.HTTPException as e:
                                punishment_applied_messages.append(f"为 {member.mention} 添加 Muted 角色时发生HTTP错误: {e} ({punishment_reason_text}; Case ID: {case_id}).")
                    else:
                        punishment_applied_messages.append(f"禁言处罚已触发 ({punishment_reason_text})，但时长为0。未执行禁言。 (Case ID: {case_id})")
                
                elif action == "remove_temporary": 
                    can_rejoin = applicable_punishment_def.get("can_rejoin", True)
                    try:
                        await member.kick(reason=f"{punishment_reason_text} (Case ID: {case_id})")
                        rejoin_status = "可以" if can_rejoin else "不可以"
                        punishment_applied_messages.append(f"{member.mention} 已被移出服务器 ({punishment_reason_text}; {rejoin_status}重新加入; Case ID: {case_id}).")
                    except discord.Forbidden:
                        punishment_applied_messages.append(f"机器人权限不足，无法将 {member.mention} 移出服务器 ({punishment_reason_text}; Case ID: {case_id}).")
                    except discord.HTTPException as e:
                        punishment_applied_messages.append(f"移出 {member.mention} 时发生HTTP错误: {e} ({punishment_reason_text}; Case ID: {case_id}).")

                elif action == "ban_permanent": 
                    try:
                        await member.ban(reason=f"{punishment_reason_text} (Case ID: {case_id})")
                        punishment_applied_messages.append(f"{member.mention} 已被永久禁止加入服务器 ({punishment_reason_text}; Case ID: {case_id}).")
                    except discord.Forbidden:
                        punishment_applied_messages.append(f"机器人权限不足，无法将 {member.mention} 永久禁止加入服务器 ({punishment_reason_text}; Case ID: {case_id}).")
                    except discord.HTTPException as e:
                        punishment_applied_messages.append(f"永久禁止 {member.mention} 加入服务器时发生HTTP错误: {e} ({punishment_reason_text}; Case ID: {case_id}).")
                else:
                    print(f"[WARNING] Unknown general punishment action type: {action} for threshold {applicable_punishment_def['threshold']}")
                    punishment_applied_messages.append(f"为 {member.mention} 应用了处罚 ({punishment_reason_text})，但具体操作 \'{action}\' 未被完全识别 (Case ID: {case_id}).")
            else:
                print(f"[DEBUG] No general punishment threshold met for user {member.id} with {total_overall_warnings} warnings.")

        if punishment_applied_messages:
            full_punishment_summary = "\n".join(punishment_applied_messages)
            if not followup_interaction.response.is_done(): await followup_interaction.response.send_message(f"**处罚结果 (Case ID: {case_id}):**\n{full_punishment_summary}", ephemeral=True)
            else: await followup_interaction.followup.send(f"**处罚结果 (Case ID: {case_id}):**\n{full_punishment_summary}", ephemeral=True)
        elif action_taken: 
            if not followup_interaction.response.is_done(): await followup_interaction.response.send_message(f"针对 Case ID: {case_id} 的特定规则处理已启动。", ephemeral=True)
            else: await followup_interaction.followup.send(f"针对 Case ID: {case_id} 的特定规则处理已启动。", ephemeral=True)

    @app_commands.command(name="warn", description="警告一名用户，记录并可能触发禁言。")
    @app_commands.describe(
        member="要警告的用户",
        reason_direct="警告理由 (可输入规则编号)", 
        channel="在哪个频道发送警告通知 (默认当前频道)"
    )
    async def warn_slash_command(self, interaction: discord.Interaction, member: discord.Member, reason_direct: str, channel: discord.TextChannel = None):
        if not await self.bot.check_admin_role(interaction):
            return
        target_channel = channel or interaction.channel
        # Defer the slash command interaction before calling _handle_warning
        await interaction.response.defer(ephemeral=True, thinking=True) 
        await self._handle_warning(interaction, member, reason_direct, target_channel)

    @warn_slash_command.error
    async def warn_slash_command_error_handler(self, interaction: discord.Interaction, error: app_commands.AppCommandError):
        print(f"Error in warn slash command: {error}")
        traceback.print_exc()
        error_message = f"执行警告命令时发生未知错误: {error}"
        if isinstance(error, app_commands.CommandInvokeError) and error.original:
            error_message = f"执行警告命令时发生内部错误: {error.original}"
        elif isinstance(error, app_commands.CheckFailure):
            error_message = "您没有权限使用此命令或检查失败。"
        
        if not interaction.response.is_done():
            await interaction.response.send_message(error_message, ephemeral=True)
        else:
            await interaction.followup.send(error_message, ephemeral=True)

async def setup(bot: commands.Bot):
    from main import ADMIN_ROLE_ID, HISTORY_CHANNEL_ID, MUTED_ROLE_NAME, VERIFIED_ROLE_ID, DATA_FILE, RULES_DATA_FILE
    from main import load_data, save_data, generate_case_id, check_admin_role, get_muted_role
    bot.ADMIN_ROLE_ID = ADMIN_ROLE_ID
    bot.HISTORY_CHANNEL_ID = HISTORY_CHANNEL_ID
    bot.MUTED_ROLE_NAME = MUTED_ROLE_NAME
    bot.VERIFIED_ROLE_ID = VERIFIED_ROLE_ID
    bot.DATA_FILE = DATA_FILE
    bot.RULES_DATA_FILE = RULES_DATA_FILE
    bot.load_data = load_data
    bot.save_data = save_data
    bot.generate_case_id = generate_case_id
    bot.check_admin_role = check_admin_role
    bot.get_muted_role = get_muted_role
    if not hasattr(bot, 'warning_data'): bot.warning_data = bot.load_data()
    
    cog_instance = WarningsCog(bot)
    await bot.add_cog(cog_instance)
    print("WarningsCog loaded and added to bot.")
    
    # Context Menu for Warning User
    async def warn_user_context_menu_callback(interaction: discord.Interaction, member: discord.Member):
        # Use cog_instance for bot methods if they are part of the cog or bot instance passed to cog
        if not await cog_instance.bot.check_admin_role(interaction): return
        target_channel = interaction.channel
        # Pass the cog_instance to the modal so it can call _handle_warning
        modal = ReasonModal(original_command_interaction=interaction, target_user=member, target_channel=target_channel, cog_instance=cog_instance)
        await interaction.response.send_modal(modal)
        
    warn_user_context_menu = app_commands.ContextMenu(name="警告用户", callback=warn_user_context_menu_callback)
    bot.tree.add_command(warn_user_context_menu)
    print("Added '警告用户' context menu to the command tree.")
