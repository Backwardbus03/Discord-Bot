import os
import random
import asyncio
import logging
import time as pytime
from datetime import datetime, timedelta, time as dt_time
from zoneinfo import ZoneInfo

import discord
from discord.ext import commands, tasks
from dotenv import load_dotenv
from supabase import create_client, Client

from test import fetch_upcoming_contests, fetch_contests
from flask import Flask
from threading import Thread

# Setup logging to monitor Discord API rate limit events
logging.basicConfig(level=logging.INFO)
discord_http_logger = logging.getLogger("discord.http")
discord_http_logger.setLevel(logging.INFO)

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

# Rate limiting for UI interactions (dropdown selects)
_USER_INTERACTION_COOLDOWNS = {}
INTERACTION_COOLDOWN_SECONDS = 3.0


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

            label = (c.get("event") or "").strip()
            if not label:
                label = f"{c.get('resource', 'Unknown')} Contest"
            if len(label) > 100:
                label = label[:97] + "..."

            description = f"{c.get('resource', '')} | {display_time}".strip()
            if len(description) > 100:
                description = description[:97] + "..."

            val = str(c.get("id", ""))[:100]
            if not val:
                continue

            options.append(discord.SelectOption(
                label=label,
                description=description,
                value=val
            ))
            self.contests_map[val] = c

        if not options:
            options.append(discord.SelectOption(
                label="No contests available",
                value="none"
            ))

        super().__init__(placeholder="Choose a contest...", min_values=1, max_values=1, options=options)

    async def callback(self, interaction: discord.Interaction):
        user_id = interaction.user.id
        now = pytime.time()
        last_time = _USER_INTERACTION_COOLDOWNS.get(user_id, 0)
        if now - last_time < INTERACTION_COOLDOWN_SECONDS:
            remaining = INTERACTION_COOLDOWN_SECONDS - (now - last_time)
            await interaction.response.send_message(
                f"⏳ You are selecting too quickly! Please wait {remaining:.1f}s before choosing again.",
                ephemeral=True
            )
            return
        _USER_INTERACTION_COOLDOWNS[user_id] = now

        selected_id = self.values[0]
        if selected_id == "none" or selected_id not in self.contests_map:
            await interaction.response.send_message("No valid contest selected.", ephemeral=True)
            return

        c = self.contests_map[selected_id]

        try:
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
        except Exception as e:
            print(f"Error saving reminder to Supabase: {e}")
            await interaction.response.send_message(
                "⚠️ Database is temporarily unavailable. Please try again in a few minutes.",
                ephemeral=True
            )
            return

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
            if not channel:
                continue
            for c in contests:
                try:
                    start_utc = datetime.fromisoformat(c["start"]).replace(tzinfo=ZoneInfo("UTC"))
                except Exception:
                    start_utc = datetime.strptime(c["start"], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=ZoneInfo("UTC"))

                if target_time_utc == start_utc:
                    try:
                        await channel.send(f"🔔 **Reminder:** {c['href']} has already started!")
                        # Pacing outbound messages to avoid burst channel rate limits
                        await asyncio.sleep(0.5)
                    except discord.HTTPException as e:
                        if e.status == 429:
                            print(f"[Rate Limit] 429 encountered in send_link: {e}")
                            await asyncio.sleep(2.0)
                        else:
                            print(f"Failed to send link message: {e}")
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
                            # Pacing outbound DMs to respect Discord's direct message rate limits
                            await asyncio.sleep(0.5)
                        except discord.Forbidden:
                            print(f"Could not send DM to {user_id}. They might have DMs disabled.")
                        except discord.HTTPException as e:
                            if e.status == 429:
                                print(f"[Rate Limit] 429 encountered sending DM to {user_id}: {e}")
                                await asyncio.sleep(2.0)
                            else:
                                print(f"Failed to send DM to {user_id}: {e}")
                        except Exception as e:
                            print(f"Failed to send DM to {user_id}: {e}")

                    supabase.table("reminders").delete().eq("id", r_id).execute()
            except Exception as e:
                print(f"Error processing reminder {r_id}: {e}")
    except Exception as e:
        err_str = str(e)
        if "521" in err_str or "Web server is down" in err_str:
            print("Error in check_reminders task: Supabase server is unreachable (HTTP 521: Web server is down / paused / maintenance).")
        else:
            print(f"Error in check_reminders task: {err_str[:250]}")


@tasks.loop(time=dt_time(hour=2, minute=30, tzinfo=ZoneInfo("UTC")))
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
                event_name = (c.get("event") or "").strip() or f"{c.get('resource', 'Unknown')} Contest"
                if len(event_name) > 256:
                    event_name = event_name[:253] + "..."
                href_val = c.get("href", "")
                embed.add_field(name=event_name, value=f"**{href_val}** at {display_time}", inline=False)

            view = ContestView(contests)
            await channel.send(embed=embed, view=view)


@bot.command(name='remind')
@commands.cooldown(rate=1, per=10.0, type=commands.BucketType.user)
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
@commands.cooldown(rate=1, per=5.0, type=commands.BucketType.channel)
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
@commands.cooldown(rate=1, per=3.0, type=commands.BucketType.user)
async def roll_die(ctx, die_sides: int = 6):
    if die_sides <= 0:
        await ctx.send("Please provide a valid number of sides greater than 0.")
        return
    response = random.randint(1, die_sides)
    await ctx.send(f"🎲 You rolled a **{response}**!")


@roll_die.error
async def roll_error(ctx, error):
    if isinstance(error, commands.CommandOnCooldown):
        await ctx.send(f"⏳ **Cooldown Active:** Please wait {error.retry_after:.1f}s before rolling again.")
    elif isinstance(error, commands.BadArgument):
        await ctx.send("Please provide a valid integer for the number of sides (e.g., `!roll 6`).")


@bot.event
async def on_command_error(ctx, error):
    # Check if command has a local error handler that already dealt with the error
    if hasattr(ctx.command, 'on_error'):
        return

    if isinstance(error, commands.CommandOnCooldown):
        await ctx.send(
            f"⏳ **Cooldown Active:** Please wait {error.retry_after:.1f}s before using `{ctx.prefix}{ctx.invoked_with}` again."
        )
    elif isinstance(error, commands.CommandNotFound):
        pass  # Silently ignore invalid commands
    elif isinstance(error, commands.BadArgument):
        await ctx.send(f"⚠️ Invalid arguments provided. Please check `{ctx.prefix}help` or `{ctx.prefix}commands`.")
    else:
        print(f"Unhandled error in command {ctx.command}: {error}")


if __name__ == '__main__':
    keep_alive()
    bot.run(TOKEN)
