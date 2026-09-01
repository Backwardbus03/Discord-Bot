import os
import random
import asyncio
from datetime import datetime, timedelta, time
from zoneinfo import ZoneInfo

import discord
from discord.ext import commands, tasks
from dotenv import load_dotenv
from supabase import create_client, Client

from test import fetch_upcoming_contests, fetch_contests
from flask import Flask
from threading import Thread

app = Flask('')
load_dotenv()


@app.route('/')
def home():
    return "Bot is alive"


def run_web():
    port = int(os.environ.get("PORT", 8080))
    app.run(host='0.0.0.0', port=port, use_reloader=False, debug=False)


def keep_alive():
    t = Thread(target=run_web)
    t.start()

TOKEN = os.environ.get("DISCORD_TOKEN")

if not TOKEN:
    raise RuntimeError("DISCORD_TOKEN environment variable is not set")

SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")

if not SUPABASE_URL:
    raise RuntimeError("SUPABASE_URL environment variable is not set")

if not SUPABASE_KEY:
    raise RuntimeError("SUPABASE_KEY environment variable is not set")

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

intents = discord.Intents.default()
intents.message_content = True

bot = commands.Bot(command_prefix='!', intents=intents)
bot.remove_command('help')  # Remove default help command to use custom!


class ContestSelect(discord.ui.Select):
    def __init__(self, contests):
        options = []
        self.contests_map = {}
        for c in contests[:25]:
            # parse start time from UTC to IST for display
            try:
                start_utc = datetime.fromisoformat(c["start"]).replace(tzinfo=ZoneInfo("UTC"))
            except Exception:
                start_utc = datetime.strptime(c["start"], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=ZoneInfo("UTC"))

            start_ist = start_utc.astimezone(ZoneInfo("Asia/Kolkata"))
            display_time = start_ist.strftime("%d %b, %I:%M %p")

            label = c["event"]
            if len(label) > 100:
                label = label[:97] + "..."

            options.append(discord.SelectOption(
                label=label,
                description=f"{c['resource']} | {display_time}",
                value=str(c["id"])
            ))
            self.contests_map[str(c["id"])] = c

        super().__init__(placeholder="Choose a contest...", min_values=1, max_values=1, options=options)

    async def callback(self, interaction: discord.Interaction):
        selected_id = self.values[0]
        c = self.contests_map[selected_id]

        # Check if reminder already exists
        existing = supabase.table("reminders").select("*").eq("user_id", interaction.user.id).eq("contest_name",
                                                                                                 c["event"]).execute()
        if existing.data:
            await interaction.response.send_message(
                f"You already have a reminder set for **{c['event']}**!",
                ephemeral=True
            )
            return

        supabase.table("reminders").insert({
            "user_id": interaction.user.id,
            "contest_name": c["event"],
            "start_time": c["start"],
            "href": c["href"],
        }).execute()

        try:
            start_utc = datetime.fromisoformat(c["start"]).replace(tzinfo=ZoneInfo("UTC"))
        except Exception:
            start_utc = datetime.strptime(c["start"], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=ZoneInfo("UTC"))

        start_ist = start_utc.astimezone(ZoneInfo("Asia/Kolkata"))

        await interaction.response.send_message(
            f"Reminder set! I will DM you 30 minutes before **{c['event']}** (Starts at {start_ist.strftime('%I:%M %p IST')}).",
            ephemeral=True
        )


class ContestView(discord.ui.View):
    def __init__(self, contests):
        super().__init__()
        self.add_item(ContestSelect(contests))


@bot.event
async def on_ready():
    print(f'{bot.user} has connected to Discord!')
    await bot.change_presence(activity=discord.Activity(type=discord.ActivityType.listening, name="!commands"))

    if not check_reminders.is_running():
        check_reminders.start()
    if not daily_notify.is_running():
        daily_notify.start()
    if not send_link.is_running():
        send_link.start()


@tasks.loop(minutes=1)
async def send_link():
    try:
        now_utc = datetime.now(ZoneInfo("UTC"))
        target_time_utc = now_utc + timedelta(minutes=5)
        loop = asyncio.get_running_loop()
        contests = await loop.run_in_executor(None, fetch_contests)

        if not contests: return

        for guild in bot.guilds:
            channel = discord.utils.get(guild.text_channels, name='notify')
            for c in contests:
                try:
                    start_utc = datetime.fromisoformat(c["start"]).replace(tzinfo=ZoneInfo("UTC"))
                except Exception:
                    start_utc = datetime.strptime(c["start"], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=ZoneInfo("UTC"))

                if target_time_utc == start_utc:
                    await channel.send(f"🔔 **Reminder:** {c['href']} has already started!")
    except Exception as ex:
        print(ex)


@tasks.loop(minutes=1)
async def check_reminders():
    try:
        now_utc = datetime.now(ZoneInfo("UTC"))
        target_time_utc = now_utc + timedelta(minutes=30)

        response = supabase.table("reminders").select("*").execute()
        reminders = response.data

        for r in reminders:
            r_id = r["id"]
            user_id = r["user_id"]
            contest_name = r["contest_name"]
            contest_url = r["href"]
            start_time_str = r["start_time"]
            try:
                try:
                    start_time = datetime.fromisoformat(start_time_str).replace(tzinfo=ZoneInfo("UTC"))
                except Exception:
                    start_time = datetime.strptime(start_time_str, "%Y-%m-%dT%H:%M:%S").replace(tzinfo=ZoneInfo("UTC"))

                if start_time <= target_time_utc:
                    # Check if contest is way in the past (stale reminder)
                    if start_time < now_utc - timedelta(hours=2):
                        # Bot was offline for a long time, skip sending this stale DM
                        supabase.table("reminders").delete().eq("id", r_id).execute()
                        continue

                    is_past = start_time <= now_utc
                    user = bot.get_user(user_id) or await bot.fetch_user(user_id)
                    if user:
                        start_time_ist = start_time.astimezone(ZoneInfo("Asia/Kolkata"))
                        try:
                            if is_past:
                                await user.send(
                                    f"🔔 **Reminder:** {contest_url} has already started! (at {start_time_ist.strftime('%I:%M %p IST')})")
                            else:
                                await user.send(
                                    f"🔔 **Reminder:** {contest_url} is starting in less than 30 minutes! (at {start_time_ist.strftime('%I:%M %p IST')})")
                        except discord.Forbidden:
                            print(f"Could not send DM to {user_id}. They might have DMs disabled.")
                        except Exception as e:
                            print(f"Failed to send DM to {user_id}: {e}")

                    supabase.table("reminders").delete().eq("id", r_id).execute()
            except Exception as e:
                print(f"Error processing reminder {r_id}: {e}")
    except Exception as e:
        print(f"Error in check_reminders task: {e}")


@tasks.loop(time=time(hour=2, minute=30, tzinfo=ZoneInfo("UTC")))
async def daily_notify():
    loop = asyncio.get_running_loop()
    contests = await loop.run_in_executor(None, fetch_contests)

    if not contests:
        return

    for guild in bot.guilds:
        channel = discord.utils.get(guild.text_channels, name='notify')
        if channel:
            await channel.send("@everyone")
            embed = discord.Embed(
                title="🏆 Today's Contests",
                description="Here are the contests scheduled for today. Select a contest from the dropdown below to set a reminder!",
                color=discord.Color.green()
            )
            for c in contests[:10]:
                try:
                    start_utc = datetime.fromisoformat(c["start"]).replace(tzinfo=ZoneInfo("UTC"))
                except Exception:
                    start_utc = datetime.strptime(c["start"], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=ZoneInfo("UTC"))
                start_ist = start_utc.astimezone(ZoneInfo("Asia/Kolkata"))
                display_time = start_ist.strftime("%I:%M %p IST")
                embed.add_field(name=c["event"], value=f"**{c['href']}** at {display_time}", inline=False)

            view = ContestView(contests)
            await channel.send(embed=embed, view=view)


@bot.command(name='remind')
async def remind_command(ctx):
    msg = await ctx.send("Fetching upcoming contests...")
    loop = asyncio.get_running_loop()
    contests = await loop.run_in_executor(None, fetch_upcoming_contests)

    if not contests:
        await msg.edit(content="Failed to fetch contests or no upcoming contests found.")
        return

    view = ContestView(contests)
    await msg.edit(content="Select a contest to be reminded about:", view=view)


@bot.command(name='commands', aliases=['help'])
async def commands_command(ctx):
    embed = discord.Embed(
        title="🤖 Bot Commands",
        description="Here is the list of commands you can use:",
        color=discord.Color.blue()
    )
    embed.add_field(name="`!remind`", value="Shows upcoming contests and lets you set a 30-minute reminder.",
                    inline=False)
    embed.add_field(name="`!commands` / `!help`", value="Shows this help message.", inline=False)
    embed.add_field(name="`!roll [sides]`", value="Rolls a die with the specified number of sides (defaults to 6).",
                    inline=False)

    await ctx.send(embed=embed)


@bot.command(name='roll')
async def roll_die(ctx, die_sides: int = 6):
    if die_sides <= 0:
        await ctx.send("Please provide a valid number of sides greater than 0.")
        return
    response = random.randint(1, die_sides)
    await ctx.send(f"🎲 You rolled a **{response}**!")


@roll_die.error
async def roll_error(ctx, error):
    if isinstance(error, commands.BadArgument):
        await ctx.send("Please provide a valid integer for the number of sides (e.g., `!roll 6`).")


keep_alive()
bot.run(TOKEN)
