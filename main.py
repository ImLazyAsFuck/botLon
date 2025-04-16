import discord
from discord.ext import commands, tasks
import os
import aiohttp
from dotenv import load_dotenv
import datetime
import asyncio
import sqlite3
from cachetools import TTLCache
import random
import json

# Tải biến môi trường từ .env
load_dotenv()
TOKEN = os.getenv('DISCORD_TOKEN')
CHANNEL_ID = os.getenv('CHANNEL_ID')
PREFIX = os.getenv('PREFIX', 'v!')

if not TOKEN or not CHANNEL_ID:
    raise ValueError("Thiếu DISCORD_TOKEN hoặc CHANNEL_ID trong file .env")

# Cấu hình
ANILIST_API = "https://graphql.anilist.co"
JIKAN_API = "https://api.jikan.moe/v4"
WAIFU_IM_API = "https://api.waifu.im"
CHECK_INTERVAL = 3600
DAILY_CHECK_HOUR = 8
CACHE_TTL = 3600
WAIFU_PIC_INTERVAL = 10  # phút

# Danh sách thể loại hợp lệ
GENRE_LIST = [
    "action", "adventure", "comedy", "drama", "fantasy", "horror", "mystery", "romance",
    "sci-fi", "slice of life", "sports", "supernatural", "ecchi", "historical", "isekai",
    "mecha", "music", "psychological", "school", "shounen", "shoujo", "seinen", "josei"
]

# Danh sách tên và từ khóa để xác định nhân vật nữ
FEMALE_NAME_PATTERNS = [
    "sakura", "hinata", "yuki", "miku", "asuka", "rei", "misaki", "haruka", "ayaka", "chika",
    "chan", "san", "ko", "ka", "mi", "na", "rin", "sama", "tsuki", "hana", "yuna", "aoi",
    "emilia", "rem", "ram", "mikasa", "nobara", "maki", "mai", "yor", "anya", "kaguya",
    "shoko", "marin", "nezuko", "saber", "violet", "erza", "lucy", "nami", "robin"
]
FEMALE_KEYWORDS = [
    "she", "her", "girl", "female", "woman", "lady", "princess", "queen", "sister", "daughter",
    "wife", "mother", "girlfriend", "heroine", "maid", "idol", "magical girl"
]
MALE_KEYWORDS = [
    "he", "his", "boy", "male", "man", "gentleman", "king", "prince", "brother", "son",
    "husband", "father", "boyfriend", "hero", "warrior", "knight", "soldier", "ninja",
    "samurai", "pirate", "captain", "commander", "leader"
]

# Phản hồi vui nhộn
RESPONSES = [" 😍", " 💖", " 🔥"]

# Khởi tạo bot
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix=PREFIX, intents=intents)

# Cache API
cache = TTLCache(maxsize=100, ttl=CACHE_TTL)

# Khởi tạo database
def init_db():
    conn = sqlite3.connect('waifu.db')
    conn.execute('CREATE TABLE IF NOT EXISTS votes (user_id TEXT, waifu TEXT)')
    conn.execute('CREATE TABLE IF NOT EXISTS rankings (genre TEXT, data TEXT)')  # Lưu bảng xếp hạng
    conn.commit()
    conn.close()

# Lớp WaifuAPI (dùng Waifu.im API)
class WaifuAPI:
    def __init__(self):
        self.session = None

    async def get_session(self):
        if self.session is None or self.session.closed:
            self.session = aiohttp.ClientSession()
        return self.session

    async def get_random_waifu(self, nsfw=False):
        params = {
            "is_nsfw": "true" if nsfw else "false",
            "many": "false"
        }
        session = await self.get_session()
        for attempt in range(3):
            try:
                async with session.get(f"{WAIFU_IM_API}/search", params=params) as resp:
                    if resp.status != 200:
                        print(f"Lỗi Waifu.im API: Mã trạng thái {resp.status}")
                        if attempt < 2:
                            await asyncio.sleep(2)
                            continue
                        return None
                    result = await resp.json()
                    if not result or 'images' not in result or not result['images']:
                        print("Lỗi Waifu.im API: Không nhận được dữ liệu hợp lệ")
                        return None
                    return result
            except Exception as e:
                print(f"Lỗi Waifu.im API: {e}")
                if attempt < 2:
                    await asyncio.sleep(2)
                    continue
                return None

    async def get_popular_waifus(self, limit=10):
        params = {
            "included_tags": "waifu",
            "many": "true",
            "limit": limit
        }
        session = await self.get_session()
        for attempt in range(3):
            try:
                async with session.get(f"{WAIFU_IM_API}/search", params=params) as resp:
                    if resp.status != 200:
                        print(f"Lỗi Waifu.im API (popular): Mã trạng thái {resp.status}")
                        if attempt < 2:
                            await asyncio.sleep(2)
                            continue
                        return None
                    result = await resp.json()
                    if not result or 'images' not in result or not result['images']:
                        print("Lỗi Waifu.im API (popular): Không nhận được dữ liệu hợp lệ")
                        return None
                    return result['images']
            except Exception as e:
                print(f"Lỗi Waifu.im API (popular): {e}")
                if attempt < 2:
                    await asyncio.sleep(2)
                    continue
                return None

    async def close(self):
        if self.session and not self.session.closed:
            await self.session.close()

# Lớp AniListClient
class AniListClient:
    def __init__(self):
        self.session = None
        self.last_checked_anime_id = 0
        self.last_checked_waifu_id = 0

    async def get_session(self):
        if self.session is None or self.session.closed:
            self.session = aiohttp.ClientSession()
        return self.session

    async def query(self, query, variables=None):
        cache_key = str((query, variables))
        if cache_key in cache:
            return cache[cache_key]
        session = await self.get_session()
        for attempt in range(3):
            try:
                async with session.post(ANILIST_API, json={"query": query, "variables": variables}) as resp:
                    if resp.status != 200:
                        print(f"Lỗi AniList API: Mã trạng thái {resp.status}")
                        if resp.status == 400:
                            print(f"Truy vấn lỗi: {query}, Biến: {variables}")
                        if attempt < 2:
                            await asyncio.sleep(2)
                            continue
                        return None
                    result = await resp.json()
                    if not result or 'data' not in result:
                        print("Lỗi AniList API: Không nhận được dữ liệu hợp lệ")
                        return None
                    cache[cache_key] = result
                    await asyncio.sleep(0.5)
                    return result
            except Exception as e:
                print(f"Lỗi AniList API: {e}")
                if attempt < 2:
                    await asyncio.sleep(2)
                    continue
                return None

    async def search_media(self, media_type, query):
        gql_query = """
        query ($search: String, $type: MediaType) {
            Media(search: $search, type: $type) {
                id
                title { romaji english }
                description
                averageScore
                status
                startDate { year month day }
                endDate { year month day }
                episodes
                chapters
                coverImage { large }
                siteUrl
            }
        }
        """
        variables = {"search": query, "type": media_type.upper()}
        return await self.query(gql_query, variables)

    async def search_character(self, query):
        gql_query = """
        query ($search: String) {
            Character(search: $search) {
                id
                name { full }
                description
                image { large }
                siteUrl
            }
        }
        """
        variables = {"search": query}
        return await self.query(gql_query, variables)

    async def get_trending(self, media_type, limit=10, genre=None):
        gql_query = """
        query ($type: MediaType, $perPage: Int, $genre: String) {
            Page(perPage: $perPage) {
                media(type: $type, sort: TRENDING_DESC, genre: $genre) {
                    id
                    title { romaji }
                    averageScore
                    startDate { year month day }
                }
            }
        }
        """
        variables = {"type": media_type.upper(), "perPage": limit, "genre": genre}
        return await self.query(gql_query, variables)

    async def get_top_characters(self, limit=50):
        gql_query = """
        query ($perPage: Int) {
            Page(perPage: $perPage) {
                characters(sort: FAVOURITES_DESC) {
                    id
                    name { full }
                    description
                    media {
                        nodes {
                            title { romaji }
                        }
                    }
                    image { large }
                }
            }
        }
        """
        variables = {"perPage": limit}
        return await self.query(gql_query, variables)

    async def get_new_releases_today(self):
        gql_query = """
        query ($perPage: Int) {
            Page(perPage: $perPage) {
                media(type: ANIME, sort: START_DATE_DESC) {
                    id
                    title { romaji }
                    description
                    coverImage { large }
                    siteUrl
                    startDate { year month day }
                }
            }
        }
        """
        variables = {"perPage": 50}
        return await self.query(gql_query, variables)

    async def get_characters_from_anime(self, anime_id):
        gql_query = """
        query ($id: Int) {
            Media(id: $id) {
                characters(sort: RELEVANCE, perPage: 10) {
                    nodes {
                        id
                        name { full }
                        description
                        image { large }
                        siteUrl
                    }
                }
            }
        }
        """
        variables = {"id": anime_id}
        return await self.query(gql_query, variables)

    async def get_airing_today(self):
        today = int(datetime.datetime.now().timestamp())
        tomorrow = int((datetime.datetime.now() + datetime.timedelta(days=1)).timestamp())
        gql_query = """
        query ($airingAt_greater: Int, $airingAt_lesser: Int) {
            Page(perPage: 5) {
                airingSchedules(airingAt_greater: $airingAt_greater, airingAt_lesser: $airingAt_lesser) {
                    airingAt
                    episode
                    media {
                        id
                        title { romaji }
                        description
                        coverImage { large }
                        siteUrl
                    }
                }
            }
        }
        """
        variables = {"airingAt_greater": today, "airingAt_lesser": tomorrow}
        return await self.query(gql_query, variables)

    async def close(self):
        if self.session and not self.session.closed:
            await self.session.close()

# Lớp JikanClient
class JikanClient:
    def __init__(self):
        self.session = None

    async def get_session(self):
        if self.session is None or self.session.closed:
            self.session = aiohttp.ClientSession()
        return self.session

    async def query(self, endpoint):
        cache_key = f"jikan_{endpoint}"
        if cache_key in cache:
            return cache[cache_key]
        session = await self.get_session()
        url = f"{JIKAN_API}{endpoint}"
        for attempt in range(3):
            try:
                async with session.get(url) as resp:
                    if resp.status != 200:
                        print(f"Lỗi Jikan API: Mã trạng thái {resp.status}")
                        if resp.status == 400:
                            print(f"Endpoint lỗi: {url}")
                        if attempt < 2:
                            await asyncio.sleep(2)
                            continue
                        return None
                    result = await resp.json()
                    if not result or 'data' not in result:
                        print("Lỗi Jikan API: Không nhận được dữ liệu hợp lệ")
                        return None
                    cache[cache_key] = result
                    await asyncio.sleep(0.5)
                    return result
            except Exception as e:
                print(f"Lỗi Jikan API: {e}")
                if attempt < 2:
                    await asyncio.sleep(2)
                    continue
                return None

    async def get_new_releases_today(self):
        today = datetime.datetime.now()
        result = await self.query("/seasons/now?limit=25")
        if not result:
            return None
        new_anime = []
        for anime in result['data']:
            aired = anime.get('aired', {}).get('from')
            if aired:
                try:
                    aired_date = datetime.datetime.strptime(aired, "%Y-%m-%dT%H:%M:%S%z")
                    if aired_date.date() == today.date():
                        new_anime.append(anime)
                except ValueError:
                    continue
        return {"data": new_anime}

    async def close(self):
        if self.session and not self.session.closed:
            await self.session.close()

# Khởi tạo client
anilist = AniListClient()
jikan = JikanClient()
waifu_api = WaifuAPI()
anime_notification_channels = set()
waifu_notification_channels = set()
airing_notification_channels = {int(CHANNEL_ID)}
waifu_pic_channels = set()  # Danh sách kênh nhận ảnh waifu tự động
ranking_notification_channels = {}  # {channel_id: genre}

# Hàm kiểm tra nhân vật nữ
def is_female_character(character):
    name = character['name']['full'].lower()
    description = (character.get('description') or '').lower()
    name_matches_female = any(pattern in name for pattern in FEMALE_NAME_PATTERNS)
    desc_matches_female = any(keyword in description for keyword in FEMALE_KEYWORDS)
    desc_matches_male = any(keyword in description for keyword in MALE_KEYWORDS)
    is_female = (name_matches_female or desc_matches_female) and not desc_matches_male
    print(f"Nhân vật: {character['name']['full']}, Nữ: {is_female}, Tên khớp: {name_matches_female}, Mô tả nữ: {desc_matches_female}, Mô tả nam: {desc_matches_male}")
    return is_female

# Task: Gửi ảnh waifu tự động mỗi 10 phút
@tasks.loop(minutes=WAIFU_PIC_INTERVAL)
async def send_waifu_pic():
    if not waifu_pic_channels:
        return
    try:
        data = await waifu_api.get_random_waifu(nsfw=False)
        if not data or 'images' not in data:
            print("Không lấy được ảnh waifu tự động")
            return
        
        embed = discord.Embed(color=0xff9ff3)
        embed.set_image(url=data['images'][0]['url'])
        embed.set_footer(text=f"Nguồn: Veloria Sever")
        
        for channel_id in waifu_pic_channels:
            channel = bot.get_channel(channel_id)
            if not channel:
                continue
            await channel.send("💖 **WAIFU CỦA PHÚT NÀY** 💖", embed=embed)
            await asyncio.sleep(0.5)
    except Exception as e:
        print(f"Lỗi send_waifu_pic: {e}")

# Task: Kiểm tra và gửi bảng xếp hạng anime khi có thay đổi
@tasks.loop(seconds=CHECK_INTERVAL)
async def check_ranking_update():
    if not ranking_notification_channels:
        return
    try:
        for channel_id, genre in ranking_notification_channels.items():
            # Lấy bảng xếp hạng mới
            data = await anilist.get_trending('anime', limit=10, genre=genre)
            if not data or not data.get('data', {}).get('Page', {}).get('media'):
                continue
            
            new_ranking = [(anime['title']['romaji'], anime.get('averageScore', 'N/A')) for anime in data['data']['Page']['media']]
            
            # Lấy bảng xếp hạng cũ từ database
            conn = sqlite3.connect('waifu.db')
            cursor = conn.execute('SELECT data FROM rankings WHERE genre = ?', (genre or 'default',))
            old_ranking_data = cursor.fetchone()
            old_ranking = json.loads(old_ranking_data[0]) if old_ranking_data else []
            
            # So sánh
            if old_ranking != new_ranking:
                # Lưu bảng xếp hạng mới vào database
                conn.execute('INSERT OR REPLACE INTO rankings (genre, data) VALUES (?, ?)',
                           (genre or 'default', json.dumps(new_ranking)))
                conn.commit()
                
                # Gửi bảng xếp hạng mới
                channel = bot.get_channel(channel_id)
                if not channel:
                    continue
                embed = discord.Embed(
                    title=f"📊 Bảng Xếp Hạng Anime Mới {'('+genre+')' if genre else ''}",
                    color=0xff69b4
                )
                for i, (title, score) in enumerate(new_ranking, 1):
                    embed.add_field(
                        name=f"{i}. {title}",
                        value=f"⭐ {score}/100",
                        inline=False
                    )
                embed.set_footer(text="Nguồn: AniList")
                await channel.send("📈 **BẢNG XẾP HẠNG ANIME ĐÃ CẬP NHẬT** 📈", embed=embed)
                await asyncio.sleep(0.5)
            conn.close()
    except Exception as e:
        print(f"Lỗi check_ranking_update: {e}")

# Các task khác (giữ nguyên)
@tasks.loop(seconds=CHECK_INTERVAL)
async def check_new_anime():
    if not anime_notification_channels:
        return
    try:
        today = datetime.datetime.now()
        new_anime = []
        anilist_data = await anilist.get_new_releases_today()
        if anilist_data and anilist_data.get('data', {}).get('Page', {}).get('media'):
            for anime in anilist_data['data']['Page']['media']:
                start_date = anime.get('startDate', {})
                if (start_date.get('year') and start_date.get('month') and start_date.get('day') and
                    start_date.get('year') == today.year and
                    start_date.get('month') == today.month and
                    start_date.get('day') == today.day):
                    new_anime.append({
                        "title": anime['title']['romaji'],
                        "description": anime.get('description', 'Không có mô tả'),
                        "url": anime['siteUrl'],
                        "cover": anime.get('coverImage', {}).get('large', None),
                        "source": "AniList",
                        "id": anime['id']
                    })
        if not new_anime:
            jikan_data = await jikan.get_new_releases_today()
            if jikan_data and jikan_data.get('data'):
                for anime in jikan_data['data']:
                    new_anime.append({
                        "title": anime['title'],
                        "description": anime.get('synopsis', 'Không có mô tả'),
                        "url": anime['url'],
                        "cover": anime.get('images', {}).get('jpg', {}).get('large_image_url', None),
                        "source": "Jikan (MyAnimeList)"
                    })
        if new_anime:
            for channel_id in anime_notification_channels:
                channel = bot.get_channel(channel_id)
                if not channel:
                    continue
                for anime in new_anime[:3]:
                    embed = discord.Embed(
                        title=anime['title'],
                        description=anime['description'][:200] + '...',
                        color=0x00ff00,
                        url=anime['url']
                    )
                    if anime['cover']:
                        embed.set_image(url=anime['cover'])
                    embed.set_footer(text=f"Nguồn: {anime['source']}")
                    await channel.send("🎉 **ANIME RA MẮT HÔM NAY** 🎉", embed=embed)
                    await asyncio.sleep(0.5)
        else:
            print(f"Không có anime mới ngày {today.day}/{today.month}/{today.year}")
    except Exception as e:
        print(f"Lỗi check_new_anime: {e}")

@tasks.loop(seconds=CHECK_INTERVAL)
async def check_new_waifu():
    if not waifu_notification_channels:
        return
    try:
        today = datetime.datetime.now()
        new_anime = await anilist.get_new_releases_today()
        new_waifu = []
        if new_anime and new_anime.get('data', {}).get('Page', {}).get('media'):
            for anime in new_anime['data']['Page']['media']:
                start_date = anime.get('startDate', {})
                if (start_date.get('year') and start_date.get('month') and start_date.get('day') and
                    start_date.get('year') == today.year and
                    start_date.get('month') == today.month and
                    start_date.get('day') == today.day):
                    characters = await anilist.get_characters_from_anime(anime['id'])
                    if characters and characters.get('data', {}).get('Media', {}).get('characters', {}).get('nodes'):
                        for character in characters['data']['Media']['characters']['nodes']:
                            if is_female_character(character):
                                new_waifu.append(character)
        for channel_id in waifu_notification_channels:
            channel = bot.get_channel(channel_id)
            if not channel:
                continue
            if new_waifu:
                for character in new_waifu[:3]:
                    embed = create_character_embed(character)
                    await channel.send("💖 **WAIFU MỚI HÔM NAY** 💖", embed=embed)
                    await asyncio.sleep(0.5)
            else:
                print(f"Không có waifu mới ngày {today.day}/{today.month}/{today.year}")
    except Exception as e:
        print(f"Lỗi check_new_waifu: {e}")

@tasks.loop(hours=24)
async def check_airing_today():
    if not airing_notification_channels:
        return
    now = datetime.datetime.now()
    if now.hour != DAILY_CHECK_HOUR:
        return
    try:
        for channel_id in airing_notification_channels:
            channel = bot.get_channel(channel_id)
            if not channel:
                continue
            data = await anilist.get_airing_today()
            if not data or not data.get('data', {}).get('Page', {}).get('airingSchedules'):
                continue
            for schedule in data['data']['Page']['airingSchedules'][:3]:
                anime = schedule['media']
                embed = create_embed(anime, 'anime')
                airing_time = datetime.datetime.fromtimestamp(schedule['airingAt']).strftime('%H:%M')
                await channel.send(f"📺 **ANIME CHIẾU HÔM NAY - Tập {schedule['episode']} ({airing_time})** 📺", embed=embed)
                await asyncio.sleep(0.5)
    except Exception as e:
        print(f"Lỗi check_airing_today: {e}")

# Commands
@bot.command()
async def anime(ctx, *, query):
    """Tìm thông tin anime"""
    await search_media(ctx, 'anime', query)

@bot.command()
async def manga(ctx, *, query):
    """Tìm thông tin manga"""
    await search_media(ctx, 'manga', query)

@bot.command()
async def character(ctx, *, query):
    """Tìm thông tin nhân vật"""
    try:
        async with ctx.typing():
            data = await anilist.search_character(query)
            if not data or not data.get('data', {}).get('Character'):
                return await ctx.send("Không tìm thấy nhân vật!")
            character = data['data']['Character']
            embed = create_character_embed(character)
            await ctx.send(embed=embed)
    except Exception as e:
        print(f"Lỗi character command: {e}")
        await ctx.send("Đã xảy ra lỗi khi tìm nhân vật!")

@bot.command()
async def top(ctx, genre=None):
    """Top 10 anime (có thể chọn thể loại)"""
    try:
        genre_name = genre
        if genre:
            genre = genre.lower()
            if genre not in GENRE_LIST:
                return await ctx.send(f"Thể loại '{genre}' không hợp lệ! Các thể loại: {', '.join(GENRE_LIST)}")
        async with ctx.typing():
            data = await anilist.get_trending('anime', limit=10, genre=genre)
            if not data or not data.get('data', {}).get('Page', {}).get('media'):
                return await ctx.send("Không tìm thấy dữ liệu!")
            embed = discord.Embed(
                title=f"Top 10 Anime {'('+genre_name+')' if genre_name else ''}",
                color=0xff69b4
            )
            for i, anime in enumerate(data['data']['Page']['media'][:10], 1):
                embed.add_field(
                    name=f"{i}. {anime['title']['romaji']}",
                    value=f"⭐ {anime.get('averageScore', 'N/A')}/100 | 🗓️ {anime['startDate']['year'] or 'N/A'}",
                    inline=False
                )
            embed.set_footer(text="Nguồn: AniList")
            await ctx.send(embed=embed)
    except Exception as e:
        print(f"Lỗi top command: {e}")
        await ctx.send("Đã xảy ra lỗi!")

@bot.command()
async def topyear(ctx):
    """Top anime được yêu thích trong năm"""
    try:
        async with ctx.typing():
            current_year = datetime.datetime.now().year
            gql_query = """
            query ($year: Int, $perPage: Int) {
                Page(perPage: $perPage) {
                    media(type: ANIME, sort: POPULARITY_DESC, seasonYear: $year) {
                        id
                        title { romaji }
                        averageScore
                        startDate { year }
                    }
                }
            }
            """
            variables = {"year": current_year, "perPage": 10}
            data = await anilist.query(gql_query, variables)
            if not data or not data.get('data', {}).get('Page', {}).get('media'):
                return await ctx.send("Không tìm thấy dữ liệu!")
            embed = discord.Embed(title=f"Top 10 Anime Năm {current_year}", color=0x1e90ff)
            for i, anime in enumerate(data['data']['Page']['media'][:10], 1):
                embed.add_field(
                    name=f"{i}. {anime['title']['romaji']}",
                    value=f"⭐ {anime.get('averageScore', 'N/A')}/100",
                    inline=False
                )
            embed.set_footer(text="Nguồn: AniList")
            await ctx.send(embed=embed)
    except Exception as e:
        print(f"Lỗi topyear command: {e}")
        await ctx.send("Đã xảy ra lỗi!")

@bot.command()
async def topwaifu(ctx):
    """Top 10 waifu được yêu thích"""
    try:
        async with ctx.typing():
            data = await anilist.get_top_characters(limit=50)
            if not data or not data.get('data', {}).get('Page', {}).get('characters'):
                return await ctx.send("Không tìm thấy dữ liệu!")
            embed = discord.Embed(title="Top 10 Waifu Được Yêu Thích", color=discord.Color.pink())
            female_characters = [c for c in data['data']['Page']['characters'] if is_female_character(c)]
            count = 0
            for character in female_characters[:10]:
                count += 1
                embed.add_field(
                    name=f"{count}. {character['name']['full']}",
                    value=f"📜 {character.get('description', 'N/A')[:50]}...",
                    inline=False
                )
            if count == 0:
                await ctx.send("Không tìm thấy nhân vật nữ nào!")
            else:
                embed.set_footer(text="Nguồn: AniList")
                await ctx.send(embed=embed)
    except Exception as e:
        print(f"Lỗi topwaifu command: {e}")
        await ctx.send("Đã xảy ra lỗi!")

@bot.command()
async def vote(ctx, *, waifu):
    """Vote cho waifu yêu thích"""
    try:
        conn = sqlite3.connect('waifu.db')
        conn.execute('INSERT INTO votes (user_id, waifu) VALUES (?, ?)', (str(ctx.author.id), waifu))
        conn.commit()
        conn.close()
        await ctx.send(f"Đã vote cho **{waifu}**! Dùng `{PREFIX}topvote` để xem kết quả.")
    except Exception as e:
        print(f"Lỗi vote command: {e}")
        await ctx.send("Đã xảy ra lỗi khi vote!")

@bot.command()
async def topvote(ctx):
    """Xem top waifu được vote trong server"""
    try:
        conn = sqlite3.connect('waifu.db')
        cursor = conn.execute('SELECT waifu, COUNT(*) as count FROM votes GROUP BY waifu ORDER BY count DESC LIMIT 5')
        embed = discord.Embed(title="Top 5 Waifu (Server)", color=discord.Color.pink())
        count = 0
        for i, (waifu, vote_count) in enumerate(cursor, 1):
            count += 1
            embed.add_field(name=f"{i}. {waifu}", value=f"{vote_count} votes", inline=False)
        conn.close()
        if count == 0:
            await ctx.send(f"Chưa có vote nào! Dùng `{PREFIX}vote <tên_waifu>` để bắt đầu.")
        else:
            embed.set_footer(text="Nguồn: Server")
            await ctx.send(embed=embed)
    except Exception as e:
        print(f"Lỗi topvote command: {e}")
        await ctx.send("Đã xảy ra lỗi khi xem top vote!")

@bot.command()
async def checknew(ctx):
    """Kiểm tra anime ra mắt hôm nay"""
    try:
        async with ctx.typing():
            today = datetime.datetime.now()
            new_anime = []
            anilist_data = await anilist.get_new_releases_today()
            if anilist_data and anilist_data.get('data', {}).get('Page', {}).get('media'):
                for anime in anilist_data['data']['Page']['media']:
                    start_date = anime.get('startDate', {})
                    if (start_date.get('year') and start_date.get('month') and start_date.get('day') and
                        start_date.get('year') == today.year and
                        start_date.get('month') == today.month and
                        start_date.get('day') == today.day):
                        new_anime.append({
                            "title": anime['title']['romaji'],
                            "url": anime['siteUrl'],
                            "source": "AniList"
                        })
            if not new_anime:
                jikan_data = await jikan.get_new_releases_today()
                if jikan_data and jikan_data.get('data'):
                    for anime in jikan_data['data']:
                        new_anime.append({
                            "title": anime['title'],
                            "url": anime['url'],
                            "source": "Jikan (MyAnimeList)"
                        })
            if not new_anime:
                await ctx.send(f"Không có anime ra mắt hôm nay ({today.day}/{today.month}/{today.year})!")
            else:
                embed = discord.Embed(
                    title=f"Anime Ra Mắt Hôm Nay ({today.day}/{today.month}/{today.year})",
                    color=0x00ff00
                )
                for i, anime in enumerate(new_anime[:10], 1):
                    embed.add_field(
                        name=f"{i}. {anime['title']} ({anime['source']})",
                        value=f"[Xem chi tiết]({anime['url']})",
                        inline=False
                    )
                embed.set_footer(text="Nguồn: AniList & Jikan")
                await ctx.send(embed=embed)
    except Exception as e:
        print(f"Lỗi checknew command: {e}")
        await ctx.send("Đã xảy ra lỗi khi kiểm tra anime mới!")

@bot.command()
@commands.has_permissions(administrator=True)
async def autoanime(ctx, channel: discord.TextChannel = None):
    """Bật/tắt thông báo anime mới"""
    if channel:
        anime_notification_channels.add(channel.id)
        if not check_new_anime.is_running():
            check_new_anime.start()
        await ctx.send(f"✅ Đã bật thông báo anime mới tại {channel.mention}")
    else:
        if ctx.channel.id in anime_notification_channels:
            anime_notification_channels.remove(ctx.channel.id)
            await ctx.send("❌ Đã tắt thông báo anime mới")
        else:
            await ctx.send(f"⚠️ Vui lòng chỉ định channel (vd: `{PREFIX}autoanime #channel`)")

@bot.command()
@commands.has_permissions(administrator=True)
async def autowaifu(ctx, channel: discord.TextChannel = None):
    """Bật/tắt thông báo waifu mới"""
    if channel:
        waifu_notification_channels.add(channel.id)
        if not check_new_waifu.is_running():
            check_new_waifu.start()
        await ctx.send(f"✅ Đã bật thông báo waifu mới tại {channel.mention}")
    else:
        if ctx.channel.id in waifu_notification_channels:
            waifu_notification_channels.remove(ctx.channel.id)
            await ctx.send("❌ Đã tắt thông báo waifu mới")
        else:
            await ctx.send(f"⚠️ Vui lòng chỉ định channel (vd: `{PREFIX}autowaifu #channel`)")

@bot.command()
@commands.has_permissions(administrator=True)
async def autoairing(ctx, channel: discord.TextChannel = None):
    """Bật/tắt thông báo anime chiếu hôm nay"""
    if channel:
        airing_notification_channels.add(channel.id)
        if not check_airing_today.is_running():
            check_airing_today.start()
        await ctx.send(f"✅ Đã bật thông báo anime chiếu hôm nay tại {channel.mention}")
    else:
        if ctx.channel.id in airing_notification_channels:
            airing_notification_channels.remove(ctx.channel.id)
            await ctx.send("❌ Đã tắt thông báo anime chiếu hôm nay")
        else:
            await ctx.send(f"⚠️ Vui lòng chỉ định channel (vd: `{PREFIX}autoairing #channel`)")

@bot.command()
@commands.has_permissions(administrator=True)
async def autowaifupic(ctx, channel: discord.TextChannel = None):
    """Bật/tắt gửi ảnh waifu tự động mỗi 10 phút"""
    if channel:
        waifu_pic_channels.add(channel.id)
        if not send_waifu_pic.is_running():
            send_waifu_pic.start()
        await ctx.send(f"✅ Đã bật gửi ảnh waifu tự động tại {channel.mention}")
    else:
        if ctx.channel.id in waifu_pic_channels:
            waifu_pic_channels.remove(ctx.channel.id)
            await ctx.send("❌ Đã tắt gửi ảnh waifu tự động")
        else:
            await ctx.send(f"⚠️ Vui lòng chỉ định channel (vd: `{PREFIX}autowaifupic #channel`)")

@bot.command()
@commands.has_permissions(administrator=True)
async def autoranking(ctx, channel: discord.TextChannel = None, genre: str = None):
    """Bật/tắt thông báo bảng xếp hạng anime khi có thay đổi (có thể chọn thể loại)"""
    if channel:
        if genre:
            genre = genre.lower()
            if genre not in GENRE_LIST:
                return await ctx.send(f"Thể loại '{genre}' không hợp lệ! Các thể loại: {', '.join(GENRE_LIST)}")
        ranking_notification_channels[channel.id] = genre
        if not check_ranking_update.is_running():
            check_ranking_update.start()
        await ctx.send(f"✅ Đã bật thông báo bảng xếp hạng {'('+genre+')' if genre else ''} tại {channel.mention}")
    else:
        if ctx.channel.id in ranking_notification_channels:
            del ranking_notification_channels[ctx.channel.id]
            await ctx.send("❌ Đã tắt thông báo bảng xếp hạng")
        else:
            await ctx.send(f"⚠️ Vui lòng chỉ định channel (vd: `{PREFIX}autoranking #channel [thể loại]`)")

@bot.command(name='waifu')
async def random_waifu(ctx, nsfw: str = "false"):
    """Lấy ảnh waifu ngẫu nhiên"""
    if nsfw.lower() not in ["eeeee", "false"]:
        return await ctx.send("Vui lòng dùng `true` hoặc `false` cho tham số NSFW")
    
    try:
        data = await waifu_api.get_random_waifu(nsfw.lower() == "eeeee")
        if not data or 'images' not in data:
            return await ctx.send("Không tìm thấy waifu nào 😢")
        
        embed = discord.Embed(color=0xff9ff3)
        embed.set_image(url=data['images'][0]['url'])
        embed.set_footer(text=f"Nguồn: Veloria Sever")
        
        await ctx.send(embed=embed)
    except Exception as e:
        print(f"Lỗi waifu command: {e}")
        await ctx.send(f"Lỗi: {str(e)}")

@bot.command(name='topwaifus')
async def top_waifus(ctx, limit: int = 10):
    """Top waifu phổ biến nhất"""
    if limit > 20:
        return await ctx.send("Tối đa 20 waifu thôi nhé!")
    
    try:
        anilist_data = await anilist.get_top_characters(limit=50)
        if not anilist_data or not anilist_data.get('data', {}).get('Page', {}).get('characters'):
            return await ctx.send("Đang cập nhật dữ liệu...")
        
        female_characters = [c for c in anilist_data['data']['Page']['characters'] if is_female_character(c)]
        if not female_characters:
            return await ctx.send("Không tìm thấy waifu nào!")
        
        waifu_images = await waifu_api.get_popular_waifus(limit=limit)
        if not waifu_images:
            return await ctx.send("Không lấy được ảnh từ Waifu.im!")
        
        waifus = []
        for idx, character in enumerate(female_characters[:limit], 1):
            anime_title = character['media']['nodes'][0]['title']['romaji'] if character['media']['nodes'] else "Không rõ"
            image = waifu_images[idx-1]['url'] if idx-1 < len(waifu_images) else (character['image']['large'] if character['image'] else None)
            waifus.append({
                "rank": idx,
                "name": character['name']['full'],
                "anime": anime_title,
                "image": image
            })
        
        embed = discord.Embed(
            title=f"🏆 Top {limit} Waifu Phổ Biến Nhất",
            color=0xfeca57
        )
        
        for waifu in waifus:
            embed.add_field(
                name=f"{waifu['rank']}. {waifu['name']}",
                value=f"Anime: {waifu['anime']}",
                inline=False
            )
        
        if waifus and waifus[0]['image']:
            embed.set_thumbnail(url=waifus[0]['image'])
        embed.set_footer(text="Nguồn: AniList & Veloria Sever")
        await ctx.send(embed=embed)
        
    except Exception as e:
        print(f"Lỗi topwaifus command: {e}")
        await ctx.send(f"Lỗi: {str(e)}")

# Helper Functions
async def search_media(ctx, media_type, query):
    try:
        async with ctx.typing():
            data = await anilist.search_media(media_type, query)
            if not data or not data.get('data', {}).get('Media'):
                return await ctx.send(f"Không tìm thấy {media_type}!")
            media = data['data']['Media']
            embed = create_embed(media, media_type)
            await ctx.send(embed=embed)
    except Exception as e:
        print(f"Lỗi {media_type} command: {e}")
        await ctx.send(f"Đã xảy ra lỗi khi tìm {media_type}!")

def create_embed(media, media_type):
    title = media['title']['romaji'] or media['title']['english']
    embed = discord.Embed(
        title=title,
        description=media.get('description', 'Không có mô tả')[:200] + '...',
        color=0x00ff00 if media_type == 'anime' else 0x0000ff,
        url=media['siteUrl']
    )
    if media.get('coverImage'):
        embed.set_image(url=media['coverImage']['large'])
    start_date = "N/A"
    if media.get('startDate') and media['startDate'].get('year'):
        start_date = f"{media['startDate']['year']}-{media['startDate']['month'] or '?'}-{media['startDate']['day'] or '?'}"
    end_date = "N/A"
    if media.get('endDate') and media['endDate'].get('year'):
        end_date = f"{media['endDate']['year']}-{media['endDate']['month'] or '?'}-{media['endDate']['day'] or '?'}"
    fields = [
        ("Rating", media.get('averageScore', 'N/A'), True),
        ("Status", media.get('status', 'N/A'), True),
        ("Start Date", start_date, True),
        ("End Date", end_date, True),
        ("Episodes" if media_type == 'anime' else "Chapters",
         str(media.get('episodes' if media_type == 'anime' else 'chapters', 'N/A')), True)
    ]
    for name, value, inline in fields:
        embed.add_field(name=name, value=value, inline=inline)
    embed.set_footer(text="Nguồn: AniList")
    return embed

def create_character_embed(character):
    embed = discord.Embed(
        title=character['name']['full'],
        description=character.get('description', 'Không có mô tả')[:200] + "...",
        color=discord.Color.pink(),
        url=character['siteUrl']
    )
    if character.get('image'):
        embed.set_image(url=character['image']['large'])
    embed.set_footer(text="Nguồn: AniList")
    return embed

# Events
@bot.event
async def on_ready():
    print(f'Bot {bot.user.name} đã sẵn sàng!')
    init_db()
    if airing_notification_channels:
        if not check_airing_today.is_running():
            check_airing_today.start()
    if waifu_pic_channels:
        if not send_waifu_pic.is_running():
            send_waifu_pic.start()
    if ranking_notification_channels:
        if not check_ranking_update.is_running():
            check_ranking_update.start()

@bot.event
async def on_command_error(ctx, error):
    if isinstance(error, commands.CommandNotFound):
        await ctx.send(f"Lệnh không tồn tại! Dùng `{PREFIX}help` để xem danh sách lệnh")
    elif isinstance(error, commands.MissingPermissions):
        await ctx.send("Bạn không có quyền sử dụng lệnh này!")
    else:
        print(f"[ERROR] {type(error)}: {error}")
        await ctx.send("Đã xảy ra lỗi!")

@bot.event
async def on_command_completion(ctx):
    if random.random() < 0.3:
        await ctx.send(random.choice(RESPONSES))

# Main
async def main():
    async with bot:
        await bot.start(TOKEN)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("Đang tắt bot...")
    finally:
        asyncio.run(anilist.close())
        asyncio.run(jikan.close())
        asyncio.run(waifu_api.close())