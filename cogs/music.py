import discord
from discord import app_commands
from discord.ext import commands
import yt_dlp
import spotipy
from spotipy.oauth2 import SpotifyClientCredentials
import asyncio
import random
import os
import json
import time
from collections import deque
from datetime import timedelta

# ── Spotify client ────────────────────────────────────────────────────────────
sp = spotipy.Spotify(auth_manager=SpotifyClientCredentials(
    client_id=os.getenv("SPOTIFY_CLIENT_ID"),
    client_secret=os.getenv("SPOTIFY_CLIENT_SECRET"),
))

# ── yt-dlp options ────────────────────────────────────────────────────────────
YTDL_OPTIONS = {
    "format": "bestaudio/best",
    "noplaylist": True,
    "quiet": True,
    "default_search": "ytsearch",
    "source_address": "0.0.0.0",
}

ytdl = yt_dlp.YoutubeDL(YTDL_OPTIONS)

PLAYLISTS_FILE = "playlists.json"
STATS_FILE = "stats.json"


# ── persistence helpers ───────────────────────────────────────────────────────

def load_json(path):
    if os.path.exists(path):
        with open(path) as f:
            return json.load(f)
    return {}

def save_json(path, data):
    with open(path, "w") as f:
        json.dump(data, f, indent=2)


# ── stats ─────────────────────────────────────────────────────────────────────

stats = load_json(STATS_FILE)

def record_play(title):
    stats["total_played"] = stats.get("total_played", 0) + 1
    leaderboard = stats.get("leaderboard", {})
    leaderboard[title] = leaderboard.get(title, 0) + 1
    stats["leaderboard"] = leaderboard
    save_json(STATS_FILE, stats)


# ── helpers ───────────────────────────────────────────────────────────────────

def is_spotify_url(url):
    return "open.spotify.com" in url

def spotify_to_search_query(url):
    queries = []
    if "/track/" in url:
        track_id = url.split("/track/")[1].split("?")[0]
        track = sp.track(track_id)
        queries.append(f"{track['artists'][0]['name']} - {track['name']}")
    elif "/playlist/" in url:
        playlist_id = url.split("/playlist/")[1].split("?")[0]
        results = sp.playlist_tracks(playlist_id)
        for item in results["items"]:
            t = item["track"]
            if t:
                queries.append(f"{t['artists'][0]['name']} - {t['name']}")
    elif "/album/" in url:
        album_id = url.split("/album/")[1].split("?")[0]
        results = sp.album_tracks(album_id)
        for t in results["items"]:
            queries.append(f"{t['artists'][0]['name']} - {t['name']}")
    return queries

async def fetch_source(query, loop):
    data = await loop.run_in_executor(None, lambda: ytdl.extract_info(query, download=False))
    if "entries" in data:
        data = data["entries"][0]
    return {
        "url": data["url"],
        "title": data.get("title", query),
        "webpage_url": data.get("webpage_url", ""),
        "thumbnail": data.get("thumbnail", ""),
        "duration": data.get("duration", 0),
        "uploader": data.get("uploader", "Unknown"),
    }

def format_duration(seconds):
    return str(timedelta(seconds=int(seconds))) if seconds else "0:00"

def progress_bar(current, total, length=20):
    if not total:
        return "─" * length
    filled = int(length * current / total)
    return "█" * filled + "─" * (length - filled)

def now_playing_embed(track, current_pos=0, effect="none", state=None):
    dur = track.get("duration", 0)
    bar = progress_bar(current_pos, dur)
    elapsed = format_duration(current_pos)
    total = format_duration(dur)
    effect_str = f" `[{effect.upper()}]`" if effect != "none" else ""

    embed = discord.Embed(
        title=f"🎵 Now Playing{effect_str}",
        description=f"**[{track['title']}]({track['webpage_url']})**",
        color=discord.Color.green(),
    )
    if track.get("thumbnail"):
        embed.set_thumbnail(url=track["thumbnail"])

    embed.add_field(
        name="⏱ Progress",
        value=f"`{elapsed}` {bar} `{total}`",
        inline=False
    )
    if state:
        embed.add_field(name="🎚 Effect", value=effect.capitalize(), inline=True)
        embed.add_field(name="🔊 Volume", value=f"{int(state.volume * 100)}%", inline=True)
        embed.add_field(name="📻 Radio", value="On" if state.radio else "Off", inline=True)
        q_len = len(state.queue)
        embed.add_field(name="🎶 Queue", value=f"{q_len} track(s)", inline=True)
    embed.set_footer(text="Updates every 15 seconds")
    return embed


# ── guild state ───────────────────────────────────────────────────────────────

class GuildState:
    def __init__(self):
        self.queue = deque()
        self.current = None
        self.loop_track = False
        self.loop_queue = False
        self.volume = 0.5
        self.radio = False
        self.effect = "none"
        self.start_time = None
        self.np_message = None      # the live now playing message
        self.np_task = None         # the background updater task
        self.np_channel = None      # channel to post/update in

guild_states = {}

def get_state(guild_id):
    if guild_id not in guild_states:
        guild_states[guild_id] = GuildState()
    return guild_states[guild_id]


# ── ffmpeg effect options ─────────────────────────────────────────────────────

def get_ffmpeg_options(effect="none", seek=0):
    base_before = "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5"
    if seek:
        base_before += f" -ss {seek}"
    effects = {
        "none":      "-vn",
        "bassboost": "-vn -af equalizer=f=40:width_type=o:width=2:g=5",
        "nightcore": "-vn -af aresample=48000,asetrate=48000*1.25",
        "slowed":    "-vn -af aresample=48000,asetrate=48000*0.85",
    }
    return {
        "before_options": base_before,
        "options": effects.get(effect, "-vn"),
        "executable": r"C:\ffmpeg\bin\ffmpeg.exe",
    }


# ── cog ───────────────────────────────────────────────────────────────────────

class Music(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    # ── live now playing updater ──────────────────────────────────────────────

    async def _live_updater(self, guild_id: int):
        """Background task: edits the now playing message every 15 seconds."""
        state = get_state(guild_id)
        while True:
            await asyncio.sleep(15)
            if not state.current or not state.np_message:
                break
            try:
                elapsed = int(time.time() - state.start_time) if state.start_time else 0
                elapsed = min(elapsed, state.current.get("duration", elapsed))
                embed = now_playing_embed(state.current, elapsed, state.effect, state)
                await state.np_message.edit(embed=embed)
            except Exception:
                break

    def _start_live_updater(self, guild_id: int):
        state = get_state(guild_id)
        if state.np_task and not state.np_task.done():
            state.np_task.cancel()
        state.np_task = self.bot.loop.create_task(self._live_updater(guild_id))

    # ── internal playback ─────────────────────────────────────────────────────

    async def _play_next(self, interaction):
        guild = interaction.guild
        state = get_state(guild.id)
        vc = guild.voice_client

        if not vc:
            return

        if state.loop_track and state.current:
            track = state.current
        elif state.queue:
            track = state.queue.popleft()
            if state.loop_queue:
                state.queue.append(track)
            state.current = track
        elif state.radio and state.current:
            try:
                related_query = f"{state.current['uploader']} music"
                track = await fetch_source(related_query, self.bot.loop)
                state.current = track
            except:
                await state.np_channel.send("📻 Radio couldn't find a related song. Stopping.")
                await vc.disconnect()
                return
        else:
            state.current = None
            if state.np_task:
                state.np_task.cancel()
            if state.np_channel:
                await state.np_channel.send("✅ Queue finished. Leaving voice channel.")
            await vc.disconnect()
            return

        ffmpeg_opts = get_ffmpeg_options(state.effect)
        source = discord.FFmpegPCMAudio(track["url"], **ffmpeg_opts)
        source = discord.PCMVolumeTransformer(source, volume=state.volume)
        state.start_time = time.time()
        record_play(track["title"])

        def after(error):
            if error:
                print(f"Player error: {error}")
            asyncio.run_coroutine_threadsafe(self._play_next(interaction), self.bot.loop)

        vc.play(source, after=after)

        # Send / update the live now playing message
        embed = now_playing_embed(track, 0, state.effect, state)
        if state.np_message:
            try:
                await state.np_message.edit(embed=embed)
            except Exception:
                state.np_message = await state.np_channel.send(embed=embed)
        else:
            state.np_message = await state.np_channel.send(embed=embed)

        self._start_live_updater(guild.id)

    async def _ensure_voice(self, interaction):
        if not interaction.user.voice:
            await interaction.response.send_message("❌ You must be in a voice channel.", ephemeral=True)
            return None
        vc = interaction.guild.voice_client
        if not vc:
            vc = await interaction.user.voice.channel.connect()
        elif vc.channel != interaction.user.voice.channel:
            await vc.move_to(interaction.user.voice.channel)
        return vc

    # ── /play ─────────────────────────────────────────────────────────────────

    @app_commands.command(name="play", description="Play a song from YouTube or Spotify.")
    @app_commands.describe(query="Song name, YouTube URL, or Spotify URL")
    async def play(self, interaction: discord.Interaction, query: str):
        vc = await self._ensure_voice(interaction)
        if not vc:
            return
        await interaction.response.defer()
        state = get_state(interaction.guild.id)
        state.np_channel = interaction.channel

        if is_spotify_url(query):
            queries = spotify_to_search_query(query)
            if not queries:
                return await interaction.followup.send("❌ Could not resolve Spotify URL.")
            added = 0
            for q in queries:
                try:
                    track = await fetch_source(q, self.bot.loop)
                    state.queue.append(track)
                    added += 1
                except Exception as e:
                    print(f"Skipping {q}: {e}")
            await interaction.followup.send(f"➕ Added **{added}** track(s) from Spotify.")
        else:
            try:
                track = await fetch_source(query, self.bot.loop)
                state.queue.append(track)
                if vc.is_playing():
                    await interaction.followup.send(f"➕ Added to queue: **{track['title']}**")
            except Exception as e:
                return await interaction.followup.send(f"❌ Error: {e}")

        if not vc.is_playing():
            await self._play_next(interaction)

    # ── /skip ─────────────────────────────────────────────────────────────────

    @app_commands.command(name="skip", description="Skip the current track.")
    async def skip(self, interaction: discord.Interaction):
        vc = interaction.guild.voice_client
        if vc and vc.is_playing():
            vc.stop()
            await interaction.response.send_message("⏭ Skipped.")
        else:
            await interaction.response.send_message("❌ Nothing is playing.", ephemeral=True)

    # ── /pause & /resume ──────────────────────────────────────────────────────

    @app_commands.command(name="pause", description="Pause playback.")
    async def pause(self, interaction: discord.Interaction):
        vc = interaction.guild.voice_client
        if vc and vc.is_playing():
            vc.pause()
            await interaction.response.send_message("⏸ Paused.")
        else:
            await interaction.response.send_message("❌ Nothing is playing.", ephemeral=True)

    @app_commands.command(name="resume", description="Resume playback.")
    async def resume(self, interaction: discord.Interaction):
        vc = interaction.guild.voice_client
        if vc and vc.is_paused():
            vc.resume()
            await interaction.response.send_message("▶️ Resumed.")
        else:
            await interaction.response.send_message("❌ Not paused.", ephemeral=True)

    # ── /stop ─────────────────────────────────────────────────────────────────

    @app_commands.command(name="stop", description="Stop playback and disconnect.")
    async def stop(self, interaction: discord.Interaction):
        state = get_state(interaction.guild.id)
        state.queue.clear()
        state.current = None
        state.radio = False
        if state.np_task:
            state.np_task.cancel()
        vc = interaction.guild.voice_client
        if vc:
            vc.stop()
            await vc.disconnect()
        await interaction.response.send_message("⏹ Stopped and disconnected.")

    # ── /queue ────────────────────────────────────────────────────────────────

    @app_commands.command(name="queue", description="Show the current music queue (paginated).")
    @app_commands.describe(page="Page number")
    async def queue(self, interaction: discord.Interaction, page: int = 1):
        state = get_state(interaction.guild.id)
        if not state.queue and not state.current:
            return await interaction.response.send_message("📭 The queue is empty.", ephemeral=True)

        per_page = 10
        q = list(state.queue)
        total_pages = max(1, (len(q) + per_page - 1) // per_page)
        page = max(1, min(page, total_pages))
        start = (page - 1) * per_page
        end = start + per_page

        lines = []
        if state.current and page == 1:
            lines.append(f"**Now Playing:** {state.current['title']} `[{format_duration(state.current['duration'])}]`")
        for i, t in enumerate(q[start:end], start + 1):
            lines.append(f"`{i}.` {t['title']} `[{format_duration(t['duration'])}]`")

        embed = discord.Embed(
            title=f"🎶 Queue — Page {page}/{total_pages}",
            description="\n".join(lines),
            color=discord.Color.blurple()
        )
        embed.set_footer(text=f"{len(q)} song(s) in queue")
        await interaction.response.send_message(embed=embed)

    # ── /nowplaying ───────────────────────────────────────────────────────────

    @app_commands.command(name="nowplaying", description="Show what's currently playing with live progress bar.")
    async def nowplaying(self, interaction: discord.Interaction):
        state = get_state(interaction.guild.id)
        if not state.current:
            return await interaction.response.send_message("❌ Nothing is playing.", ephemeral=True)
        elapsed = int(time.time() - state.start_time) if state.start_time else 0
        elapsed = min(elapsed, state.current.get("duration", elapsed))
        embed = now_playing_embed(state.current, elapsed, state.effect, state)
        msg = await interaction.response.send_message(embed=embed)
        # Update the tracked np message so the updater edits this one
        state.np_message = await interaction.original_response()
        state.np_channel = interaction.channel
        self._start_live_updater(interaction.guild.id)

    # ── /seek ─────────────────────────────────────────────────────────────────

    @app_commands.command(name="seek", description="Seek to a position in the current track.")
    @app_commands.describe(seconds="Position in seconds to seek to")
    async def seek(self, interaction: discord.Interaction, seconds: int):
        state = get_state(interaction.guild.id)
        vc = interaction.guild.voice_client
        if not state.current or not vc:
            return await interaction.response.send_message("❌ Nothing is playing.", ephemeral=True)

        await interaction.response.defer()
        track = state.current
        ffmpeg_opts = get_ffmpeg_options(state.effect, seek=seconds)
        source = discord.FFmpegPCMAudio(track["url"], **ffmpeg_opts)
        source = discord.PCMVolumeTransformer(source, volume=state.volume)
        state.start_time = time.time() - seconds

        def after(error):
            if error:
                print(f"Seek error: {error}")
            asyncio.run_coroutine_threadsafe(self._play_next(interaction), self.bot.loop)

        vc.stop()
        vc.play(source, after=after)
        await interaction.followup.send(f"⏩ Seeked to `{format_duration(seconds)}`.")

    # ── /volume ───────────────────────────────────────────────────────────────

    @app_commands.command(name="volume", description="Set the playback volume (0–100).")
    @app_commands.describe(level="Volume level between 0 and 100")
    async def volume(self, interaction: discord.Interaction, level: app_commands.Range[int, 0, 100]):
        state = get_state(interaction.guild.id)
        state.volume = level / 100
        vc = interaction.guild.voice_client
        if vc and vc.source:
            vc.source.volume = state.volume
        await interaction.response.send_message(f"🔊 Volume set to **{level}%**.")

    # ── /loop ─────────────────────────────────────────────────────────────────

    @app_commands.command(name="loop", description="Toggle loop mode.")
    @app_commands.choices(mode=[
        app_commands.Choice(name="track", value="track"),
        app_commands.Choice(name="queue", value="queue"),
        app_commands.Choice(name="off", value="off"),
    ])
    async def loop(self, interaction: discord.Interaction, mode: str):
        state = get_state(interaction.guild.id)
        if mode == "track":
            state.loop_track = not state.loop_track
            await interaction.response.send_message(f"🔂 Track loop: {'ON' if state.loop_track else 'OFF'}")
        elif mode == "queue":
            state.loop_queue = not state.loop_queue
            await interaction.response.send_message(f"🔁 Queue loop: {'ON' if state.loop_queue else 'OFF'}")
        else:
            state.loop_track = False
            state.loop_queue = False
            await interaction.response.send_message("🔕 Looping disabled.")

    # ── /shuffle ──────────────────────────────────────────────────────────────

    @app_commands.command(name="shuffle", description="Shuffle the queue.")
    async def shuffle(self, interaction: discord.Interaction):
        state = get_state(interaction.guild.id)
        q = list(state.queue)
        random.shuffle(q)
        state.queue = deque(q)
        await interaction.response.send_message("🔀 Queue shuffled.")

    # ── /remove ───────────────────────────────────────────────────────────────

    @app_commands.command(name="remove", description="Remove a track from the queue by position.")
    @app_commands.describe(index="Position of the track to remove")
    async def remove(self, interaction: discord.Interaction, index: int):
        state = get_state(interaction.guild.id)
        q = list(state.queue)
        if index < 1 or index > len(q):
            return await interaction.response.send_message("❌ Invalid index.", ephemeral=True)
        removed = q.pop(index - 1)
        state.queue = deque(q)
        await interaction.response.send_message(f"🗑 Removed: **{removed['title']}**")

    # ── /disconnect ───────────────────────────────────────────────────────────

    @app_commands.command(name="disconnect", description="Disconnect the bot from voice.")
    async def disconnect(self, interaction: discord.Interaction):
        vc = interaction.guild.voice_client
        if vc:
            await vc.disconnect()
            await interaction.response.send_message("👋 Disconnected.")
        else:
            await interaction.response.send_message("❌ Not connected.", ephemeral=True)

    # ── /radio ────────────────────────────────────────────────────────────────

    @app_commands.command(name="radio", description="Toggle radio mode — endlessly autoplays related songs.")
    async def radio(self, interaction: discord.Interaction):
        state = get_state(interaction.guild.id)
        state.radio = not state.radio
        await interaction.response.send_message(f"📻 Radio mode: {'ON' if state.radio else 'OFF'}")

    # ── /randomplay ───────────────────────────────────────────────────────────

    @app_commands.command(name="randomplay", description="Play a random song from a genre.")
    @app_commands.describe(genre="Genre to search (e.g. lofi, jazz, rock, pop, classical)")
    async def randomplay(self, interaction: discord.Interaction, genre: str):
        vc = await self._ensure_voice(interaction)
        if not vc:
            return
        await interaction.response.defer()
        state = get_state(interaction.guild.id)
        state.np_channel = interaction.channel

        seeds = [
            f"{genre} music mix",
            f"best {genre} songs",
            f"{genre} playlist",
            f"popular {genre}",
            f"top {genre} hits",
        ]
        query = random.choice(seeds)
        try:
            track = await fetch_source(query, self.bot.loop)
            state.queue.append(track)
            if vc.is_playing():
                await interaction.followup.send(f"🎲 Added random **{genre}** song: **{track['title']}**")
            else:
                await interaction.followup.send(f"🎲 Playing random **{genre}** song!")
                await self._play_next(interaction)
        except Exception as e:
            await interaction.followup.send(f"❌ Error: {e}")

    # ── /effect ───────────────────────────────────────────────────────────────

    @app_commands.command(name="effect", description="Apply an audio effect to the current song.")
    @app_commands.choices(effect=[
        app_commands.Choice(name="none", value="none"),
        app_commands.Choice(name="bassboost", value="bassboost"),
        app_commands.Choice(name="nightcore", value="nightcore"),
        app_commands.Choice(name="slowed", value="slowed"),
    ])
    async def effect(self, interaction: discord.Interaction, effect: str):
        state = get_state(interaction.guild.id)
        vc = interaction.guild.voice_client
        state.effect = effect

        if not state.current or not vc or not vc.is_playing():
            await interaction.response.send_message(f"✅ Effect set to **{effect}**. Will apply on next song.")
            return

        await interaction.response.defer()
        track = state.current
        elapsed = int(time.time() - state.start_time) if state.start_time else 0
        ffmpeg_opts = get_ffmpeg_options(effect, seek=elapsed)
        source = discord.FFmpegPCMAudio(track["url"], **ffmpeg_opts)
        source = discord.PCMVolumeTransformer(source, volume=state.volume)
        state.start_time = time.time() - elapsed

        def after(error):
            if error:
                print(f"Effect error: {error}")
            asyncio.run_coroutine_threadsafe(self._play_next(interaction), self.bot.loop)

        vc.stop()
        vc.play(source, after=after)
        labels = {"none": "🔊 Normal", "bassboost": "🔊 Bass Boost", "nightcore": "⚡ Nightcore", "slowed": "🐢 Slowed"}
        await interaction.followup.send(f"{labels[effect]} effect applied!")

    # ── /lyrics ───────────────────────────────────────────────────────────────

    @app_commands.command(name="lyrics", description="Show lyrics for the current or a specific song.")
    @app_commands.describe(song="Song name (leave empty for current song)")
    async def lyrics(self, interaction: discord.Interaction, song: str = None):
        await interaction.response.defer()
        state = get_state(interaction.guild.id)

        if not song:
            if not state.current:
                return await interaction.followup.send("❌ Nothing is playing.", ephemeral=True)
            song = state.current["title"]

        try:
            import urllib.request, urllib.parse
            query = urllib.parse.quote(song)
            url = f"https://lyrist.vercel.app/api/{query}"
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=5) as r:
                data = json.loads(r.read())
            lyrics = data.get("lyrics", "")
            artist = data.get("artist", "")
            title = data.get("title", song)

            if not lyrics:
                return await interaction.followup.send("❌ Lyrics not found.")

            chunks = [lyrics[i:i+4000] for i in range(0, len(lyrics), 4000)]
            embed = discord.Embed(
                title=f"🎵 {title}",
                description=chunks[0],
                color=discord.Color.purple()
            )
            if artist:
                embed.set_footer(text=f"Artist: {artist}")
            await interaction.followup.send(embed=embed)
            for chunk in chunks[1:]:
                await interaction.channel.send(embed=discord.Embed(description=chunk, color=discord.Color.purple()))
        except Exception as e:
            await interaction.followup.send(f"❌ Could not fetch lyrics: {e}")

    # ── /saveplaylist ─────────────────────────────────────────────────────────

    @app_commands.command(name="saveplaylist", description="Save the current queue as a named playlist.")
    @app_commands.describe(name="Name for your playlist")
    async def saveplaylist(self, interaction: discord.Interaction, name: str):
        state = get_state(interaction.guild.id)
        if not state.queue and not state.current:
            return await interaction.response.send_message("❌ Nothing in queue to save.", ephemeral=True)

        playlists = load_json(PLAYLISTS_FILE)
        guild_pl = playlists.get(str(interaction.guild.id), {})
        tracks = ([state.current] if state.current else []) + list(state.queue)
        guild_pl[name] = tracks
        playlists[str(interaction.guild.id)] = guild_pl
        save_json(PLAYLISTS_FILE, playlists)
        await interaction.response.send_message(f"💾 Saved **{len(tracks)}** tracks as playlist `{name}`.")

    # ── /loadplaylist ─────────────────────────────────────────────────────────

    @app_commands.command(name="loadplaylist", description="Load a saved playlist into the queue.")
    @app_commands.describe(name="Name of the playlist to load")
    async def loadplaylist(self, interaction: discord.Interaction, name: str):
        vc = await self._ensure_voice(interaction)
        if not vc:
            return

        playlists = load_json(PLAYLISTS_FILE)
        guild_pl = playlists.get(str(interaction.guild.id), {})
        if name not in guild_pl:
            return await interaction.response.send_message(f"❌ Playlist `{name}` not found.", ephemeral=True)

        state = get_state(interaction.guild.id)
        state.np_channel = interaction.channel
        tracks = guild_pl[name]
        for t in tracks:
            state.queue.append(t)

        await interaction.response.send_message(f"📂 Loaded **{len(tracks)}** tracks from `{name}`.")
        if not vc.is_playing():
            await self._play_next(interaction)

    # ── /playlists ────────────────────────────────────────────────────────────

    @app_commands.command(name="playlists", description="List all saved playlists.")
    async def playlists(self, interaction: discord.Interaction):
        playlists = load_json(PLAYLISTS_FILE)
        guild_pl = playlists.get(str(interaction.guild.id), {})
        if not guild_pl:
            return await interaction.response.send_message("📭 No saved playlists.", ephemeral=True)
        lines = [f"`{name}` — {len(tracks)} tracks" for name, tracks in guild_pl.items()]
        embed = discord.Embed(title="🎶 Saved Playlists", description="\n".join(lines), color=discord.Color.blurple())
        await interaction.response.send_message(embed=embed)

    # ── /deleteplaylist ───────────────────────────────────────────────────────

    @app_commands.command(name="deleteplaylist", description="Delete a saved playlist.")
    @app_commands.describe(name="Name of the playlist to delete")
    async def deleteplaylist(self, interaction: discord.Interaction, name: str):
        playlists = load_json(PLAYLISTS_FILE)
        guild_pl = playlists.get(str(interaction.guild.id), {})
        if name not in guild_pl:
            return await interaction.response.send_message(f"❌ Playlist `{name}` not found.", ephemeral=True)
        del guild_pl[name]
        playlists[str(interaction.guild.id)] = guild_pl
        save_json(PLAYLISTS_FILE, playlists)
        await interaction.response.send_message(f"🗑 Deleted playlist `{name}`.")

    # ── /leaderboard ──────────────────────────────────────────────────────────

    @app_commands.command(name="leaderboard", description="Show the most played songs.")
    async def leaderboard(self, interaction: discord.Interaction):
        leaderboard = stats.get("leaderboard", {})
        if not leaderboard:
            return await interaction.response.send_message("📭 No songs played yet.", ephemeral=True)
        sorted_songs = sorted(leaderboard.items(), key=lambda x: x[1], reverse=True)[:10]
        lines = [f"`{i}.` {title} — **{count}** plays" for i, (title, count) in enumerate(sorted_songs, 1)]
        embed = discord.Embed(title="🏆 Most Played Songs", description="\n".join(lines), color=discord.Color.gold())
        await interaction.response.send_message(embed=embed)

    # ── /stats ────────────────────────────────────────────────────────────────

    @app_commands.command(name="stats", description="Show bot music stats.")
    async def stats_cmd(self, interaction: discord.Interaction):
        total = stats.get("total_played", 0)
        unique = len(stats.get("leaderboard", {}))
        state = get_state(interaction.guild.id)
        queue_len = len(state.queue)
        embed = discord.Embed(title="📈 Music Stats", color=discord.Color.blue())
        embed.add_field(name="Total Songs Played", value=f"**{total}**", inline=True)
        embed.add_field(name="Unique Songs", value=f"**{unique}**", inline=True)
        embed.add_field(name="Songs in Queue", value=f"**{queue_len}**", inline=True)
        embed.add_field(name="Radio Mode", value="ON" if state.radio else "OFF", inline=True)
        embed.add_field(name="Current Effect", value=state.effect.capitalize(), inline=True)
        embed.add_field(name="Volume", value=f"{int(state.volume * 100)}%", inline=True)
        await interaction.response.send_message(embed=embed)


async def setup(bot):
    await bot.add_cog(Music(bot))
