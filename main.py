import discord
from discord.ext import commands
import logging
import requests
import json
import os
import firebase_admin
from firebase_admin import credentials, db
from datetime import datetime
import pytz
from typing import Dict, List, Optional
import functions_framework
import threading
import asyncio
from flask import Flask

app = Flask(__name__)

# 設定 Discord Bot
DISCORD_TOKEN = os.getenv('DISCORD_TOKEN')
GROQ_API_KEY = os.getenv('GROQ_API_KEY')

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

class TimeContext:
    def __init__(self):
        self.tz = pytz.timezone('Asia/Taipei')
    
    def get_current_time(self) -> datetime:
        """獲取當前台灣時間"""
        return datetime.now(self.tz)
    
    def get_formatted_time(self) -> str:
        """獲取格式化的時間字符串"""
        return self.get_current_time().strftime("%Y-%m-%d %H:%M:%S")
    
    def get_time_greeting(self) -> str:
        """根據當前實時時間返回準確的問候語"""
        current_time = self.get_current_time()
        hour = current_time.hour
        minute = current_time.minute
        
        if 5 <= hour < 11:
            return f"早安！現在是早上 {hour:02d}:{minute:02d}"
        elif hour == 11:
            return f"快中午了！現在是 {hour:02d}:{minute:02d}"
        elif 12 <= hour < 13:
            return f"中午好！現在是 {hour:02d}:{minute:02d}"
        elif 13 <= hour < 18:
            return f"午安！現在是下午 {hour:02d}:{minute:02d}"
        elif 18 <= hour < 22:
            return f"晚上好！現在是晚上 {hour:02d}:{minute:02d}"
        else:
            return f"夜深了！現在是凌晨 {hour:02d}:{minute:02d}"
    
    def get_detailed_time_context(self) -> str:
        """獲取詳細的時間上下文信息"""
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
        return f"現在是 {current_time.strftime('%Y年%m月%d日')} 星期{weekday} {current_time.strftime('%H:%M:%S')}"

class MessageHandler:
    def __init__(self):
        self.time_context = TimeContext()
        
    def enhance_message_with_time_context(self, msg: str) -> str:
        """根據實時時間增強訊息內容"""
        greeting_keywords = ['hi', 'hello', '你好', '哈囉', '早', '午', '晚']
        time_related_keywords = ['幾點', '現在時間', '時間', '日期', '幾號']
        
        if any(keyword in msg.lower() for keyword in greeting_keywords):
            return f"{self.time_context.get_time_greeting()} {msg}"
        elif any(keyword in msg for keyword in time_related_keywords):
            return f"{self.time_context.get_detailed_time_context()}\n{msg}"
        return msg

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
        
        time_context = TimeContext()
        system_prompt = f"{character_description}\n{time_context.get_detailed_time_context()}"
        
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

@bot.event
async def on_ready():
    logging.info(f'{bot.user} has connected to Discord!')

@bot.event
async def on_message(message):
    # 忽略機器人自己的消息
    if message.author == bot.user:
        return

    # 處理命令
    await bot.process_commands(message)

    try:
        should_respond = False
        content = message.content
        
        # 檢查是否為私訊
        if isinstance(message.channel, discord.DMChannel):
            should_respond = True
            logging.info(f"Received DM: {content}")
        # 檢查是否有提及機器人
        elif bot.user.mentioned_in(message):
            should_respond = True
            # 移除提及並獲取實際消息內容
            content = message.clean_content.replace(f'@{bot.user.display_name}', '').strip()
            logging.info(f"Mentioned in channel: {content}")

        if should_respond:
            message_handler = MessageHandler()
            
            if content == "忘掉一切吧":
                clear_conversation_history(str(message.author.id))
                await message.reply("已經忘掉所有過去的對話紀錄。")
                return

            # 增加時間上下文
            enhanced_msg = message_handler.enhance_message_with_time_context(content)
            
            # 獲取對話歷史
            conversation_history = get_conversation_history(str(message.author.id))
            conversation_history.append({"role": "user", "content": enhanced_msg})

            async with message.channel.typing():
                reply_msg = await get_ai_response(enhanced_msg, str(message.author.id), conversation_history)
            
            await message.reply(reply_msg)
            
            # 保存對話記錄
            add_message_to_firebase(str(message.author.id), content, reply_msg)

    except Exception as e:
        logging.error(f"Error processing message: {e}")
        await message.reply("抱歉，處理訊息時發生錯誤。")

# 全局變量追踪
bot_started = False
bot_thread = None
event_loop = None

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