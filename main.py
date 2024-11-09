import os
import json
import time
import logging
import threading
import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta
from datetime import time as datetime_time  # 為了避免與 time 模組衝突
from typing import Dict, List, Optional

import pytz
import aiohttp
import requests
import firebase_admin
from firebase_admin import credentials, db
import discord
from discord.ext import commands
import functions_framework
from flask import Flask

app = Flask(__name__)

# 設定 Discord Bot
DISCORD_TOKEN = os.getenv('DISCORD_TOKEN')
GROQ_API_KEY = os.getenv('GROQ_API_KEY')
OPENWEATHER_API_KEY = os.getenv('OPENWEATHER_API_KEY')


# 初始化 Discord bot
intents = discord.Intents.default()
intents.message_content = True  # 啟用消息內容權限
intents.guilds = True          # 啟用伺服器權限
intents.guild_messages = True  # 啟用伺服器消息權限
intents.dm_messages = True     # 啟用私信權限

bot = commands.Bot(command_prefix='!', intents=intents)

# 初始化 Firebase Admin SDK
try:
    cred = credentials.Certificate("serviceAccountKey.json")
    firebase_admin.initialize_app(cred, {
        "databaseURL": "https://dcbot1-b2100-default-rtdb.firebaseio.com/"
    })
except Exception as e:
    logging.error(f"Firebase initialization error: {e}")

GROQ_API_URL = "https://api.groq.com/openai/v1/chat/completions"

# 初始化日誌設定
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)

@dataclass
class WeatherData:
    """天氣數據結構"""
    location: str
    temperature: float
    feels_like: float
    humidity: int
    description: str
    timestamp: datetime
    
    def to_dict(self) -> dict:
        """轉換為字典格式以存儲到 Firebase"""
        return {
            "location": self.location,
            "temperature": self.temperature,
            "feels_like": self.feels_like,
            "humidity": self.humidity,
            "description": self.description,
            "timestamp": self.timestamp.strftime("%Y-%m-%d %H:%M:%S")
        }
    
    @classmethod
    def from_dict(cls, data: dict) -> 'WeatherData':
        """從字典格式轉換回物件"""
        return cls(
            location=data["location"],
            temperature=data["temperature"],
            feels_like=data["feels_like"],
            humidity=data["humidity"],
            description=data["description"],
            timestamp=datetime.strptime(data["timestamp"], "%Y-%m-%d %H:%M:%S")
        )
    
    def format_message(self) -> str:
        """格式化天氣信息"""
        return (
            f"📍 地點：{self.location}\n"
            f"🌡️ 溫度：{self.temperature}°C\n"
            f"🌡️ 體感溫度：{self.feels_like}°C\n"
            f"💧 濕度：{self.humidity}%\n"
            f"🌥️ 天氣狀況：{self.description}"
        )

class WeatherService:
    def __init__(self, api_key: str, city_id: str = "1668341"):
        self.api_key = api_key
        self.city_id = city_id
        self.api_url = "https://api.openweathermap.org/data/2.5/weather"
        self.cached_data: Optional[WeatherData] = None
        self.cache_time: Optional[float] = None
        self.cache_duration = 1800  # 30分鐘緩存
        self.subscribers: Dict[str, bool] = {}
        self._load_subscribers()

    async def get_weather(self) -> WeatherData:
        """獲取天氣數據（優先使用緩存，但考慮過期時間）"""
        current_time = time.time()
        if (self.cached_data is None or 
            self.cache_time is None or 
            current_time - self.cache_time > self.cache_duration):
            self.cached_data = await self.fetch_weather()
            self.cache_time = current_time
        return self.cached_data
    
    def _load_subscribers(self):
        """從 Firebase 加載訂閱者"""
        try:
            ref = db.reference("weather_subscribers")
            data = ref.get()
            if data:
                self.subscribers = data
        except Exception as e:
            logging.error(f"Failed to load weather subscribers: {e}")
    
    def save_subscribers(self):
        """保存訂閱者到 Firebase"""
        try:
            ref = db.reference("weather_subscribers")
            ref.set(self.subscribers)
        except Exception as e:
            logging.error(f"Failed to save weather subscribers: {e}")
    
    async def fetch_weather(self) -> WeatherData:
        """從 OpenWeather API 獲取天氣數據"""
        max_retries = 3
        retry_delay = 1  # 初始延遲1秒
        
        for attempt in range(max_retries):
            try:
                async with aiohttp.ClientSession() as session:
                    params = {
                        "id": self.city_id,
                        "appid": self.api_key,
                        "units": "metric",
                        "lang": "zh_tw"
                    }
                    
                    async with session.get(self.api_url, params=params) as response:
                        if response.status == 200:
                            data = await response.json()
                            weather_data = WeatherData(
                                location="臺北市",
                                temperature=round(data["main"]["temp"], 1),
                                feels_like=round(data["main"]["feels_like"], 1),
                                humidity=data["main"]["humidity"],
                                description=data["weather"][0]["description"],
                                timestamp=datetime.now()
                            )
                            
                            # 緩存數據
                            self.cached_data = weather_data
                            
                            # 保存到 Firebase
                            try:
                                ref = db.reference("weather_data")
                                ref.set(weather_data.to_dict())
                            except Exception as e:
                                logging.error(f"Failed to save weather data: {e}")
                            
                            return weather_data
                        else:
                            raise Exception(f"Weather API error: {response.status}")
            except Exception as e:
                if attempt == max_retries - 1:  # 最後一次嘗試
                    raise
                logging.warning(f"Attempt {attempt + 1} failed: {str(e)}, retrying...")
                await asyncio.sleep(retry_delay * (2 ** attempt))  # 指數退避

    def subscribe(self, user_id: str):
        """訂閱天氣推播"""
        self.subscribers[user_id] = True
        self.save_subscribers()
        logging.info(f"User {user_id} subscribed to weather updates")

    def unsubscribe(self, user_id: str):
        """取消訂閱天氣推播"""
        if user_id in self.subscribers:
            del self.subscribers[user_id]
            self.save_subscribers()
            logging.info(f"User {user_id} unsubscribed from weather updates")
    

class TimeContext:
    def __init__(self):
        self.tz = pytz.timezone('Asia/Taipei')
    
    def get_current_time(self) -> datetime:
        """獲取當前台北時間"""
        # 使用 utc 時間然後轉換到當地時區，這是處理時區的正確方式
        utc_now = pytz.utc.localize(datetime.utcnow())
        return utc_now.astimezone(self.tz)
    
    def get_greeting(self) -> str:
        """返回簡單的時間相關問候語，不帶具體時間"""
        current_time = self.get_current_time()
        hour = current_time.hour
        
        if 5 <= hour < 11:
            return "早安！"
        elif 11 <= hour < 13:
            return "午安！"
        elif 13 <= hour < 18:
            return "下午好！"
        elif 18 <= hour < 22:
            return "晚安！"
        else:
            return "夜安！"
    
    def get_detailed_context(self) -> str:
        """只在直接詢問時間時使用"""
        current_time = self.get_current_time()
        weekday_mapping = {
            0: '一',
            1: '二',
            2: '三',
            3: '四',
            4: '五',
            5: '六',
            6: '日'
        }
        weekday = weekday_mapping[current_time.weekday()]
        
        return (
            f"現在是 {current_time.strftime('%m')}月{current_time.strftime('%d')}號"
            f" 星期{weekday}"
            f" {current_time.strftime('%H:%M')}"
        )
    
    def get_formatted_time(self) -> str:
        """獲取格式化的時間字符串，用於存儲"""
        current_time = self.get_current_time()
        return current_time.strftime("%Y-%m-%d %H:%M:%S")


class MessageHandler:
    def __init__(self, weather_service: WeatherService):
        self.time_context = TimeContext()
        self._last_time_mention = 0
        self.weather_service = weather_service
        
        # 添加天氣相關關鍵詞
        self.weather_patterns = {
            'general': ['天氣', '天氣如何', '今天天氣'],
            'temperature': ['溫度', '幾度', '熱不熱'],
            'humidity': ['濕度', '溼度', '濕不濕'],
            'feels_like': ['體感', '感覺溫度'],
            'subscribe': ['訂閱天氣', '天氣訂閱'],
            'unsubscribe': ['取消訂閱', '停止天氣推播']
        }
    
    async def handle_weather_query(self, msg: str, user_id: str) -> Optional[str]:
        """處理天氣相關查詢"""
        # 訂閱相關
        if any(keyword in msg for keyword in self.weather_patterns['subscribe']):
            self.weather_service.subscribe(user_id)
            return "已訂閱每日天氣推播！每天早上 6:00 我會告訴你天氣狀況 ⏰"
        
        if any(keyword in msg for keyword in self.weather_patterns['unsubscribe']):
            self.weather_service.unsubscribe(user_id)
            return "已取消天氣推播訂閱。"
        
        try:
            # 天氣查詢
            weather_data = await self.weather_service.get_weather()
            
            # 溫度查詢
            if any(keyword in msg for keyword in self.weather_patterns['temperature']):
                return f"🌡️ 現在溫度是 {weather_data.temperature}°C"
            
            # 濕度查詢
            if any(keyword in msg for keyword in self.weather_patterns['humidity']):
                return f"💧 現在濕度是 {weather_data.humidity}%"
            
            # 體感溫度查詢
            if any(keyword in msg for keyword in self.weather_patterns['feels_like']):
                return f"🌡️ 現在體感溫度是 {weather_data.feels_like}°C"
            
            # 一般天氣查詢
            if any(keyword in msg for keyword in self.weather_patterns['general']):
                return weather_data.format_message()
            
        except Exception as e:
            logging.error(f"Error handling weather query: {e}")
            return "抱歉，獲取天氣信息時發生錯誤。"
        
        return None
    
    async def enhance_message(self, msg: str, user_id: str) -> str:
        """增強消息內容，包含時間和天氣處理"""
        # 檢查是否是天氣相關查詢
        weather_response = await self.handle_weather_query(msg, user_id)
        if weather_response:
            return weather_response
        
        # 原有的時間處理邏輯
        current_time = time.time()
        
        patterns = {
            'high_priority': ['幾點', '現在時間', '日期', '幾號'],
            'low_priority': ['早', '午', '晚', 'hi', 'hello', '你好', '哈囉']
        }
        
        if any(keyword in msg for keyword in patterns['high_priority']):
            return f"{self.time_context.get_detailed_context()}\n{msg}"
            
        if any(keyword in msg.lower() for keyword in patterns['low_priority']):
            if current_time - self._last_time_mention > 1800:
                self._last_time_mention = current_time
                greeting = self.time_context.get_greeting()
                return f"{greeting} {msg}"
            else:
                return f"你好！{msg}"
                
        return msg
    
    async def enhance_message_with_time_context(self, msg: str, user_id: str) -> str:
        """保持原有方法名稱的兼容性"""
        return await self.enhance_message(msg, user_id)

# 添加 WeatherScheduler 類
class WeatherScheduler:
    def __init__(self, bot: commands.Bot, weather_service: WeatherService):
        self.bot = bot
        self.weather_service = weather_service
        self.broadcast_time = datetime_time(hour=6, minute=0)  # 使用 datetime.tim
        self.scheduler_started = False
    
    async def broadcast_weather(self):
        max_retries = 3
        retry_delay = 60  # 1分鐘
        
        for attempt in range(max_retries):
            try:
                weather_data = await self.weather_service.fetch_weather()
                message = (
                    "🌅 早安！這是今天的天氣預報：\n\n" +
                    weather_data.format_message()
                )
                
                for user_id in self.weather_service.subscribers:
                    try:
                        user = await self.bot.fetch_user(int(user_id))
                        await user.send(message)
                    except Exception as e:
                        logging.error(f"Failed to send weather to user {user_id}: {e}")
                break  # 成功後跳出循環
            except Exception as e:
                if attempt == max_retries - 1:
                    logging.error(f"Failed to broadcast weather after {max_retries} attempts: {e}")
                else:
                    logging.warning(f"Broadcast attempt {attempt + 1} failed: {e}, retrying...")
                    await asyncio.sleep(retry_delay)

    async def schedule_weather_broadcast(self):
        """定時廣播天氣信息"""
        while True:
            try:
                now = datetime.now(pytz.timezone('Asia/Taipei'))
                target_time = now.replace(
                    hour=self.broadcast_time.hour,
                    minute=self.broadcast_time.minute,
                    second=0,
                    microsecond=0
                )
                
                if now >= target_time:
                    target_time += timedelta(days=1)
                
                delay = (target_time - now).total_seconds()
                logging.info(f"Next weather broadcast scheduled in {delay} seconds")
                
                await asyncio.sleep(delay)
                await self.broadcast_weather()
                
            except Exception as e:
                logging.error(f"Error in weather scheduler: {e}")
                await asyncio.sleep(60)  # 發生錯誤時等待1分鐘後重試
                
    def start(self):
        """開始天氣廣播排程"""
        if not self.scheduler_started:
            asyncio.create_task(self.schedule_weather_broadcast())
            self.scheduler_started = True



def choose_model_based_on_message(msg: str, fallback_level: int = 0) -> str:
    """根據消息長度和fallback級別選擇合適的模型"""
    model_sequence = [
        "llama-3.2-90b-text-preview",    
        "llama-3.1-70b-versatile",       
        "llama-3.2-11b-text-preview",    
        "llama-3.1-8b-instant"           
    ]
    
    if 0 <= fallback_level < len(model_sequence):
        selected_model = model_sequence[fallback_level]
        logging.info(f"Selected model (fallback level {fallback_level}): {selected_model}")
        return selected_model
    
    logging.warning(f"Fallback level {fallback_level} exceeded available models, using last resort model")
    return model_sequence[-1]

def add_message_to_firebase(user_id: str, user_message: str, bot_reply: str):
    time_context = TimeContext()
    ref = db.reference(f"discord_bot_messages/{user_id}/conversation")
    ref.push({
        "user_message": user_message,
        "bot_reply": bot_reply,
        "timestamp": time_context.get_formatted_time()
    })

def get_conversation_history(user_id: str) -> List[Dict[str, str]]:
    ref = db.reference(f"discord_bot_messages/{user_id}/conversation")
    messages = ref.get()
    history = []
    if messages:
        for msg in messages.values():
            history.append({"role": "user", "content": msg["user_message"]})
            history.append({"role": "assistant", "content": msg["bot_reply"]})
    return history

def clear_conversation_history(user_id: str):
    ref = db.reference(f"discord_bot_messages/{user_id}/conversation")
    ref.delete()

async def get_ai_response(msg: str, user_id: str, conversation_history: List[Dict[str, str]]) -> str:
    """獲取AI回應"""
    fallback_level = 0
    success = False
    reply_msg = ""
    last_error = None
    
    while not success and fallback_level <= 3:
        model_name = choose_model_based_on_message(msg, fallback_level)
        
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {GROQ_API_KEY}"
        }
        
        with open("character_description.txt", "r", encoding="utf-8") as file:
            character_description = file.read()
        
        
        system_prompt = character_description
        
        payload = {
            "model": model_name,
            "messages": [
                {
                    "role": "system",
                    "content": system_prompt
                },
                *conversation_history
            ],
            "max_tokens": 600,
            "temperature": 0.7,
            "presence_penalty": 0.6,
            "frequency_penalty": 0.3
        }

        try:
            response = requests.post(GROQ_API_URL, headers=headers, json=payload)
            response_data = response.json()

            if response.status_code == 200 and 'choices' in response_data:
                reply_msg = response_data['choices'][0]['message']['content'].strip()
                success = True
                logging.info(f"Successfully got response from {model_name}")
            else:
                error_msg = f"Unexpected response from {model_name}: {response_data}"
                logging.error(error_msg)
                last_error = error_msg
                fallback_level += 1

        except requests.exceptions.RequestException as e:
            error_msg = f"Request failed for {model_name}: {str(e)}"
            logging.error(error_msg)
            last_error = error_msg
            fallback_level += 1

    if not success:
        reply_msg = "非常抱歉，兄長大人...我現在似乎無法正常回應。"
        logging.error(f"All models failed. Last error: {last_error}")

    return reply_msg

# 修改 on_ready 事件以啟動天氣排程
@bot.event
async def on_ready():
    logging.info(f'{bot.user} has connected to Discord!')
    weather_scheduler.start()
    logging.info("Weather scheduler started")

# 修正 on_message 事件處理
@bot.event
async def on_message(message):
    if message.author == bot.user:
        return

    await bot.process_commands(message)

    try:
        should_respond = False
        content = message.content
        
        if isinstance(message.channel, discord.DMChannel):
            should_respond = True
            logging.info(f"Received DM: {content}")
        elif bot.user.mentioned_in(message):
            should_respond = True
            content = message.clean_content.replace(f'@{bot.user.display_name}', '').strip()
            logging.info(f"Mentioned in channel: {content}")

        if should_respond:
            if content == "忘掉一切吧":
                clear_conversation_history(str(message.author.id))
                await message.reply("已經忘掉所有過去的對話紀錄。")
                return

            # 使用全局的 weather_service
            message_handler = MessageHandler(weather_service)
            enhanced_msg = await message_handler.enhance_message_with_time_context(content, str(message.author.id))
            
            conversation_history = get_conversation_history(str(message.author.id))
            conversation_history.append({"role": "user", "content": enhanced_msg})

            async with message.channel.typing():
                reply_msg = await get_ai_response(enhanced_msg, str(message.author.id), conversation_history)
            
            await message.reply(reply_msg)
            add_message_to_firebase(str(message.author.id), content, reply_msg)

    except Exception as e:
        logging.error(f"Error processing message: {e}")
        await message.reply("抱歉，處理訊息時發生錯誤。")


# 全局變量追踪
bot_started = False
bot_thread = None
event_loop = None
weather_service = WeatherService(OPENWEATHER_API_KEY)
weather_scheduler = WeatherScheduler(bot, weather_service)
logging.info("Weather service and scheduler initialized")

def run_discord_bot():
    """在背景執行 Discord bot"""
    global bot_started, bot_thread, event_loop
    
    if bot_thread and bot_thread.is_alive():
        return True
        
    def bot_task():
        global event_loop
        try:
            event_loop = asyncio.new_event_loop()
            asyncio.set_event_loop(event_loop)
            event_loop.run_until_complete(bot.start(DISCORD_TOKEN))
        except Exception as e:
            logging.error(f"Error in bot task: {e}")
            
    try:
        bot_thread = threading.Thread(target=bot_task, daemon=True)
        bot_thread.start()
        logging.info("Bot thread started successfully")
        bot_started = True
        return True
    except Exception as e:
        logging.error(f"Failed to start bot thread: {e}")
        return False

@functions_framework.http
def hello_http(request):
    """HTTP Cloud Function 入口點"""
    global bot_started
    
    logging.info(f"Received request: {request.method} from {request.headers.get('User-Agent', 'Unknown')}")
    
    # 確保 bot 在任何請求時都會啟動
    if not bot_started:
        try:
            if run_discord_bot():
                logging.info("Successfully started Discord bot")
            else:
                logging.error("Failed to start Discord bot")
                return "Failed to start bot", 500
        except Exception as e:
            logging.error(f"Error starting bot: {e}")
            return f"Error starting bot: {str(e)}", 500

    # 返回當前狀態
    status = "running" if bot_thread and bot_thread.is_alive() else "not running"
    return f"Discord bot status: {status}", 200