"""
Main file for the Discord Warning Bot.
Handles bot initialization, event handling (on_ready, setup_hook),
loading/saving data, and core utility functions.
"""
import discord
from discord.ext import commands, tasks
import json
import os
import random
import string
import asyncio
from datetime import datetime, timedelta, timezone

# --- Constants ---
TOKEN = "YOUR_BOT_TOKEN_HERE" # Replace with your actual bot token
DATA_FILE = "/home/ubuntu/discord_bot/warnings_data.json"
ADMIN_ROLE_ID = 959251532535169065
HISTORY_CHANNEL_ID = 1076394033003368449
MUTED_ROLE_NAME = "Muted"
VERIFIED_ROLE_ID = 881855912908845077 # Role to be removed on mute and restored on unmute
RULES_DATA_FILE = "/home/ubuntu/discord_bot/rules_database.json"

# --- Bot Setup ---
intents = discord.Intents.default()
intents.members = True
intents.message_content = True # Required for message content access if not using only slash commands

bot = commands.Bot(command_prefix="!", intents=intents) # Prefix is fallback, primarily using app commands

# --- Data Management ---
def load_data():
    """Loads warning data from the JSON file."""
    if os.path.exists(DATA_FILE):
        try:
            with open(DATA_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                # Ensure top-level keys exist
                if "warnings" not in data:
                    data["warnings"] = {}
                if "active_mutes" not in data:
                    data["active_mutes"] = {}
                if "member_activity" not in data: # New key for member join/leave logs
                    data["member_activity"] = {}
                return data
        except json.JSONDecodeError:
            print(f"Error decoding JSON from {DATA_FILE}. Starting with empty data.")
            return {"warnings": {}, "active_mutes": {}}
    return {"warnings": {}, "active_mutes": {}}

def save_data(data):
    """Saves warning data to the JSON file."""
    try:
        with open(DATA_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=4)
    except IOError as e:
        print(f"Error saving data to {DATA_FILE}: {e}")

# --- Utility Functions ---
def generate_case_id():
    """Generates a unique 5-character alphanumeric case ID."""
    return "".join(random.choices(string.ascii_uppercase + string.digits, k=5))

async def check_admin_role(interaction: discord.Interaction) -> bool:
    """Checks if the interacting user has the admin role."""
    if interaction.user.get_role(ADMIN_ROLE_ID):
        return True
    await interaction.response.send_message("你不是管理员！", ephemeral=True)
    return False

async def get_muted_role(guild: discord.Guild) -> discord.Role | None:
    """Gets the Muted role, creating it if it doesn't exist and bot has perms."""
    muted_role = discord.utils.get(guild.roles, name=MUTED_ROLE_NAME)
    if not muted_role:
        if guild.me.guild_permissions.manage_roles and guild.me.guild_permissions.administrator:
            try:
                permissions = discord.Permissions(
                    send_messages=False,
                    speak=False,
                    send_messages_in_threads=False,
                    add_reactions=False # Deny most communication perms
                )
                muted_role = await guild.create_role(name=MUTED_ROLE_NAME, permissions=permissions, reason="For bot mute functionality")
                print(f"Created '{MUTED_ROLE_NAME}' role in guild {guild.id}.")
                # Configure overwrites for existing channels
                for channel in guild.text_channels:
                    await channel.set_permissions(muted_role, send_messages=False, add_reactions=False)
                for channel in guild.voice_channels:
                    await channel.set_permissions(muted_role, speak=False)
                await asyncio.sleep(1) # Ensure role creation is processed
            except discord.Forbidden:
                print(f"Forbidden to create '{MUTED_ROLE_NAME}' role in guild {guild.id}.")
                return None
            except discord.HTTPException as e:
                print(f"HTTPException while creating '{MUTED_ROLE_NAME}' role: {e}")
                return None
        else:
            print(f"Bot lacks permissions to create '{MUTED_ROLE_NAME}' role in guild {guild.id}.")
            return None
    return muted_role

# --- Event Handlers ---
@bot.event
async def on_ready():
    """Called when the bot is ready and connected to Discord."""
    print(f"Logged in as {bot.user.name} (ID: {bot.user.id})")
    print("Bot is ready and listening for commands.")
    print("------")
    # Load data and start background tasks if any (mute handler will be in warnings.py)
    bot.warning_data = load_data()
    # The actual mute task loop will be started from warnings.py after cog loading

@bot.event
async def on_member_join(member: discord.Member):
    """Called when a member joins the guild."""
    server_id = str(member.guild.id)
    user_id = str(member.id)
    timestamp = int(datetime.now(timezone.utc).timestamp())

    activity_entry = {
        "type": "join",
        "timestamp": timestamp,
        "user_id": user_id,
        "guild_id": server_id
    }

    if server_id not in bot.warning_data["member_activity"]:
        bot.warning_data["member_activity"][server_id] = {}
    if user_id not in bot.warning_data["member_activity"][server_id]:
        bot.warning_data["member_activity"][server_id][user_id] = []
    
    bot.warning_data["member_activity"][server_id][user_id].append(activity_entry)
    save_data(bot.warning_data)
    print(f"Member {member.display_name} (ID: {user_id}) joined guild {member.guild.name} (ID: {server_id}). Event logged.")

async def setup_hook():
    """Asynchronously called after login but before connecting to the Websocket."""
    print("Running setup_hook...")
    bot.warning_data = load_data() # Load data early

    # Load cogs (extensions)
    try:
        await bot.load_extension("bot_warnings_cog")
        print("Loaded 'bot_warnings_cog' extension.")
        await bot.load_extension("userhistory")
        print("Loaded 'userhistory' extension.")
    except commands.ExtensionNotFound as e:
        print(f"Error loading extension: {e} - Make sure the file exists and is in the correct path.")
    except commands.ExtensionAlreadyLoaded as e:
        print(f"Extension already loaded: {e}")
    except commands.NoEntryPointError as e:
        print(f"Extension has no setup function: {e}")
    except commands.ExtensionFailed as e:
        print(f"Extension setup failed: {e}")
    except Exception as e:
        print(f"An unexpected error occurred during extension loading: {e}")

    # Sync application commands
    # It's generally better to sync commands after cogs are loaded, 
    # as commands are often defined within cogs.
    try:
        # If you have a specific guild for testing, use guild=discord.Object(id=YOUR_GUILD_ID)
        # For global commands, it might take up to an hour to propagate.
        synced_commands = await bot.tree.sync() 
        print(f"Synced {len(synced_commands)} application commands globally.")
        for cmd in synced_commands:
            print(f"- {cmd.name} (ID: {cmd.id})")
    except discord.HTTPException as e:
        print(f"Failed to sync application commands: {e}")
    except Exception as e:
        print(f"An unexpected error occurred during command syncing: {e}")

    print("setup_hook completed.")

bot.setup_hook = setup_hook

# --- Main Execution ---
if __name__ == "__main__":
    # This part is for direct execution. 
    # In a multi-file setup, you'd typically run the bot from this main.py.
    # The cogs (warnings.py, userhistory.py) will be loaded by the bot.
    
    # Ensure the discord_bot directory exists for DATA_FILE
    os.makedirs(os.path.dirname(DATA_FILE), exist_ok=True)

    async def main():
        async with bot:
            await bot.start(TOKEN)

    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("Bot shutdown requested by user.")
    except Exception as e:
        print(f"An error occurred while running the bot: {e}")
    finally:
        print("Bot has been shut down.")


