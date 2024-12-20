import discord
from discord.ext import commands
import asyncio
import os
from youtubesearchpython import VideosSearch
import yt_dlp
import json
from concurrent.futures import ThreadPoolExecutor
from functools import partial

intents = discord.Intents.default()
intents.message_content = True

bot = commands.Bot(command_prefix='!', intents=intents)

class Music(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.queue = []
        self.current_song = None
        self.voice_client = None
        self.last_search_user = None
        self.sent_messages = []
        self.thread_pool = ThreadPoolExecutor(max_workers=3)
        if not os.path.exists('./downloads'):
            os.makedirs('./downloads')

    async def send_message(self, ctx, content, **kwargs):
        message = await ctx.send(content, **kwargs, silent=True)
        self.sent_messages.append(message)
        asyncio.create_task(self.delete_message_after_delay(message))
        return message
        
    async def delete_message_after_delay(self, message, delay=60):
        await asyncio.sleep(delay)
        try:
            await message.delete()
        except discord.errors.NotFound:
            pass
        if message in self.sent_messages:
            self.sent_messages.remove(message)

    @commands.command()
    async def play(self, ctx, *, query=None):
        if not query:
            await self.send_message(ctx, "Please provide a search query or YouTube URL. Usage: `!play <query or URL>`")
            return

        if not ctx.author.voice:
            await self.send_message(ctx, "You need to be in a voice channel to use this command.")
            return

        try:
            if not self.voice_client or not self.voice_client.is_connected():
                self.voice_client = await ctx.author.voice.channel.connect()
        except discord.errors.ClientException:
            if self.voice_client and self.voice_client.guild != ctx.guild:
                await self.send_message(ctx, "I'm already being used in another server.")
                return
        except Exception as e:
            await self.send_message(ctx, f"An error occurred while connecting: {str(e)}")
            return

        if 'youtube.com/watch?v=' in query or 'youtu.be/' in query:
            await self.add_to_queue(ctx, query)
        else:
            results = await self.search_videos(query)
            if not results:
                await self.send_message(ctx, "No results found for your search query.")
                return
            await self.send_search_results(ctx, results)

    @commands.command()
    async def stop(self, ctx):
        if self.voice_client and self.voice_client.is_playing():
            self.voice_client.stop()
            await self.send_message(ctx, "Playback stopped.")
        else:
            await self.send_message(ctx, "Nothing is currently playing.")

    @commands.command()
    async def skip(self, ctx):
        if self.voice_client and self.voice_client.is_playing():
            self.voice_client.stop()
            await self.send_message(ctx, "Skipped the current song.")
        else:
            await self.send_message(ctx, "Nothing is currently playing.")

    @commands.command()
    async def queue(self, ctx):
        if not self.queue:
            await self.send_message(ctx, "The queue is empty. Use `!play` to add songs!")
        else:
            queue_list = "Current queue:\n"
            for i, song in enumerate(self.queue, 1):
                queue_list += f"{i}. {song['title']}\n"
            await self.send_message(ctx, queue_list)
            
    async def send_search_results(self, ctx, results):
        self.last_search_user = ctx.author
        embed = discord.Embed(title="Select a song to play:", color=discord.Color.blue())
        
        for i, result in enumerate(results, 1):
            truncated_title = result['title'][:70]
            if len(result['title']) > 70:
                truncated_title += '...'
            embed.add_field(
                name=f"{i}. {truncated_title}",
                value=f"Channel: {result['channel']} | Duration: {result['duration']}",
                inline=False
            )

        view = discord.ui.View(timeout=30)
        for i, result in enumerate(results):
            button = discord.ui.Button(
                label=str(i+1), 
                style=discord.ButtonStyle.primary, 
                custom_id=str(i)
            )
            button.callback = lambda interaction, i=i: self.button_callback(
                interaction, 
                results[i]['url'], 
                results[i]['title']
            )
            view.add_item(button)

        message = await ctx.send(embed=embed, view=view, silent=True)
        self.sent_messages.append(message)

        async def cleanup():
            await asyncio.sleep(30)
            try:
                await message.delete()
            except:
                pass
            if message in self.sent_messages:
                self.sent_messages.remove(message)

        asyncio.create_task(cleanup())

    async def button_callback(self, interaction, url, title):
        if interaction.user != self.last_search_user:
            await interaction.response.send_message(
                "You can't use this button.", 
                ephemeral=True
            )
            return

        await interaction.response.defer()
        try:
            await interaction.message.delete()
        except:
            pass
        await self.add_to_queue(interaction.channel, url, title)

    async def add_to_queue(self, ctx, url, title=None):
        status_msg = await self.send_message(
            ctx, 
            f"⏳ Adding {'**' + title + '**' if title else 'song'} to queue..."
        )

        try:
            song_info = await self.download_audio(url)
            if not song_info:
                await status_msg.edit(content="❌ Failed to download the audio. Please try again.")
                return

            self.queue.append(song_info)
            await status_msg.edit(
                content=f"✅ Added to queue: **{song_info['title']}**"
            )

            if not self.voice_client.is_playing():
                await self.play_next(ctx)

        except Exception as e:
            await status_msg.edit(
                content=f"❌ Error adding song to queue: {str(e)}"
            )
            print(f"Error in add_to_queue: {str(e)}")

    async def search_videos(self, query, max_results=5):
        try:
            videos_search = VideosSearch(query, limit=max_results)
            results = []
            for video in videos_search.result()['result']:
                results.append({
                    'title': video['title'],
                    'url': video['link'],
                    'duration': video['duration'],
                    'channel': video['channel']['name']
                })
            return results
        except Exception as e:
            print(f"Search error: {str(e)}")
            return []

    def download_progress_hook(self, d):
        if d['status'] == 'downloading':
            try:
                percent = d['_percent_str']
                speed = d['_speed_str']
                print(f"Download Progress: {percent} at {speed}")
            except Exception as e:
                print(f"Error in progress hook: {str(e)}")
        elif d['status'] == 'finished':
            print('Download finished, now converting...')
        elif d['status'] == 'error':
            print(f"Error in download: {d.get('error', 'Unknown error')}")

    def download_audio_sync(self, url, output_path='./downloads'):
        ydl_opts = {
            'format': 'bestaudio[ext=m4a]/bestaudio/best',  # Prefer m4a but fallback to any audio
            'postprocessors': [],  # No conversion needed
            'outtmpl': os.path.join(output_path, '%(title)s-%(id)s.%(ext)s'),
            'restrictfilenames': True,
            'noplaylist': True,
            'nocheckcertificate': True,
            'ignoreerrors': False,
            'logtostderr': False,
            'quiet': False,
            'no_warnings': False,
            'progress_hooks': [self.download_progress_hook]
        }

        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                print(f"Starting download process for: {url}")
                info = ydl.extract_info(url, download=True)
                
                if not info:
                    print("Failed to extract video information")
                    return None
                
                # Get the filename with original extension
                filename = ydl.prepare_filename(info)
                
                if not os.path.exists(filename):
                    print(f"File not found: {filename}")
                    return None
                    
                print(f"Download complete: {filename}")
                return {
                    'title': info['title'],
                    'filename': filename,
                    'duration': info.get('duration', 0)
                }
        except Exception as e:
            print(f"Download error: {str(e)}")
            return None

    async def download_audio(self, url):
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(
            self.thread_pool,
            partial(self.download_audio_sync, url)
        )

    async def play_next(self, ctx):
        if not self.queue:
            self.current_song = None
            await self.send_message(ctx, "Queue is empty!")
            return

        try:
            self.current_song = self.queue.pop(0)
            if not os.path.exists(self.current_song['filename']):
                await self.send_message(
                    ctx, 
                    f"❌ File not found for {self.current_song['title']}"
                )
                await self.play_next(ctx)
                return

            source = discord.FFmpegPCMAudio(self.current_song['filename'])
            
            def after_callback(error):
                if error:
                    print(f"Playback error: {error}")
                asyncio.run_coroutine_threadsafe(
                    self.cleanup_and_play_next(ctx), 
                    self.bot.loop
                )

            self.voice_client.play(source, after=after_callback)
            await self.send_message(
                ctx,
                f"🎵 Now playing: **{self.current_song['title']}**"
            )

        except Exception as e:
            print(f"Error in play_next: {e}")
            await self.send_message(ctx, f"❌ Playback error: {str(e)}")
            await self.play_next(ctx)

    async def cleanup_and_play_next(self, ctx):
        try:
            if self.current_song and os.path.exists(self.current_song['filename']):
                os.remove(self.current_song['filename'])
        except Exception as e:
            print(f"Cleanup error: {e}")
        
        await self.play_next(ctx)

    def cog_unload(self):
        self.thread_pool.shutdown(wait=False)
      
async def setup(bot):
    await bot.add_cog(Music(bot))

@bot.event
async def on_ready():
    print(f'Logged in as {bot.user.name}')
    await setup(bot)

@bot.event
async def on_message(message):
    if message.author != bot.user and message.content.startswith(bot.command_prefix):
        asyncio.create_task(delete_message_after_delay(message))
    await bot.process_commands(message)

async def delete_message_after_delay(message, delay=60):
    await asyncio.sleep(delay)
    try:
        await message.delete()
    except discord.errors.NotFound:
        pass

@bot.event
async def on_command_error(ctx, error):
    if isinstance(error, commands.CommandNotFound):
        await ctx.send("Unknown command. Use `!help` to see available commands.", silent=True)
    elif isinstance(error, commands.MissingRequiredArgument):
        await ctx.send(f"Missing required argument: {error.param.name}", silent=True)
    else:
        await ctx.send(f"An error occurred: {str(error)}", silent=True)

def load_credentials():
    with open('creds.json') as f:
        return json.load(f)

credentials = load_credentials()
bot.run(credentials['token'])