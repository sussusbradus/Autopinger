import asyncio
import io
import json
import os
import random
import re
import string
from typing import List, Optional

from dotenv import load_dotenv

import discord
from discord import app_commands
from discord.ext import commands

load_dotenv()
token = os.getenv("token")

GUILD_ID = None

# Saved files sit next to main.py no matter where the bot is launched from
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_FILE = os.path.join(BASE_DIR, "data.json")  # created and managed by the bot itself
LEGACY_PERMISSIONS_FILE = os.path.join(BASE_DIR, "permissions.json")  # imported if found
LEGACY_BANLIST_FILES = (  # old versions; imported once
    os.path.join(BASE_DIR, "banlist.jsonc"),
    os.path.join(BASE_DIR, "banlist.json"),
)

# Commands only admins and the bot owner can use until a role is allowed with
# /perms allow (for example: /perms allow command:ban role:@Moderators)
LOCKED_BY_DEFAULT = {"ban", "unban", "banlist"}

# Delay (in seconds) between messages for loops
DEFAULT_DELAY = 1.0
MIN_DELAY = 1.0
MAX_DELAY = 3600.0

# Characters used by /randsend (edit this to change what random messages look like)
RANDOM_CHARS = string.ascii_letters + string.digits

# Most channels one command can target at once
MAX_CHANNELS = 10

# Biggest image the bot will attach (bytes)
MAX_IMAGE_BYTES = 8 * 1024 * 1024


def make_random(length):
    return "".join(random.choices(RANDOM_CHARS, k=length))


# ------------------------------------------------------------ saved data
# Everything the bot remembers lives in data.json, which it creates by itself:
# {
#   "permissions": { "<guild id>": { "<command>": {"roles": [...], "users": [...]} } },
#   "bans": { "global": [user ids], "guilds": { "<guild id>": [user ids] } },
#   "access": { "<guild id>": {"mode": "everyone" | "selected", "roles": [...], "users": [...]} }
# }
# A command with no permissions entry is open to everyone, except the commands
# in LOCKED_BY_DEFAULT. Bans are managed with /ban, /unban and /banlist.
def read_legacy_banlist():
    """Old banlist.jsonc / banlist.json: read once so existing bans aren't lost."""
    for name in LEGACY_BANLIST_FILES:
        if not os.path.exists(name):
            continue
        try:
            with open(name, "r", encoding="utf-8") as f:
                content = f.read()
            cleaned = re.sub(r"//.*?\n|/\*.*?\*/", "", content, flags=re.DOTALL)
            return {int(uid) for uid in json.loads(cleaned) if str(uid).isdigit()}
        except Exception as e:
            print(f"Couldn't read {name}: {e}")
    return None


def write_data(payload):
    try:
        with open(DATA_FILE, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2)
    except OSError as e:
        print(f"Error saving {DATA_FILE}: {e}")


def load_data():
    data = {}
    try:
        if os.path.exists(DATA_FILE):
            with open(DATA_FILE, "r", encoding="utf-8") as f:
                loaded = json.load(f)
            if isinstance(loaded, dict):
                data = loaded
        elif os.path.exists(LEGACY_PERMISSIONS_FILE):
            with open(LEGACY_PERMISSIONS_FILE, "r", encoding="utf-8") as f:
                legacy = json.load(f)
            if isinstance(legacy, dict):
                data["permissions"] = legacy
    except Exception as e:
        print(f"Error loading saved data: {e}")

    if not isinstance(data.get("permissions"), dict):
        data["permissions"] = {}
    if not isinstance(data.get("access"), dict):
        data["access"] = {}
    ban_data = data.get("bans")
    if not isinstance(ban_data, dict):
        ban_data = data["bans"] = {}
    if not isinstance(ban_data.get("global"), list):
        ban_data["global"] = []
    if not isinstance(ban_data.get("guilds"), dict):
        ban_data["guilds"] = {}

    # One-time import of the old banlist file (it is never created any more)
    if not data.get("legacy_banlist_imported"):
        legacy_ids = read_legacy_banlist()
        if legacy_ids is not None:
            ban_data["global"] = sorted(set(ban_data["global"]) | legacy_ids)
            data["legacy_banlist_imported"] = True
            write_data(data)
            print(f"Imported {len(legacy_ids)} ban(s) from the old banlist file. "
                  "You can delete it now.")
    return data


data = load_data()
permissions = data["permissions"]
bans = data["bans"]


def save_data():
    write_data(data)


def is_banned(user_id, guild_id):
    if user_id in bans["global"]:
        return True
    return guild_id is not None and user_id in bans["guilds"].get(str(guild_id), [])


def get_rule(guild_id, command, create=False):
    if create:
        guild_rules = permissions.setdefault(str(guild_id), {})
    else:
        guild_rules = permissions.get(str(guild_id), {})
    rule = guild_rules.get(command)
    if rule is None and create:
        rule = {"roles": [], "users": []}
        guild_rules[command] = rule
    if rule is not None:
        rule.setdefault("roles", [])
        rule.setdefault("users", [])
    return rule


async def can_manage(interaction: discord.Interaction) -> bool:
    """Bot owner or a server administrator: always allowed, can edit permissions."""
    user = interaction.user
    if await bot.is_owner(user):
        return True
    return isinstance(user, discord.Member) and user.guild_permissions.administrator


async def can_use(interaction: discord.Interaction, command_name: str) -> bool:
    if interaction.guild is None:
        return True
    rule = get_rule(interaction.guild.id, command_name)
    if rule is None:
        if command_name in LOCKED_BY_DEFAULT:
            return await can_manage(interaction)
        return True
    if await can_manage(interaction):
        return True
    if interaction.user.id in rule["users"]:
        return True
    role_ids = {r.id for r in getattr(interaction.user, "roles", [])}
    return bool(role_ids & set(rule["roles"]))


def get_access(guild_id):
    """This server's /setup settings (created on first use)."""
    access = data["access"].setdefault(str(guild_id), {})
    if access.get("mode") not in ("everyone", "selected"):
        access["mode"] = "everyone"
    access.setdefault("roles", [])
    access.setdefault("users", [])
    return access


async def has_bot_access(interaction: discord.Interaction) -> bool:
    """False if this server limited the bot to selected people and this user isn't one."""
    if interaction.guild is None:
        return True
    access = data["access"].get(str(interaction.guild.id))
    if not access or access.get("mode") != "selected":
        return True
    if await can_manage(interaction):  # admins and the bot owner can't be locked out
        return True
    if interaction.user.id in access.get("users", []):
        return True
    role_ids = {r.id for r in getattr(interaction.user, "roles", [])}
    return bool(role_ids & set(access.get("roles", [])))


class BanlistTree(app_commands.CommandTree):
    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        is_command = interaction.type is discord.InteractionType.application_command

        if is_banned(interaction.user.id, interaction.guild_id):
            if is_command:
                await interaction.response.send_message(
                    "yea no fuck you :joy:", ephemeral=True
                )
            return False

        if not await has_bot_access(interaction):
            if is_command:
                await interaction.response.send_message(
                    "This server has limited who can use the bot. "
                    "Ask an admin to add you with /setup.",
                    ephemeral=True,
                )
            return False

        # Autocomplete requests only fetch suggestions, so skip permission rules
        if not is_command:
            return True

        name = (interaction.data or {}).get("name")
        if name and name not in ("perms", "setup") and not await can_use(interaction, name):
            await interaction.response.send_message(
                f"You don't have permission to use /{name} here.", ephemeral=True
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
            allowed_mentions=discord.AllowedMentions(
                everyone=False, roles=False, users=True
            ),
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

# channel id -> running loop task (one loop per channel)
active_loops = {}


# startup message
@bot.event
async def on_ready():
    print(f"Logged in as {bot.user}")
    print(f"Loaded {len(bans['global'])} global ban(s) and "
          f"{sum(len(v) for v in bans['guilds'].values())} server ban(s)")


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


# --------------------------------------------------------- shared helpers
def label(channel):
    """Clickable channel mention (DMs have no mention)."""
    return getattr(channel, "mention", "this chat")


def check_image(attachment):
    """Returns an error message if the attachment can't be used, else None."""
    if attachment is None:
        return None
    if not (attachment.content_type or "").startswith("image/"):
        return "That file isn't an image."
    if attachment.size > MAX_IMAGE_BYTES:
        return f"That image is too big (max {MAX_IMAGE_BYTES // (1024 * 1024)} MB)."
    return None


async def read_image(attachment):
    """Download the image once; the same bytes are reused for every message."""
    if attachment is None:
        return None
    return (await attachment.read(), attachment.filename)


def make_file(prepared):
    if prepared is None:
        return None
    data, filename = prepared
    return discord.File(io.BytesIO(data), filename=filename)


async def need_content(interaction, message, image):
    """Reply with an error and return True if there's nothing to send."""
    if not message and image is None:
        await interaction.response.send_message(
            "Give me a message, an image, or both", ephemeral=True
        )
        return True
    return False


async def safe_send(channel, content, prepared):
    """Send one message. Returns False if sending fails so loops can stop."""
    try:
        if prepared is not None:
            await channel.send(content, file=make_file(prepared))
        else:
            await channel.send(content)
        return True
    except discord.HTTPException as e:  # includes Forbidden / NotFound
        print(f"Stopped sending in {channel}: {e}")
        return False


def parse_channels(guild, text):
    """Turn 'a, #b <#123> 456' into (channels found, names that weren't found)."""
    found, unknown, seen = [], [], set()
    for part in re.split(r"[,\s]+", text.strip()):
        if not part:
            continue
        match = re.fullmatch(r"<#(\d+)>|(\d{15,20})", part)
        if match:
            channel = guild.get_channel_or_thread(int(match.group(1) or match.group(2)))
        else:
            name = part.lstrip("#").lower()
            channel = discord.utils.find(
                lambda c: c.name.lower() == name, guild.text_channels
            )
        if channel is None or not isinstance(channel, discord.abc.Messageable):
            unknown.append(part)
            continue
        if channel.id not in seen:
            seen.add(channel.id)
            found.append(channel)
    return found, unknown


def resolve_targets(interaction, channels_text, needs_files=False, check_busy=True):
    """Work out which channels a command should use. Returns (channels, notes)."""
    notes = []
    guild = interaction.guild

    # No channels given (or in a DM): just use the current channel
    if guild is None or not channels_text or not channels_text.strip():
        channel = interaction.channel
        if check_busy and channel.id in active_loops:
            return [], ["A loop is already running in this channel. Use /stop first"]
        return [channel], notes

    candidates, unknown = parse_channels(guild, channels_text)
    if unknown:
        notes.append("Couldn't find: " + ", ".join(unknown))

    targets = []
    for ch in candidates:
        user_perms = ch.permissions_for(interaction.user)
        bot_perms = ch.permissions_for(guild.me)
        if not (user_perms.view_channel and user_perms.send_messages):
            # Stops people using the bot to post where they couldn't themselves
            notes.append(f"You can't send messages in {ch.mention}")
        elif not bot_perms.send_messages:
            notes.append(f"I can't send messages in {ch.mention}")
        elif needs_files and not bot_perms.attach_files:
            notes.append(f"I can't attach files in {ch.mention}")
        elif check_busy and ch.id in active_loops:
            notes.append(f"A loop is already running in {ch.mention}")
        else:
            targets.append(ch)

    if len(targets) > MAX_CHANNELS:
        notes.append(f"Only the first {MAX_CHANNELS} channels were used")
        targets = targets[:MAX_CHANNELS]
    return targets, notes


async def channels_autocomplete(
    interaction: discord.Interaction, current: str
) -> List[app_commands.Choice[str]]:
    """Suggests channel names; keeps earlier comma-separated picks in the value."""
    guild = interaction.guild
    if guild is None:
        return []
    head, _, tail = current.rpartition(",")
    tail = tail.strip().lstrip("#").lower()
    prefix = head.strip() + ", " if head.strip() else ""
    choices = []
    for ch in guild.text_channels:
        if tail and tail not in ch.name.lower():
            continue
        if not ch.permissions_for(interaction.user).view_channel:
            continue
        value = f"{prefix}#{ch.name}"
        if len(value) > 100:
            continue
        choices.append(app_commands.Choice(name=value, value=value))
        if len(choices) >= 25:
            break
    return choices


# ---------------------------------------------------------------- loops
async def _run_loop(channel, get_content, times, delay, prepared):
    """times == 0 means keep going until stopped."""
    try:
        sent = 0
        while times == 0 or sent < times:
            if not await safe_send(channel, get_content(), prepared):
                break
            sent += 1
            await asyncio.sleep(delay)
    except asyncio.CancelledError:
        pass
    finally:
        if active_loops.get(channel.id) is asyncio.current_task():
            del active_loops[channel.id]


async def start_loops(
    interaction, *, summary, get_content, times, delay, image, channels
):
    """Shared by /repeat, /infsend and /randsend: one loop per target channel."""
    error = check_image(image)
    if error:
        await interaction.response.send_message(error, ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)
    prepared = await read_image(image)

    targets, notes = resolve_targets(
        interaction, channels, needs_files=image is not None
    )
    if not targets:
        await interaction.followup.send(
            "Nothing started.\n" + "\n".join(f"- {n}" for n in notes)
        )
        return

    for ch in targets:
        task = asyncio.create_task(_run_loop(ch, get_content, times, delay, prepared))
        active_loops[ch.id] = task

    text = f"{summary} in {', '.join(label(c) for c in targets)}"
    if notes:
        text += "\n" + "\n".join(f"- {n}" for n in notes)
    await interaction.followup.send(text)


# ------------------------------------------------------------- commands
# help command
@bot.tree.command(name="help", description="List the available commands")
async def help_command(interaction: discord.Interaction):
    await interaction.response.send_message(
        "Available commands as of now:\n"
        "/help\n"
        "/version\n"
        "/send (Message and/or image, Channels - optional)\n"
        "/repeat (Times, Message and/or image, Delay, Channels)\n"
        "/infsend (Message and/or image, Delay, Channels)\n"
        "/randsend (Mode, Length, Times, Image, Delay, Channels)\n"
        "/stop (Quit ping loops here, or in the Channels you list)\n"
        "/stopall (Quit all ping loops)\n"
        "/ban, /unban, /banlist (Admins, or roles allowed with /perms - block people from the bot)\n"
        "/setup (Admins only - panel for bot access, per-command permissions and bans)\n"
        "/perms allow|remove|lock|reset|list (Admins only - control who can use which command)\n"
        "Channels: separate several with commas, e.g. #one, #two\n"
    )


# test
@bot.tree.command(name="test", description="Check that the bot is responding")
async def test(interaction: discord.Interaction):
    await interaction.response.send_message("I am alive and well!")


# version
@bot.tree.command(name="version", description="Show the current bot version")
async def version(interaction: discord.Interaction):
    await interaction.response.send_message("Autopinger (Name not final) Release 1")


# send a singular message
@bot.tree.command(name="send", description="Send a single message")
@app_commands.describe(
    message="The message to send",
    image="An image to attach",
    channels="Channels to send in, separated by commas (default: this channel)",
)
@app_commands.autocomplete(channels=channels_autocomplete)
async def send(
    interaction: discord.Interaction,
    message: Optional[str] = None,
    image: Optional[discord.Attachment] = None,
    channels: Optional[str] = None,
):
    if await need_content(interaction, message, image):
        return
    error = check_image(image)
    if error:
        await interaction.response.send_message(error, ephemeral=True)
        return

    multi = bool(channels and channels.strip() and interaction.guild)

    # Plain text in this channel: same as before
    if image is None and not multi:
        await interaction.response.send_message(message)
        return

    await interaction.response.defer(ephemeral=multi)
    prepared = await read_image(image)

    if not multi:
        # file=None isn't accepted by followup.send, so only pass it when there is one
        kwargs = {"file": make_file(prepared)} if prepared is not None else {}
        await interaction.followup.send(message, **kwargs)
        return

    targets, notes = resolve_targets(
        interaction, channels, needs_files=image is not None, check_busy=False
    )
    sent_to = []
    for ch in targets:
        if await safe_send(ch, message, prepared):
            sent_to.append(label(ch))
        else:
            notes.append(f"Couldn't send in {label(ch)}")

    text = ("Sent in " + ", ".join(sent_to)) if sent_to else "Nothing was sent."
    if notes:
        text += "\n" + "\n".join(f"- {n}" for n in notes)
    await interaction.followup.send(text)


# multiple loops in same channel prevention happens in resolve_targets
@bot.tree.command(name="repeat", description="Send a message a set number of times")
@app_commands.describe(
    times="How many times to send it",
    message="The message to send",
    image="An image to attach to every message",
    delay="Seconds to wait between each message (default 1)",
    channels="Channels to send in, separated by commas (default: this channel)",
)
@app_commands.autocomplete(channels=channels_autocomplete)
async def repeat(
    interaction: discord.Interaction,
    times: app_commands.Range[int, 1, 1000],
    message: Optional[str] = None,
    image: Optional[discord.Attachment] = None,
    delay: app_commands.Range[float, MIN_DELAY, MAX_DELAY] = DEFAULT_DELAY,
    channels: Optional[str] = None,
):
    if await need_content(interaction, message, image):
        return
    await start_loops(
        interaction,
        summary=f"Repeating that {times} time(s) per channel, {delay:g}s apart",
        get_content=lambda: message,
        times=times,
        delay=delay,
        image=image,
        channels=channels,
    )


# infsend
@bot.tree.command(name="infsend", description="Send a message on a loop until stopped")
@app_commands.describe(
    message="The message to send",
    image="An image to attach to every message",
    delay="Seconds to wait between each message (default 1)",
    channels="Channels to send in, separated by commas (default: this channel)",
)
@app_commands.autocomplete(channels=channels_autocomplete)
async def infsend(
    interaction: discord.Interaction,
    message: Optional[str] = None,
    image: Optional[discord.Attachment] = None,
    delay: app_commands.Range[float, MIN_DELAY, MAX_DELAY] = DEFAULT_DELAY,
    channels: Optional[str] = None,
):
    if await need_content(interaction, message, image):
        return
    await start_loops(
        interaction,
        summary=f"Loop started, {delay:g}s between messages",
        get_content=lambda: message,
        times=0,
        delay=delay,
        image=image,
        channels=channels,
    )


# randsend
@bot.tree.command(name="randsend", description="Send randomly generated messages")
@app_commands.describe(
    mode="Same random message every time, or a new random message each time",
    length="How many characters long each message is (default 10)",
    times="How many messages to send (0 = keep going until /stop)",
    image="An image to attach to every message",
    delay="Seconds to wait between each message (default 1)",
    channels="Channels to send in, separated by commas (default: this channel)",
)
@app_commands.choices(
    mode=[
        app_commands.Choice(name="Same message every time", value="same"),
        app_commands.Choice(name="New random message each time", value="different"),
    ]
)
@app_commands.autocomplete(channels=channels_autocomplete)
async def randsend(
    interaction: discord.Interaction,
    mode: app_commands.Choice[str],
    length: app_commands.Range[int, 1, 2000] = 10,
    times: app_commands.Range[int, 0, 1000] = 0,
    image: Optional[discord.Attachment] = None,
    delay: app_commands.Range[float, MIN_DELAY, MAX_DELAY] = DEFAULT_DELAY,
    channels: Optional[str] = None,
):
    if mode.value == "same":
        fixed = make_random(length)  # generated once, shared by every channel
        get_content = lambda: fixed
    else:
        get_content = lambda: make_random(length)

    count = f"{times} time(s) per channel" if times else "until stopped"
    await start_loops(
        interaction,
        summary=(
            f"Sending {length}-character random messages ({mode.name.lower()}), "
            f"{count}, {delay:g}s apart"
        ),
        get_content=get_content,
        times=times,
        delay=delay,
        image=image,
        channels=channels,
    )


# stop
@bot.tree.command(name="stop", description="Stop the loop running in this channel")
@app_commands.describe(
    channels="Channels to stop loops in, separated by commas (default: this channel)"
)
@app_commands.autocomplete(channels=channels_autocomplete)
async def stop(interaction: discord.Interaction, channels: Optional[str] = None):
    if channels and channels.strip() and interaction.guild:
        found, unknown = parse_channels(interaction.guild, channels)
        stopped = []
        for ch in found:
            task = active_loops.pop(ch.id, None)
            if task:
                task.cancel()
                stopped.append(ch.mention)
        text = ("Stopped loops in " + ", ".join(stopped)) if stopped else (
            "No loops running in those channels right now"
        )
        if unknown:
            text += "\n- Couldn't find: " + ", ".join(unknown)
        await interaction.response.send_message(text)
        return

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


# ---------------------------------------------------------------- /setup
# Admin panel: choose whether everyone can use the bot, or only selected roles/users.
# Admins and the bot owner can always use the bot, so they can't lock themselves out.
HAS_SELECT_DEFAULTS = hasattr(discord, "SelectDefaultValue")  # discord.py 2.4+


def role_defaults(guild, ids):
    return [
        discord.SelectDefaultValue(id=r, type=discord.SelectDefaultValueType.role)
        for r in ids
        if guild.get_role(r)
    ][:25]


def user_defaults(ids):
    return [
        discord.SelectDefaultValue(id=u, type=discord.SelectDefaultValueType.user)
        for u in ids
    ][:25]


def mentions(roles=(), users=()):
    parts = [f"<@&{r}>" for r in roles] + [f"<@{u}>" for u in users]
    return ", ".join(parts)


def setup_text(guild_id):
    access = get_access(guild_id)
    if access["mode"] != "selected":
        return (
            "**Bot setup**\n"
            "**Who can use the bot:** everyone\n"
            "Use the button to limit it to chosen roles and users, or open "
            "**Command permissions** / **Bans** to manage things without typing commands."
        )
    roles = ", ".join(f"<@&{r}>" for r in access["roles"]) or "none"
    users = ", ".join(f"<@{u}>" for u in access["users"]) or "none"
    return (
        "**Bot setup**\n"
        "**Who can use the bot:** only selected people\n"
        f"Allowed roles: {roles}\n"
        f"Allowed users: {users}\n"
        "Admins and the bot owner can always use the bot."
    )[:2000]


def perms_page_text(guild_id, command):
    if command is None:
        return (
            "**Command permissions**\n"
            "Pick a command below, then choose which roles and users may use it.\n"
            "Admins and the bot owner can always use every command."
        )
    rule = get_rule(guild_id, command)
    if rule is None:
        if command in LOCKED_BY_DEFAULT:
            state = "admins and the bot owner only (default)"
        else:
            state = "everyone (no rule set)"
    else:
        who = mentions(rule["roles"], rule["users"])
        state = who if who else "admins and the bot owner only (locked)"
    return (
        f"**Command permissions: /{command}**\n"
        f"Currently usable by: {state}\n"
        "Choosing roles or users below replaces the list. "
        "**Open to everyone** removes the rule."
    )[:2000]


def bans_page_text(guild_id):
    here = bans["guilds"].get(str(guild_id), [])
    listing = ", ".join(f"<@{u}>" for u in here) if here else "nobody"
    return (
        "**Bans (this server)**\n"
        f"Banned from the bot here: {listing}\n"
        "Pick people in the first menu to ban them, or use the second to unban. "
        "Bans everywhere are bot-owner only (/ban everywhere:True)."
    )[:2000]


class SetupView(discord.ui.View):
    def __init__(self, guild, author_id):
        super().__init__(timeout=300)
        self.guild = guild
        self.author_id = author_id
        self.page = "main"  # "main", "perms" or "bans"
        self.command = None  # command being edited on the perms page
        self.notice = ""  # one-off message shown after an action
        self.build()

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.author_id and await can_manage(interaction):
            return True
        await interaction.response.send_message(
            "This panel isn't yours. Use /setup to open your own", ephemeral=True
        )
        return False

    def current_text(self):
        if self.page == "perms":
            text = perms_page_text(self.guild.id, self.command)
        elif self.page == "bans":
            text = bans_page_text(self.guild.id)
        else:
            text = setup_text(self.guild.id)
        if self.notice:
            text += f"\n\n{self.notice}"
        return text[:2000]

    # ------------------------------------------------------------ building
    def build(self):
        self.clear_items()
        if self.page == "perms":
            self.build_perms()
        elif self.page == "bans":
            self.build_bans()
        else:
            self.build_main()

    def add_button(self, text, callback, style=discord.ButtonStyle.secondary, disabled=False):
        button = discord.ui.Button(label=text, style=style, disabled=disabled)
        button.callback = callback
        self.add_item(button)
        return button

    def build_main(self):
        access = get_access(self.guild.id)
        limited = access["mode"] == "selected"

        self.add_button(
            "Limited to selected people" if limited else "Open to everyone",
            self.on_toggle,
            discord.ButtonStyle.primary if limited else discord.ButtonStyle.success,
        )
        self.add_button("Command permissions", self.go_perms)
        self.add_button("Bans", self.go_bans)

        role_kwargs = dict(
            placeholder="Roles allowed to use the bot", min_values=0, max_values=25
        )
        user_kwargs = dict(
            placeholder="Users allowed to use the bot", min_values=0, max_values=25
        )
        if HAS_SELECT_DEFAULTS:
            roles = role_defaults(self.guild, access["roles"])
            users = user_defaults(access["users"])
            if roles:
                role_kwargs["default_values"] = roles
            if users:
                user_kwargs["default_values"] = users
        else:
            role_kwargs["placeholder"] = "Allowed roles (replaces the current list)"
            user_kwargs["placeholder"] = "Allowed users (replaces the current list)"

        self.role_select = discord.ui.RoleSelect(**role_kwargs)
        self.role_select.callback = self.on_roles
        self.add_item(self.role_select)

        self.user_select = discord.ui.UserSelect(**user_kwargs)
        self.user_select.callback = self.on_users
        self.add_item(self.user_select)

    def build_perms(self):
        names = sorted(valid_commands())[:25]
        options = []
        for name in names:
            rule = get_rule(self.guild.id, name)
            if rule is not None:
                desc = "Custom rule"
            elif name in LOCKED_BY_DEFAULT:
                desc = "Admins only (default)"
            else:
                desc = "Everyone"
            options.append(
                discord.SelectOption(
                    label=f"/{name}",
                    value=name,
                    description=desc,
                    default=(name == self.command),
                )
            )
        self.command_select = discord.ui.Select(
            placeholder="Choose a command to edit", options=options
        )
        self.command_select.callback = self.on_command
        self.add_item(self.command_select)

        editing = self.command is not None
        rule = get_rule(self.guild.id, self.command) if editing else None
        role_kwargs = dict(
            placeholder="Roles allowed to use it",
            min_values=0,
            max_values=25,
            disabled=not editing,
        )
        user_kwargs = dict(
            placeholder="Users allowed to use it",
            min_values=0,
            max_values=25,
            disabled=not editing,
        )
        if HAS_SELECT_DEFAULTS and rule is not None:
            roles = role_defaults(self.guild, rule["roles"])
            users = user_defaults(rule["users"])
            if roles:
                role_kwargs["default_values"] = roles
            if users:
                user_kwargs["default_values"] = users

        self.cmd_role_select = discord.ui.RoleSelect(**role_kwargs)
        self.cmd_role_select.callback = self.on_cmd_roles
        self.add_item(self.cmd_role_select)

        self.cmd_user_select = discord.ui.UserSelect(**user_kwargs)
        self.cmd_user_select.callback = self.on_cmd_users
        self.add_item(self.cmd_user_select)

        self.add_button(
            "Admins only", self.on_cmd_lock, discord.ButtonStyle.danger, disabled=not editing
        )
        self.add_button(
            "Open to everyone",
            self.on_cmd_open,
            discord.ButtonStyle.success,
            disabled=not editing,
        )
        self.add_button("Back", self.go_main)

    def build_bans(self):
        self.ban_select = discord.ui.UserSelect(
            placeholder="Ban people from the bot here", min_values=1, max_values=5
        )
        self.ban_select.callback = self.on_ban
        self.add_item(self.ban_select)

        banned = list(bans["guilds"].get(str(self.guild.id), []))[:25]
        if banned:
            options = [
                discord.SelectOption(label=user_label(uid)[:100], value=str(uid))
                for uid in banned
            ]
            self.unban_select = discord.ui.Select(
                placeholder="Unban people",
                options=options,
                min_values=1,
                max_values=len(options),
            )
            self.unban_select.callback = self.on_unban
            self.add_item(self.unban_select)

        self.add_button("Back", self.go_main)

    # ------------------------------------------------------------- helpers
    async def refresh(self, interaction: discord.Interaction):
        save_data()
        self.build()
        text = self.current_text()
        self.notice = ""
        await interaction.response.edit_message(content=text, view=self)

    async def go_main(self, interaction: discord.Interaction):
        self.page = "main"
        await self.refresh(interaction)

    async def go_perms(self, interaction: discord.Interaction):
        self.page = "perms"
        await self.refresh(interaction)

    async def go_bans(self, interaction: discord.Interaction):
        self.page = "bans"
        await self.refresh(interaction)

    # ------------------------------------------------------------ main page
    async def on_toggle(self, interaction: discord.Interaction):
        access = get_access(self.guild.id)
        access["mode"] = "everyone" if access["mode"] == "selected" else "selected"
        await self.refresh(interaction)

    async def on_roles(self, interaction: discord.Interaction):
        get_access(self.guild.id)["roles"] = [r.id for r in self.role_select.values]
        await self.refresh(interaction)

    async def on_users(self, interaction: discord.Interaction):
        get_access(self.guild.id)["users"] = [u.id for u in self.user_select.values]
        await self.refresh(interaction)

    # ----------------------------------------------------- permissions page
    async def on_command(self, interaction: discord.Interaction):
        self.command = self.command_select.values[0]
        await self.refresh(interaction)

    async def on_cmd_roles(self, interaction: discord.Interaction):
        rule = get_rule(self.guild.id, self.command, create=True)
        rule["roles"] = [r.id for r in self.cmd_role_select.values]
        await self.refresh(interaction)

    async def on_cmd_users(self, interaction: discord.Interaction):
        rule = get_rule(self.guild.id, self.command, create=True)
        rule["users"] = [u.id for u in self.cmd_user_select.values]
        await self.refresh(interaction)

    async def on_cmd_lock(self, interaction: discord.Interaction):
        rule = get_rule(self.guild.id, self.command, create=True)
        rule["roles"].clear()
        rule["users"].clear()
        await self.refresh(interaction)

    async def on_cmd_open(self, interaction: discord.Interaction):
        permissions.get(str(self.guild.id), {}).pop(self.command, None)
        if self.command in LOCKED_BY_DEFAULT:
            self.notice = f"/{self.command} is back to its default (admins only)."
        await self.refresh(interaction)

    # ------------------------------------------------------------ bans page
    async def on_ban(self, interaction: discord.Interaction):
        owner = await bot.is_owner(interaction.user)
        banned = guild_ban_list(self.guild.id)
        added, skipped = [], []
        for user in self.ban_select.values:
            member = self.guild.get_member(user.id)
            if user.id == interaction.user.id or user.bot or await bot.is_owner(user):
                skipped.append(f"<@{user.id}>")
            elif not owner and member is not None and member.guild_permissions.administrator:
                skipped.append(f"<@{user.id}> (administrator)")
            elif user.id in banned:
                skipped.append(f"<@{user.id}> (already banned)")
            else:
                banned.append(user.id)
                added.append(f"<@{user.id}>")
        notes = []
        if added:
            notes.append("Banned: " + ", ".join(added))
        if skipped:
            notes.append("Couldn't ban: " + ", ".join(skipped))
        self.notice = "\n".join(notes)
        await self.refresh(interaction)

    async def on_unban(self, interaction: discord.Interaction):
        banned = guild_ban_list(self.guild.id)
        lifted = []
        for value in self.unban_select.values:
            uid = int(value)
            if uid in banned:
                banned.remove(uid)
                lifted.append(f"<@{uid}>")
        if lifted:
            self.notice = "Unbanned: " + ", ".join(lifted)
        await self.refresh(interaction)


@bot.tree.command(name="setup", description="Choose who can use the bot in this server")
@app_commands.guild_only()
@app_commands.default_permissions(administrator=True)
async def setup(interaction: discord.Interaction):
    if not await can_manage(interaction):
        await interaction.response.send_message(
            "Only the bot owner or server administrators can use /setup",
            ephemeral=True,
        )
        return
    view = SetupView(interaction.guild, interaction.user.id)
    await interaction.response.send_message(
        view.current_text(), view=view, ephemeral=True
    )


# ------------------------------------------------------- ban commands
# Blocks someone from using this bot (not a Discord ban). Admins and the bot owner
# can always use these; give a role access with /perms allow command:ban role:...
def guild_ban_list(guild_id):
    return bans["guilds"].setdefault(str(guild_id), [])


def user_label(user_id):
    user = bot.get_user(user_id)
    return f"{user} ({user_id})" if user else str(user_id)


@bot.tree.command(name="ban", description="Block someone from using the bot")
@app_commands.describe(
    user="Who to block",
    everywhere="Bot owner only: block them in every server",
)
@app_commands.guild_only()
async def ban(
    interaction: discord.Interaction, user: discord.User, everywhere: bool = False
):
    owner = await bot.is_owner(interaction.user)
    if everywhere and not owner:
        await interaction.response.send_message(
            "Only the bot owner can ban someone everywhere", ephemeral=True
        )
        return
    if user.id == interaction.user.id:
        await interaction.response.send_message("You can't ban yourself", ephemeral=True)
        return
    if user.bot or await bot.is_owner(user):
        await interaction.response.send_message(
            "You can't ban that user", ephemeral=True
        )
        return
    member = interaction.guild.get_member(user.id)
    if not owner and member is not None and member.guild_permissions.administrator:
        await interaction.response.send_message(
            "You can't ban a server administrator", ephemeral=True
        )
        return

    banned = bans["global"] if everywhere else guild_ban_list(interaction.guild_id)
    if user.id in banned:
        await interaction.response.send_message(
            f"{user.mention} is already banned", ephemeral=True
        )
        return

    banned.append(user.id)
    save_data()
    where = "everywhere" if everywhere else "in this server"
    await interaction.response.send_message(
        f"{user.mention} can no longer use the bot {where}", ephemeral=True
    )


async def unban_autocomplete(
    interaction: discord.Interaction, current: str
) -> List[app_commands.Choice[str]]:
    # Autocomplete skips the permission check, so don't leak the list to everyone
    if not await can_use(interaction, "unban"):
        return []
    ids = list(bans["guilds"].get(str(interaction.guild_id), []))
    if await bot.is_owner(interaction.user):
        ids += [i for i in bans["global"] if i not in ids]
    choices = []
    for uid in ids:
        text = user_label(uid)
        if current.lower() in text.lower():
            choices.append(app_commands.Choice(name=text[:100], value=str(uid)))
    return choices[:25]


@bot.tree.command(name="unban", description="Let someone use the bot again")
@app_commands.describe(
    user="Pick from the list, or paste their user ID",
    everywhere="Bot owner only: lift a ban that applies in every server",
)
@app_commands.autocomplete(user=unban_autocomplete)
@app_commands.guild_only()
async def unban(
    interaction: discord.Interaction, user: str, everywhere: bool = False
):
    owner = await bot.is_owner(interaction.user)
    if everywhere and not owner:
        await interaction.response.send_message(
            "Only the bot owner can lift an everywhere ban", ephemeral=True
        )
        return
    match = re.search(r"\d{15,20}", user)
    if not match:
        await interaction.response.send_message(
            "Pick someone from the list, or paste their user ID", ephemeral=True
        )
        return

    uid = int(match.group())
    banned = bans["global"] if everywhere else guild_ban_list(interaction.guild_id)
    if uid not in banned:
        note = ""
        if uid in bans["global"] and not owner:
            note = " They're banned everywhere, and only the bot owner can lift that."
        await interaction.response.send_message(
            f"{user_label(uid)} isn't banned here.{note}", ephemeral=True
        )
        return

    banned.remove(uid)
    save_data()
    await interaction.response.send_message(
        f"<@{uid}> can use the bot again", ephemeral=True
    )


@bot.tree.command(name="banlist", description="Show who is banned from the bot here")
@app_commands.guild_only()
async def banlist(interaction: discord.Interaction):
    owner = await bot.is_owner(interaction.user)
    here = bans["guilds"].get(str(interaction.guild_id), [])
    lines = [
        "Banned in this server: "
        + (", ".join(f"<@{u}>" for u in here) if here else "nobody")
    ]
    everywhere = bans["global"]
    if owner:
        lines.append(
            "Banned everywhere: "
            + (", ".join(f"<@{u}>" for u in everywhere) if everywhere else "nobody")
        )
    elif everywhere:
        lines.append(
            f"Banned everywhere: {len(everywhere)} (only the bot owner can see or change these)"
        )
    await interaction.response.send_message("\n".join(lines)[:2000], ephemeral=True)


# ------------------------------------------------------ /perms commands
class PermsGroup(app_commands.Group):
    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if await can_manage(interaction):
            return True
        if interaction.type is discord.InteractionType.application_command:
            await interaction.response.send_message(
                "Only the bot owner or server administrators can change permissions.",
                ephemeral=True,
            )
        return False


perms = PermsGroup(
    name="perms",
    description="Control who can use which commands",
    guild_only=True,
    default_permissions=discord.Permissions(administrator=True),
)
bot.tree.add_command(perms)


def valid_commands():
    return {c.name for c in bot.tree.get_commands() if c.name not in ("perms", "setup")}


async def command_autocomplete(
    interaction: discord.Interaction, current: str
) -> List[app_commands.Choice[str]]:
    names = sorted(n for n in valid_commands() if current.lower() in n)
    return [app_commands.Choice(name=n, value=n) for n in names][:25]


async def unknown_command(interaction, command):
    """Reply with an error and return True if the command name isn't valid."""
    if command in valid_commands():
        return False
    await interaction.response.send_message(
        f"/{command} isn't a command I know", ephemeral=True
    )
    return True


def describe_who(role, user):
    parts = []
    if role is not None:
        parts.append(role.mention)
    if user is not None:
        parts.append(user.mention)
    return " and ".join(parts)


@perms.command(name="allow", description="Let a role or user use a command")
@app_commands.describe(
    command="The command to restrict",
    role="A role that should be allowed to use it",
    user="A user that should be allowed to use it",
)
@app_commands.autocomplete(command=command_autocomplete)
async def perms_allow(
    interaction: discord.Interaction,
    command: str,
    role: Optional[discord.Role] = None,
    user: Optional[discord.Member] = None,
):
    if await unknown_command(interaction, command):
        return
    if role is None and user is None:
        await interaction.response.send_message(
            "Pick a role, a user, or both", ephemeral=True
        )
        return

    rule = get_rule(interaction.guild_id, command, create=True)
    if role is not None and role.id not in rule["roles"]:
        rule["roles"].append(role.id)
    if user is not None and user.id not in rule["users"]:
        rule["users"].append(user.id)
    save_data()

    await interaction.response.send_message(
        f"/{command} can now be used by {describe_who(role, user)}. "
        "Admins and the bot owner can always use it too.",
        ephemeral=True,
    )


@perms.command(name="remove", description="Take a role or user off a command's allowed list")
@app_commands.describe(
    command="The command to change",
    role="A role to remove",
    user="A user to remove",
)
@app_commands.autocomplete(command=command_autocomplete)
async def perms_remove(
    interaction: discord.Interaction,
    command: str,
    role: Optional[discord.Role] = None,
    user: Optional[discord.Member] = None,
):
    if await unknown_command(interaction, command):
        return
    rule = get_rule(interaction.guild_id, command)
    if rule is None:
        who = (
            "admins and the bot owner can use it"
            if command in LOCKED_BY_DEFAULT
            else "everyone can use it"
        )
        await interaction.response.send_message(
            f"/{command} has no custom rule, so {who}", ephemeral=True
        )
        return
    if role is None and user is None:
        await interaction.response.send_message(
            "Pick a role, a user, or both", ephemeral=True
        )
        return

    if role is not None and role.id in rule["roles"]:
        rule["roles"].remove(role.id)
    if user is not None and user.id in rule["users"]:
        rule["users"].remove(user.id)
    save_data()

    await interaction.response.send_message(
        f"Removed {describe_who(role, user)} from /{command}. "
        "Use /perms reset to clear the rule.",
        ephemeral=True,
    )


@perms.command(name="lock", description="Make a command admins and bot owner only")
@app_commands.describe(command="The command to lock")
@app_commands.autocomplete(command=command_autocomplete)
async def perms_lock(interaction: discord.Interaction, command: str):
    if await unknown_command(interaction, command):
        return
    rule = get_rule(interaction.guild_id, command, create=True)
    rule["roles"].clear()
    rule["users"].clear()
    save_data()
    await interaction.response.send_message(
        f"/{command} is now limited to admins and the bot owner", ephemeral=True
    )


@perms.command(name="reset", description="Let everyone use a command again")
@app_commands.describe(command="The command to open up")
@app_commands.autocomplete(command=command_autocomplete)
async def perms_reset(interaction: discord.Interaction, command: str):
    if await unknown_command(interaction, command):
        return
    guild_rules = permissions.get(str(interaction.guild_id), {})
    default_locked = command in LOCKED_BY_DEFAULT
    if guild_rules.pop(command, None) is None:
        await interaction.response.send_message(
            f"/{command} has no custom rule", ephemeral=True
        )
        return
    save_data()
    await interaction.response.send_message(
        f"/{command} is back to its default (admins and the bot owner only)"
        if default_locked
        else f"/{command} can be used by everyone again",
        ephemeral=True,
    )


@perms.command(name="list", description="Show which commands are restricted here")
async def perms_list(interaction: discord.Interaction):
    rules = permissions.get(str(interaction.guild_id), {})
    defaults = ", ".join(f"/{c}" for c in sorted(LOCKED_BY_DEFAULT))
    if not rules:
        await interaction.response.send_message(
            "No custom rules. Everyone can use every command except "
            f"{defaults}, which are admins and bot owner only by default",
            ephemeral=True,
        )
        return

    lines = []
    for command, rule in sorted(rules.items()):
        who = [f"<@&{r}>" for r in rule.get("roles", [])]
        who += [f"<@{u}>" for u in rule.get("users", [])]
        lines.append(
            f"/{command}: " + (", ".join(who) if who else "admins and bot owner only")
        )
    text = "Restricted commands (admins and the bot owner can always use them):\n"
    text += "\n".join(lines)
    text += f"\nDefault admins-only commands without a rule: {defaults}"
    await interaction.response.send_message(text[:2000], ephemeral=True)


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


if __name__ == "__main__":
    if not token:
        raise SystemExit(
            "No bot token found. Put token=YOUR_TOKEN in a .env file, "
            "or enter it in the toggle window."
        )
    bot.run(token)