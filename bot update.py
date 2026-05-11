import discord
from discord.ext import commands
import os
import subprocess
from dotenv import load_dotenv

load_dotenv()

# Install ffmpeg if not available
try:
    subprocess.run(["ffmpeg", "-version"], capture_output=True, check=True)
except Exception:
    os.system("apt-get install -y ffmpeg")

intents = discord.Intents.default()
intents.message_content = True
intents.voice_states = True

bot = commands.Bot(command_prefix="!", intents=intents)

@bot.event
async def on_ready():
    await bot.load_extension("cogs.music")
    try:
        synced = await bot.tree.sync()
        print(f"✅ Logged in as {bot.user} (ID: {bot.user.id})")
        print(f"🔧 Synced {len(synced)} slash command(s).")
    except Exception as e:
        print(f"❌ Failed to sync commands: {e}")

if __name__ == "__main__":
    bot.run(os.getenv("DISCORD_TOKEN"))