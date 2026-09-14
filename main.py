from dotenv import load_dotenv
import os
import asyncio

import discord
from discord.ext import commands

load_dotenv()
token = os.getenv("token")

intents = discord.Intents.default()
intents.message_content = True

bot = commands.Bot(
    command_prefix="a!",
    intents=intents,
    help_command=None
)

active_loops = {}

# startup message
@bot.event
async def on_ready():
    print(f"Logged in as {bot.user}")

# logger
@bot.event
async def on_message(message):
    if message.author.bot:
        return

    print(f"[{message.guild}] {message.author}: {message.content}")

    await bot.process_commands(message)

#help command
@bot.command()
async def help(ctx):
    await ctx.send(
        "Available commands as of now:\n"
        "a!help\n"
        "a!version\n"
        "a!send (Your message)\n"
        "a!repeat (Times, Message)\n"
        "a!infsend (Your message)\n"
        "a!stop (Quit current ping loop)\n"
        "a!stopall (Quit all ping loops)\n"
    )

#test
@bot.command()
async def test(ctx):
    await ctx.send("I am alive and well!")

#joke
@bot.command()
async def fuckyou(ctx):
    await ctx.send("Yeah im pissed now")

#version
@bot.command()
async def version(ctx):
    await ctx.send(
        "Autopinger (Name not final) Alpha 0.6.4"
    )

#send a singular message
@bot.command()
async def send(ctx, *, message):
    await ctx.send(message)

#not inf loop
async def _repeat_loop(ctx, times, message):
    try:
        for _ in range(times):
            await ctx.send(message)
            await asyncio.sleep(1)
    except asyncio.CancelledError:
        pass
    finally:
        if active_loops.get(ctx.channel.id) is asyncio.current_task():
            del active_loops[ctx.channel.id]

#insend loop
async def _infsend_loop(ctx, message):
    try:
        while True:
            await ctx.send(message)
            await asyncio.sleep(1)
    except asyncio.CancelledError:
        pass
    finally:
        if active_loops.get(ctx.channel.id) is asyncio.current_task():
            del active_loops[ctx.channel.id]

# multiple loops in same channel prevention
@bot.command()
async def repeat(ctx, times: int, *, message):
    if ctx.channel.id in active_loops:
        await ctx.send("A loop is already running in this channel. Use a!stop first.")
        return
    task = asyncio.create_task(_repeat_loop(ctx, times, message))
    active_loops[ctx.channel.id] = task

# infsend
@bot.command()
async def infsend(ctx, *, message):
    if ctx.channel.id in active_loops:
        await ctx.send("A loop is already running in this channel. Use a!stop first.")
        return
    task = asyncio.create_task(_infsend_loop(ctx, message))
    active_loops[ctx.channel.id] = task

# stop
@bot.command()
async def stop(ctx):
    task = active_loops.pop(ctx.channel.id, None)
    if task:
        task.cancel()
        await ctx.send("Ping loop stopped.")
    else:
        await ctx.send("No loops running in this channel right now") 

#stop all
@bot.command()
async def stopall(ctx):
    if not active_loops:
        await ctx.send("No loops running in any channel right now")
        return

    count = len(active_loops)
    for task in list(active_loops.values()):
        task.cancel()
    active_loops.clear()

    await ctx.send(f"Stopped {count} running loop(s)")

bot.run(token)