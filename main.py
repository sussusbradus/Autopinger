import asyncio
import json
import os
import re
from dotenv import load_dotenv

import discord
from discord import app_commands
from discord.ext import commands

load_dotenv()
token = os.getenv("token")

# Set this to your server's ID for instant command updates while developing.
# Leave as None to sync globally (can take up to an hour to propagate).
GUILD_ID = None

# --- BANLIST MANAGEMENT ---
BANLIST_FILE = "banlist.jsonc" if os.path.exists("banlist.jsonc") else "banlist.json"


def load_banlist():
    if not os.path.exists(BANLIST_FILE):
        with open(BANLIST_FILE, "w") as f:
            f.write("[\n  // Add user IDs below as integers or strings\n]\n")
        return set()

    try:
        with open(BANLIST_FILE, "r", encoding="utf-8") as f:
            content = f.read()

        cleaned_content = re.sub(
            r"//.*?\n|/\*.*?\*/", "", content, flags=re.DOTALL
        )
        data = json.loads(cleaned_content)

        # Convert all entries to integer IDs safely
        return {int(uid) for uid in data if str(uid).isdigit()}
    except Exception as e:
        print(f"Error loading {BANLIST_FILE}: {e}")
        return set()


banned_users = load_banlist()

class BanlistTree(app_commands.CommandTree):
    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id in banned_users:
            await interaction.response.send_message(
                "yea no fuck you :joy:", ephemeral=True
            )
            return False
        return True


class Autopinger(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True
        super().__init__(
            command_prefix="a!",
            intents=intents,
            help_command=None,
            tree_cls=BanlistTree,
        )

    async def setup_hook(self):
        if GUILD_ID:
            guild = discord.Object(id=GUILD_ID)
            self.tree.copy_global_to(guild=guild)
            synced = await self.tree.sync(guild=guild)
        else:
            synced = await self.tree.sync()
        print(f"Synced {len(synced)} slash command(s)")


bot = Autopinger()

active_loops = {}


# startup message
@bot.event
async def on_ready():
    print(f"Logged in as {bot.user}")
    print(f"Loaded {len(banned_users)} banned user ID(s): {banned_users}")


# logger
@bot.event
async def on_message(message):
    if message.author.bot:
        return

    print(f"[{message.guild}] {message.author}: {message.content}")

    await bot.process_commands(message)


@bot.tree.error
async def on_app_command_error(
    interaction: discord.Interaction, error: app_commands.AppCommandError
):
    if isinstance(error, app_commands.CheckFailure):
        return
    print(f"Command error: {error}")
    if interaction.response.is_done():
        await interaction.followup.send(f"Something broke: {error}", ephemeral=True)
    else:
        await interaction.response.send_message(
            f"Something broke: {error}", ephemeral=True
        )


# help command
@bot.tree.command(name="help", description="List the available commands")
async def help_command(interaction: discord.Interaction):
    await interaction.response.send_message(
        "Available commands as of now:\n"
        "/help\n"
        "/version\n"
        "/send (Your message)\n"
        "/repeat (Times, Message)\n"
        "/infsend (Your message)\n"
        "/stop (Quit current ping loop)\n"
        "/stopall (Quit all ping loops)\n"
    )


# test
@bot.tree.command(name="test", description="Check that the bot is responding")
async def test(interaction: discord.Interaction):
    await interaction.response.send_message("I am alive and well!")


# version
@bot.tree.command(name="version", description="Show the current bot version")
async def version(interaction: discord.Interaction):
    await interaction.response.send_message("Autopinger (Name not final) Alpha 0.7.6")


# send a singular message
@bot.tree.command(name="send", description="Send a single message")
@app_commands.describe(message="The message to send")
async def send(interaction: discord.Interaction, message: str):
    await interaction.response.send_message(message)


# not inf loop
async def _repeat_loop(channel, times, message):
    try:
        for _ in range(times):
            await channel.send(message)
            await asyncio.sleep(1)
    except asyncio.CancelledError:
        pass
    finally:
        if active_loops.get(channel.id) is asyncio.current_task():
            del active_loops[channel.id]


# infsend loop
async def _infsend_loop(channel, message):
    try:
        while True:
            await channel.send(message)
            await asyncio.sleep(1)
    except asyncio.CancelledError:
        pass
    finally:
        if active_loops.get(channel.id) is asyncio.current_task():
            del active_loops[channel.id]


# multiple loops in same channel prevention
@bot.tree.command(name="repeat", description="Send a message a set number of times")
@app_commands.describe(times="How many times to send it", message="The message to send")
async def repeat(
    interaction: discord.Interaction,
    times: app_commands.Range[int, 1, 1000],
    message: str,
):
    if interaction.channel_id in active_loops:
        await interaction.response.send_message(
            "A loop is already running in this channel. Use /stop first",
            ephemeral=True,
        )
        return

    await interaction.response.send_message(
        f"Repeating that {times} time(s)", ephemeral=True
    )
    task = asyncio.create_task(_repeat_loop(interaction.channel, times, message))
    active_loops[interaction.channel_id] = task


# infsend
@bot.tree.command(name="infsend", description="Send a message on a loop until stopped")
@app_commands.describe(message="The message to send")
async def infsend(interaction: discord.Interaction, message: str):
    if interaction.channel_id in active_loops:
        await interaction.response.send_message(
            "A loop is already running in this channel. Use /stop first",
            ephemeral=True,
        )
        return

    await interaction.response.send_message("Loop started", ephemeral=True)
    task = asyncio.create_task(_infsend_loop(interaction.channel, message))
    active_loops[interaction.channel_id] = task


# stop
@bot.tree.command(name="stop", description="Stop the loop running in this channel")
async def stop(interaction: discord.Interaction):
    task = active_loops.pop(interaction.channel_id, None)
    if task:
        task.cancel()
        await interaction.response.send_message("Ping loop stopped")
    else:
        await interaction.response.send_message(
            "No loops running in this channel right now"
        )


# stop all
@bot.tree.command(name="stopall", description="Stop every running loop")
async def stopall(interaction: discord.Interaction):
    if not active_loops:
        await interaction.response.send_message(
            "No loops running in any channel right now"
        )
        return

    count = len(active_loops)
    for task in list(active_loops.values()):
        task.cancel()
    active_loops.clear()

    await interaction.response.send_message(f"Stopped {count} running loop(s)")


# manual resync, owner only
@bot.command()
@commands.is_owner()
async def sync(ctx):
    if GUILD_ID:
        guild = discord.Object(id=GUILD_ID)
        bot.tree.copy_global_to(guild=guild)
        synced = await bot.tree.sync(guild=guild)
    else:
        synced = await bot.tree.sync()
    await ctx.send(f"Synced {len(synced)} command(s)")


bot.run(token)