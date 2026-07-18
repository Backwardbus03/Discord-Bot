import os
import random
import sqlite3
import asyncio
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import discord
from discord.ext import commands, tasks
from dotenv import load_dotenv

from test import fetch_upcoming_contests

load_dotenv()
TOKEN = os.getenv('DISCORD_TOKEN')
intents = discord.Intents.default()
intents.message_content = True

bot = commands.Bot(command_prefix='!', intents=intents)

# Database setup
def setup_db():
    conn = sqlite3.connect('reminders.db')
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS reminders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            contest_name TEXT,
            start_time TEXT
        )
    ''')
    conn.commit()
    conn.close()

setup_db()

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
        
        conn = sqlite3.connect('reminders.db')
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO reminders (user_id, contest_name, start_time) VALUES (?, ?, ?)",
            (interaction.user.id, c["event"], c["start"])
        )
        conn.commit()
        conn.close()
        
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
    if not check_reminders.is_running():
        check_reminders.start()

@tasks.loop(minutes=1)
async def check_reminders():
    conn = sqlite3.connect('reminders.db')
    cursor = conn.cursor()
    
    now_utc = datetime.now(ZoneInfo("UTC"))
    target_time_utc = now_utc + timedelta(minutes=30)
    
    cursor.execute("SELECT id, user_id, contest_name, start_time FROM reminders")
    reminders = cursor.fetchall()
    
    for r_id, user_id, contest_name, start_time_str in reminders:
        try:
            try:
                start_time = datetime.fromisoformat(start_time_str).replace(tzinfo=ZoneInfo("UTC"))
            except Exception:
                start_time = datetime.strptime(start_time_str, "%Y-%m-%dT%H:%M:%S").replace(tzinfo=ZoneInfo("UTC"))
                
            if start_time <= target_time_utc:
                user = bot.get_user(user_id) or await bot.fetch_user(user_id)
                if user:
                    start_time_ist = start_time.astimezone(ZoneInfo("Asia/Kolkata"))
                    await user.send(f"🔔 **Reminder:** {contest_name} is starting in less than 30 minutes! (at {start_time_ist.strftime('%I:%M %p IST')})")
                
                cursor.execute("DELETE FROM reminders WHERE id = ?", (r_id,))
                conn.commit()
        except Exception as e:
            print(f"Error processing reminder {r_id}: {e}")
            
    conn.close()

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

@bot.command(name='roll')
async def roll_die(ctx, die_sides):
    response = random.randint(1, int(die_sides) + 1)
    await ctx.send(response)

bot.run(TOKEN)