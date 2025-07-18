# bot.py - यह अब स्टैंडअलोन चलेगा

import os
import logging
import asyncio
import random
from datetime import datetime, timedelta
from threading import Thread # Threading के लिए इम्पोर्ट करें

from pyrogram import Client, filters, idle
from pyrogram.types import Message, CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup

# PollAnswer के लिए कॉम्पैटिबिलिटी फिक्स
try:
    from pyrogram.types import PollAnswer
except ImportError:
    # यदि Pyrogram का पुराना वर्ज़न है जहाँ PollAnswer सीधे import नहीं होता
    logger.warning("PollAnswer could not be imported directly from pyrogram.types. Using a fallback class. Please consider updating Pyrogram for full functionality.")
    class PollAnswer:
        def __init__(self, **kwargs):
            self.user = kwargs.get('user')
            self.poll_id = kwargs.get('poll_id')
            self.option_ids = kwargs.get('option_ids', [])
        
        # Pyrogram के PollAnswer ऑब्जेक्ट के कुछ आवश्यक गुण और तरीके जोड़ें
        @property
        def from_user(self):
            return self.user

from pymongo import MongoClient

# --- Configuration & Global Variables ---
API_ID = int(os.getenv("API_ID"))
API_HASH = os.getenv("API_HASH")
BOT_TOKEN = os.getenv("BOT_TOKEN")
MONGO_URI = os.getenv("MONGO_URI")
ADMIN_USER_ID = int(os.getenv("ADMIN_USER_ID", 0))
CONTENT_CHANNEL_ID = int(os.getenv("CONTENT_CHANNEL_ID", 0))
LOG_CHANNEL_ID = int(os.getenv("LOG_CHANNEL_ID", 0))

DB_NAME = "telegram_games_db"

# Logging setup
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

# Pyrogram Client Initialization
if not all([API_ID, API_HASH, BOT_TOKEN, MONGO_URI]):
    logger.critical("CRITICAL ERROR: Missing one or more required environment variables (API_ID, API_HASH, BOT_TOKEN, MONGO_URI). Bot will not start.")
    exit(1) # Exit if critical variables are missing

app = Client(
    "game_bot_session", # Session name
    api_id=API_ID,
    api_hash=API_HASH,
    bot_token=BOT_TOKEN
)

# Flask App Initialization (for health checks)
from flask import Flask, jsonify
flask_app_instance = Flask(__name__)

@flask_app_instance.route('/')
def home():
    return "Hello! Telegram Bot and Flask server are running (Pyrogram on 1 file)."

@flask_app_instance.route('/healthz')
def health_check():
    # Pyrogram client connected status checking for healthz can be complex
    # A simple approach for a basic health check:
    is_bot_running = app.is_connected
    return jsonify({"status": "healthy", "bot_running": is_bot_running}), 200

# MongoDB client initialization
mongo_client = None
db = None
users_collection = None
groups_collection = None
game_states_collection = None
channel_content_cache_collection = None

def init_mongo_collections():
    global mongo_client, db, users_collection, groups_collection, game_states_collection, channel_content_cache_collection
    try:
        logger.info("Attempting to connect to MongoDB...")
        mongo_client = MongoClient(MONGO_URI)
        db = mongo_client[DB_NAME]
        
        # Test connection
        mongo_client.admin.command('ping') 
        logger.info("MongoDB connected successfully within bot.py.")

        users_collection = db["users"]
        groups_collection = db["groups"]
        game_states_collection = db["game_states"]
        channel_content_cache_collection = db["channel_content_cache"]
        
        # TTL index for users to remove old data (e.g., after 1 year)
        users_collection.create_index([("last_updated", 1)], expireAfterSeconds=365 * 24 * 60 * 60) 
        logger.info("TTL index on 'users' collection created/updated.")
    except Exception as e:
        logger.critical(f"Failed to connect to MongoDB or set up indexes: {e}")
        # Re-raise the exception to be caught by the calling function
        raise 

# Game Constants
GAME_QUIZ = "Quiz / Trivia"
GAME_WORDCHAIN = "Shabd Shrinkhala"
GAME_GUESSING = "Andaaz Lagaao"
GAME_NUMBER_GUESSING = "Sankhya Anuamaan"

GAMES_LIST = [
    (GAME_QUIZ, "quiz"),
    (GAME_WORDCHAIN, "wordchain"),
    (GAME_GUESSING, "guessing"),
    (GAME_NUMBER_GUESSING, "number_guessing")
]

active_games = {} # Stores current game state for each chat_id

# --- Helper Functions (Game Logic) ---

async def get_channel_content(game_type: str):
    logger.info(f"Fetching content for game_type: {game_type}")
    if channel_content_cache_collection is None:
        logger.error("channel_content_cache_collection not initialized.")
        return []
    try:
        content = list(channel_content_cache_collection.find({"game_type": game_type}))
        if not content:
            logger.warning(f"No content found for game type: {game_type} in DB. Please add content to 'channel_content_cache' collection.")
        return content
    except Exception as e:
        logger.error(f"Error fetching content from DB for {game_type}: {e}")
        return []

async def update_user_score(user_id: int, username: str, group_id: int, points: int):
    if users_collection is None:
        logger.error("users_collection not initialized. Cannot update user score.")
        return
    try:
        users_collection.update_one(
            {"user_id": user_id},
            {"$inc": {"total_score": points, f"group_scores.{group_id}": points},
             "$set": {"username": username, "last_updated": datetime.utcnow()}},
            upsert=True
        )
        logger.info(f"User {username} ({user_id}) scored {points} points in group {group_id}.")
    except Exception as e:
        logger.error(f"Error updating user score for {username}: {e}")

async def get_leaderboard(group_id: int = None):
    if users_collection is None:
        logger.error("users_collection not initialized. Cannot get leaderboard.")
        return []
    try:
        if group_id:
            return list(users_collection.find().sort(f"group_scores.{group_id}", -1).limit(10))
        else:
            return list(users_collection.find().sort("total_score", -1).limit(10))
    except Exception as e:
        logger.error(f"Error getting leaderboard: {e}")
        return []

async def is_admin(chat_id: int, user_id: int, client: Client):
    try:
        chat_member = await client.get_chat_member(chat_id, user_id)
        return chat_member.status in ["administrator", "creator"]
    except Exception as e:
        logger.error(f"Error checking admin status for chat {chat_id}, user {user_id}: {e}")
        return False

async def save_game_state_to_db(chat_id: int):
    if game_states_collection is None:
        logger.error("game_states_collection not initialized. Cannot save game state.")
        return
    if chat_id in active_games:
        try:
            temp_game_state = active_games[chat_id].copy()
            if "timer_task" in temp_game_state and isinstance(temp_game_state["timer_task"], asyncio.Task):
                del temp_game_state["timer_task"] # Task objects cannot be serialized
            
            # Ensure any other non-serializable objects are removed/converted
            # e.g., Pyrogram Message objects or client objects should not be saved

            game_states_collection.update_one(
                {"_id": chat_id},
                {"$set": temp_game_state},
                upsert=True
            )
            logger.info(f"Game state saved for group {chat_id}.")
        except Exception as e:
            logger.error(f"Error saving game state for group {chat_id}: {e}")

async def load_game_state_from_db():
    global active_games
    if game_states_collection is None:
        logger.error("game_states_collection not initialized. Cannot load game state.")
        return
    try:
        active_games = {doc["_id"]: doc for doc in game_states_collection.find()}
        logger.info(f"Loaded {len(active_games)} active games from DB. Timer tasks will be re-initialized after bot start.")
    except Exception as e:
        logger.error(f"Error loading game states from DB: {e}")

async def auto_end_game_timer(chat_id: int, client: Client):
    while chat_id in active_games and active_games[chat_id]["status"] == "in_progress":
        game_state = active_games.get(chat_id)
        if not game_state:
            break

        last_activity = game_state.get("last_activity_time", datetime.utcnow())
        time_since_last_activity = (datetime.utcnow() - last_activity).total_seconds()

        if time_since_last_activity >= 300: # 5 minutes
            await client.send_message(chat_id=chat_id, text="Game mein 5 minute se koi activity nahi. Game khatam kar diya gaya.")
            del active_games[chat_id]
            if game_states_collection:
                game_states_collection.delete_one({"_id": chat_id})
            logger.info(f"Game auto-ended due to inactivity in group {chat_id}.")
            break
        
        await asyncio.sleep(30)
    logger.info(f"Auto-end timer for group {chat_id} stopped.")

quiz_questions_cache = {}

async def start_game_countdown(chat_id: int, game_type_code: str, message_to_edit: Message, client: Client):
    await asyncio.sleep(60)
    
    if chat_id in active_games and active_games[chat_id]["status"] == "waiting_for_players":
        active_games[chat_id]["status"] = "in_progress"
        await save_game_state_to_db(chat_id)

        players_count = len(active_games[chat_id]["players"])
        if players_count == 0:
            await message_to_edit.edit_text("Koi khiladi nahi juda. Game cancel kar diya gaya.")
            del active_games[chat_id]
            if game_states_collection:
                game_states_collection.delete_one({"_id": chat_id})
            logger.info(f"Game cancelled in group {chat_id}: no players.")
            return

        await message_to_edit.edit_text(f"**{next((name for name, code in GAMES_LIST if code == game_type_code), 'Game')} Shuru!**\n\nKul {players_count} khiladi shamil hain.")
        logger.info(f"Game {game_type_code} started in group {chat_id} with {players_count} players.")
        
        if game_type_code == "quiz":
            await start_quiz_game(chat_id, client)
        elif game_type_code == "wordchain":
            await start_wordchain_game(chat_id, client)
        elif game_type_code == "guessing":
            await start_guessing_game(chat_id, client)
        elif game_type_code == "number_guessing":
            await start_number_guessing_game(chat_id, client)
    
    if chat_id in active_games:
        active_games[chat_id]["timer_task"] = asyncio.create_task(
            auto_end_game_timer(chat_id, client)
        )

async def start_quiz_game(chat_id: int, client: Client):
    questions = await get_channel_content("quiz")
    if not questions:
        await client.send_message(chat_id=chat_id, text="Quiz questions nahi mil paaye. Game abhi shuru nahi ho sakta.")
        del active_games[chat_id]
        if game_states_collection:
            game_states_collection.delete_one({"_id": chat_id})
        logger.warning(f"Quiz game failed to start in {chat_id}: no questions.")
        return
    
    quiz_questions_cache[chat_id] = random.sample(questions, min(len(questions), 10))
    active_games[chat_id]["current_round"] = 0
    active_games[chat_id]["quiz_data"] = quiz_questions_cache[chat_id]
    active_games[chat_id]["current_question"] = {}
    active_games[chat_id]["answered_this_round"] = False
    active_games[chat_id]["last_activity_time"] = datetime.utcnow()
    await save_game_state_to_db(chat_id)

    active_games[chat_id]["timer_task"] = asyncio.create_task(
        send_next_quiz_question_with_timer(chat_id, client)
    )

async def send_next_quiz_question_with_timer(chat_id: int, client: Client):
    while chat_id in active_games and active_games[chat_id]["status"] == "in_progress" and active_games[chat_id]["game_type"] == "quiz":
        game_state = active_games.get(chat_id)
        if not game_state:
            break
        
        if game_state["current_round"] >= len(game_state["quiz_data"]):
            await client.send_message(chat_id=chat_id, text="Quiz khatam! Sabhi sawal pooche ja chuke hain.")
            del active_games[chat_id]
            if game_states_collection:
                game_states_collection.delete_one({"_id": chat_id})
            logger.info(f"Quiz game ended in group {chat_id}: all questions asked.")
            break
        
        question_data = game_state["quiz_data"][game_state["current_round"]]
        
        if "options" in question_data and question_data["options"]:
            message = await client.send_poll(
                chat_id=chat_id,
                question=question_data["text"],
                options=question_data["options"],
                is_anonymous=False,
                type='quiz',
                correct_option_id=question_data["correct_option_id"],
                explanation=question_data.get("explanation", ""),
                open_period=20
            )
            game_state["current_question"] = {
                "type": "poll",
                "message_id": message.id, # Pyrogram uses message.id
                "poll_id": message.poll.id,
                "correct_answer_text": question_data["options"][question_data["correct_option_id"]],
                "correct_option_id": question_data["correct_option_id"]
            }
        else:
            await client.send_message(chat_id=chat_id, text=f"**Sawal {game_state['current_round'] + 1}:**\n\n{question_data['text']}", parse_mode="Markdown")
            game_state["current_question"] = {
                "type": "text",
                "correct_answer": question_data["answer"].lower()
            }
        
        game_state["answered_this_round"] = False
        game_state["last_activity_time"] = datetime.utcnow()
        await save_game_state_to_db(chat_id)

        await asyncio.sleep(20)
        
        if game_state["current_question"].get("type") == "text" and not game_state["answered_this_round"]:
            await client.send_message(chat_id=chat_id, text=f"Samay samapt! Sahi jawab tha: **{game_state['current_question']['correct_answer'].upper()}**")

        game_state["current_round"] += 1
    logger.info(f"Quiz question sending loop ended for group {chat_id}.")

async def handle_quiz_answer_text(message: Message, client: Client):
    chat_id = message.chat.id
    user = message.from_user
    game_state = active_games.get(chat_id)

    if not game_state or game_state["game_type"] != "quiz" or game_state["status"] != "in_progress":
        return
    if user.id not in [p["user_id"] for p in game_state["players"]]:
        return
    if game_state["current_question"].get("type") == "poll":
        return

    if game_state["answered_this_round"]:
        await message.reply_text("Iss sawal ka jawab pehle ही diya ja chuka hai.")
        return

    current_q = game_state["current_question"]
    user_answer = message.text.lower()
    
    if user_answer == current_q["correct_answer"]:
        await message.reply_text(f"Sahi jawab, {user.first_name}! Tumhe 10 points mile.")
        await update_user_score(user.id, user.full_name, chat_id, 10)
        game_state["answered_this_round"] = True
        game_state["last_activity_time"] = datetime.utcnow()
        await save_game_state_to_db(chat_id)
        logger.info(f"Quiz: User {user.full_name} gave correct answer in group {chat_id}.")

async def handle_quiz_poll_answer(poll_answer: PollAnswer, client: Client):
    user_id = poll_answer.user.id
    user_name = poll_answer.user.full_name

    for chat_id, game_state in active_games.items():
        if game_state.get("game_type") == "quiz" and game_state.get("status") == "in_progress":
            current_q = game_state.get("current_question", {})
            if current_q.get("type") == "poll" and current_q.get("poll_id") == poll_answer.poll_id:
                
                if user_id not in [p["user_id"] for p in game_state["players"]]:
                    logger.info(f"Poll answer from non-player {user_name} in group {chat_id}.")
                    return

                if game_state["answered_this_round"]:
                     logger.info(f"Poll: Already answered this round in group {chat_id}.")
                     return
                
                correct_option_id = current_q.get("correct_option_id")
                if correct_option_id is not None and correct_option_id in poll_answer.option_ids:
                    await client.send_message(chat_id=chat_id, text=f"Sahi jawab, {user_name}! Tumhe 10 points mile.")
                    await update_user_score(user_id, user_name, chat_id, 10)
                    game_state["answered_this_round"] = True
                    game_state["last_activity_time"] = datetime.utcnow()
                    await save_game_state_to_db(chat_id)
                    logger.info(f"Quiz Poll: User {user_name} gave correct answer in group {chat_id}.")
                else:
                    await client.send_message(chat_id=chat_id, text=f"Galat jawab, {user_name}!")
                    logger.info(f"Quiz Poll: User {user_name} gave wrong answer in group {chat_id}.")
                break

async def start_wordchain_game(chat_id: int, client: Client):
    words_data = await get_channel_content("wordchain")
    if not words_data:
        await client.send_message(chat_id=chat_id, text="Word Chain words nahi mil paaye. Game abhi shuru nahi ho sakta.")
        del active_games[chat_id]
        if game_states_collection:
            game_states_collection.delete_one({"_id": chat_id})
        logger.warning(f"WordChain game failed to start in {chat_id}: no words.")
        return

    start_word_obj = random.choice(words_data)
    start_word = start_word_obj["question"].strip().lower()

    active_games[chat_id]["current_word"] = start_word
    active_games[chat_id]["turn_index"] = 0
    active_games[chat_id]["last_activity_time"] = datetime.utcnow()
    await save_game_state_to_db(chat_id)
    
    player_count = len(active_games[chat_id]["players"])
    if player_count == 0:
        await client.send_message(chat_id=chat_id, text="Word Chain ke liye khiladi nahi hain. Game band.")
        del active_games[chat_id]
        if game_states_collection:
            game_states_collection.delete_one({"_id": chat_id})
        logger.info(f"Word Chain game ended in {chat_id}: no players after join.")
        return

    random.shuffle(active_games[chat_id]["players"])
    current_player = active_games[chat_id]["players"][active_games[chat_id]["turn_index"]]

    await client.send_message(
        chat_id=chat_id,
        text=f"**Shabd Shrinkhala Shuru!**\n\nPehla shabd: **{start_word.upper()}**\n\n{current_player['username']} ki baari hai. '{start_word[-1].upper()}' se shuru hone wala shabd batao."
    )
    active_games[chat_id]["timer_task"] = asyncio.create_task(
        turn_timer(chat_id, 60, client, "wordchain")
    )
    logger.info(f"WordChain game started in group {chat_id}. First word: {start_word}")

async def handle_wordchain_answer(message: Message, client: Client):
    chat_id = message.chat.id
    user = message.from_user
    game_state = active_games.get(chat_id)

    if not game_state or game_state["game_type"] != "wordchain" or game_state["status"] != "in_progress":
        return
    
    if not game_state["players"]:
        logger.warning(f"WordChain: No players found in game state for {chat_id}.")
        return

    current_player = game_state["players"][game_state["turn_index"]]
    if user.id != current_player["user_id"]:
        await message.reply_text("Abhi tumhari baari nahi hai.")
        return

    user_word = message.text.strip().lower()
    last_char_of_prev_word = game_state["current_word"][-1].lower()

    if user_word.startswith(last_char_of_prev_word) and len(user_word) > 1 and user_word.isalpha():
        await update_user_score(user.id, user.full_name, chat_id, 5)
        await message.reply_text(f"Sahi! '{user_word.upper()}' ab naya shabd hai. {user.first_name} ko 5 points mile.")
        
        game_state["current_word"] = user_word
        game_state["turn_index"] = (game_state["turn_index"] + 1) % len(game_state["players"])
        game_state["last_activity_time"] = datetime.utcnow()
        await save_game_state_to_db(chat_id)

        if game_state.get("timer_task"):
            game_state["timer_task"].cancel()
        
        next_player = game_state["players"][game_state["turn_index"]]
        await client.send_message(
            chat_id=chat_id,
            text=f"{next_player['username']} ki baari. '{user_word[-1].upper()}' se shuru hone wala shabd batao."
        )
        game_state["timer_task"] = asyncio.create_task(turn_timer(chat_id, 60, client, "wordchain"))
        logger.info(f"WordChain: User {user.full_name} gave correct word '{user_word}' in group {chat_id}.")

    else:
        await message.reply_text(f"Galat shabd! '{last_char_of_prev_word.upper()}' se shuru hona chahiye tha ya shabd valid nahi hai. {user.first_name} game se bahar ho gaya.")
        
        game_state["players"] = [p for p in game_state["players"] if p["user_id"] != user.id]
        
        game_state["last_activity_time"] = datetime.utcnow()
        await save_game_state_to_db(chat_id)

        if game_state.get("timer_task"):
            game_state["timer_task"].cancel()

        if len(game_state["players"]) < 2:
            await client.send_message(chat_id=chat_id, text="Khel khatam! Sirf ek khiladi bacha hai ya koi nahi bacha.")
            del active_games[chat_id]
            if game_states_collection:
                game_states_collection.delete_one({"_id": chat_id})
            logger.info(f"WordChain game ended in {chat_id}: not enough players.")
        else:
            if game_state["turn_index"] >= len(game_state["players"]):
                game_state["turn_index"] = 0
            
            next_player = game_state["players"][game_state["turn_index"]]
            await client.send_message(
                chat_id=chat_id,
                text=f"{next_player['username']} ki baari. '{game_state['current_word'][-1].upper()}' se shuru hone wala shabd batao."
            )
            game_state["timer_task"] = asyncio.create_task(turn_timer(chat_id, 60, client, "wordchain"))
            logger.info(f"WordChain: User {user.full_name} failed. Next turn for {next_player['username']} in group {chat_id}.")

async def turn_timer(chat_id: int, duration: int, client: Client, game_type: str):
    await asyncio.sleep(duration)
    
    game_state = active_games.get(chat_id)
    if game_state and game_state["status"] == "in_progress" and game_state["game_type"] == game_type:
        
        if game_type == "wordchain":
            if not game_state["players"]:
                logger.warning(f"WordChain: No players found for timer in group {chat_id}.")
                return

            current_player = game_state["players"][game_state["turn_index"]]
            await client.send_message(chat_id=chat_id, text=f"{current_player['username']} ne jawab nahi diya! Woh game se bahar.")
            
            game_state["players"].pop(game_state["turn_index"]) 
            game_state["last_activity_time"] = datetime.utcnow()
            await save_game_state_to_db(chat_id)

            if len(game_state["players"]) < 2:
                await client.send_message(chat_id=chat_id, text="Khel khatam! Sirf ek khiladi bacha hai ya koi nahi bacha.")
                del active_games[chat_id]
                if game_states_collection:
                    game_states_collection.delete_one({"_id": chat_id})
                logger.info(f"WordChain game ended in {chat_id} due to timeout: not enough players.")
            else:
                if game_state["turn_index"] >= len(game_state["players"]):
                    game_state["turn_index"] = 0
                next_player = game_state["players"][game_state["turn_index"]]
                await client.send_message(
                    chat_id=chat_id,
                    text=f"{next_player['username']} ki baari. '{game_state['current_word'][-1].upper()}' se shuru hone wala shabd batao."
                )
                game_state["timer_task"] = asyncio.create_task(turn_timer(chat_id, duration, client, "wordchain"))
                logger.info(f"WordChain: {current_player['username']} timed out. Next turn for {next_player['username']} in group {chat_id}.")
        
        elif game_type == "guessing":
            if not game_state["guessed_this_round"]:
                correct_answer = game_state["current_guess_item"]["answer"]
                await client.send_message(chat_id=chat_id, text=f"Samay samapt! Sahi jawab tha: **{correct_answer.upper()}**.")
            
            game_state["current_round"] += 1
            game_state["last_activity_time"] = datetime.utcnow()
            await save_game_state_to_db(chat_id)
            active_games[chat_id]["timer_task"] = asyncio.create_task(send_next_guess_item(chat_id, client))
            logger.info(f"Guessing game round timed out in {chat_id}. Moving to next round.")

async def start_guessing_game(chat_id: int, client: Client):
    guesses = await get_channel_content("guessing")
    if not guesses:
        await client.send_message(chat_id=chat_id, text="Guessing game content nahi mil paaya. Game abhi shuru nahi ho sakta.")
        del active_games[chat_id]
        if game_states_collection:
            game_states_collection.delete_one({"_id": chat_id})
        logger.warning(f"Guessing game failed to start in {chat_id}: no content.")
        return

    active_games[chat_id]["guessing_data"] = random.sample(guesses, min(len(guesses), 5))
    active_games[chat_id]["current_round"] = 0
    active_games[chat_id]["current_guess_item"] = {}
    active_games[chat_id]["attempts"] = {}
    active_games[chat_id]["guessed_this_round"] = False
    active_games[chat_id]["last_activity_time"] = datetime.utcnow()
    await save_game_state_to_db(chat_id)

    active_games[chat_id]["timer_task"] = asyncio.create_task(send_next_guess_item(chat_id, client))

async def send_next_guess_item(chat_id: int, client: Client):
    game_state = active_games.get(chat_id)
    if not game_state or game_state["status"] != "in_progress" or game_state["game_type"] != "guessing":
        return

    if game_state["current_round"] >= len(game_state["guessing_data"]):
        await client.send_message(chat_id=chat_id, text="Guessing game khatam! Sabhi items guess kiye ja chuke hain.")
        del active_games[chat_id]
        if game_states_collection:
            game_states_collection.delete_one({"_id": chat_id})
        logger.info(f"Guessing game ended in group {chat_id}: all rounds complete.")
        return

    guess_item = game_state["guessing_data"][game_state["current_round"]]
    
    await client.send_message(chat_id=chat_id, text=f"**Round {game_state['current_round'] + 1}:**\n\nIs shabd/phrase ko guess karo: `{guess_item['question']}`", parse_mode="Markdown")
    
    game_state["current_guess_item"] = {
        "question": guess_item["question"],
        "answer": guess_item["answer"].lower()
    }
    game_state["guessed_this_round"] = False
    game_state["attempts"] = {str(p["user_id"]): 0 for p in game_state["players"]}
    game_state["last_activity_time"] = datetime.utcnow()
    await save_game_state_to_db(chat_id)

    if game_state.get("timer_task"):
        game_state["timer_task"].cancel()
    game_state["timer_task"] = asyncio.create_task(turn_timer(chat_id, 60, client, "guessing"))
    logger.info(f"Guessing game: new round {game_state['current_round']+1} started in {chat_id}.")

async def handle_guessing_answer(message: Message, client: Client):
    chat_id = message.chat.id
    user = message.from_user
    game_state = active_games.get(chat_id)

    if not game_state or game_state["game_type"] != "guessing" or game_state["status"] != "in_progress":
        return
    if user.id not in [p["user_id"] for p in game_state["players"]]:
        return
    if game_state["guessed_this_round"]:
        return

    user_guess = message.text.strip().lower()
    correct_answer = game_state["current_guess_item"]["answer"]

    if user_guess == correct_answer:
        await update_user_score(user.id, user.full_name, chat_id, 15)
        await message.reply_text(f"Shandar, {user.first_name}! Tumne sahi guess kiya: **{correct_answer.upper()}**! Tumhe 15 points mile.")
        game_state["guessed_this_round"] = True
        
        if game_state.get("timer_task"):
            game_state["timer_task"].cancel()
        
        game_state["current_round"] += 1
        game_state["last_activity_time"] = datetime.utcnow()
        await save_game_state_to_db(chat_id)
        active_games[chat_id]["timer_task"] = asyncio.create_task(send_next_guess_item(chat_id, client))
        logger.info(f"Guessing game: User {user.full_name} guessed correctly in {chat_id}.")
    else:
        user_id_str = str(user.id)
        game_state["attempts"][user_id_str] = game_state["attempts"].get(user_id_str, 0) + 1
        await message.reply_text("Galat guess. Phir se koshish karo!")
        game_state["last_activity_time"] = datetime.utcnow()
        await save_game_state_to_db(chat_id)
        logger.info(f"Guessing game: User {user.full_name} made wrong guess in {chat_id}. Attempts: {game_state['attempts'][user_id_str]}.")

async def start_number_guessing_game(chat_id: int, client: Client):
    secret_number = random.randint(1, 100)
    active_games[chat_id]["secret_number"] = secret_number
    active_games[chat_id]["guesses_made"] = {}
    active_games[chat_id]["last_activity_time"] = datetime.utcnow()
    await save_game_state_to_db(chat_id)
    
    await client.send_message(
        chat_id=chat_id,
        text=f"**Sankhya Anuamaan Shuru!**\n\nMaine 1 se 100 ke beech ek number socha hai. Guess karo!"
    )
    active_games[chat_id]["timer_task"] = asyncio.create_task(auto_end_game_timer(chat_id, client))
    logger.info(f"Number Guessing game started in group {chat_id}. Secret: {secret_number}.")

async def handle_number_guess(message: Message, client: Client):
    chat_id = message.chat.id
    user = message.from_user
    game_state = active_games.get(chat_id)

    if not game_state or game_state["game_type"] != "number_guessing" or game_state["status"] != "in_progress":
        return
    if user.id not in [p["user_id"] for p in game_state["players"]]:
        return

    try:
        user_guess = int(message.text)
        if not (1 <= user_guess <= 100):
            await message.reply_text("Kripya 1 se 100 ke beech ek number type karein.")
            return
    except ValueError:
        await message.reply_text("Kripya ek valid number type karein.")
        return

    secret_number = game_state["secret_number"]
    
    user_id_str = str(user.id)
    game_state["guesses_made"][user_id_str] = game_state["guesses_made"].get(user_id_str, 0) + 1 
    game_state["last_activity_time"] = datetime.utcnow()
    await save_game_state_to_db(chat_id)
    
    if user_guess == secret_number:
        guesses_count = game_state["guesses_made"][user_id_str]
        points = 100 - (guesses_count * 5)
        points = max(10, points)
        await update_user_score(user.id, user.full_name, chat_id, points)
        await message.reply_text(f"Sahi jawab, {user.first_name}! Number **{secret_number}** tha! Tumhe {points} points mile. (Koshish: {guesses_count})")
        
        if game_state.get("timer_task"):
            game_state["timer_task"].cancel()
        del active_games[chat_id]
        if game_states_collection:
            game_states_collection.delete_one({"_id": chat_id})
        logger.info(f"Number Guessing: User {user.full_name} won in group {chat_id}. Game ended.")

    elif user_guess < secret_number:
        await message.reply_text("Mera number isse **bada** hai.")
    else:
        await message.reply_text("Mera number isse **chhota** hai.")
    logger.info(f"Number Guessing: User {user.full_name} guessed {user_guess} in group {chat_id}.")


# --- Pyrogram Command Handlers ---

@app.on_message(filters.command("start"))
async def start_command(client: Client, message: Message):
    user = message.from_user
    chat = message.chat
    
    await message.reply_html(
        f"Hi {user.mention}! Main ek game bot hoon. /games type karke available games dekho."
    )
    
    log_message = ""
    if chat.type == "private":
        log_message = (
            f"**Naya User Join Hua!**\n"
            f"User ID: `{user.id}`\n"
            f"Username: @{user.username} (or {user.full_name})\n"
            f"First Name: {user.first_name}\n"
            f"Last Name: {user.last_name or 'N/A'}"
        )
        logger.info(f"New user started bot: {user.full_name} ({user.id})")
    elif chat.type in ["group", "supergroup"]:
        if groups_collection:
            existing_group = groups_collection.find_one({"_id": chat.id})
            if not existing_group:
                log_message = (
                    f"**Naye Group Mein Bot Add Hua!**\n"
                    f"Group ID: `{chat.id}`\n"
                    f"Group Name: {chat.title}\n"
                    f"Added by User ID: `{user.id}`\n"
                    f"Added by Username: @{user.username} (or {user.full_name})"
                )
                logger.info(f"Bot added to new group: {chat.title} ({chat.id}) by {user.full_name}")
            
            groups_collection.update_one(
                {"_id": chat.id},
                {"$set": {"name": chat.title, "active": True, "last_seen": datetime.utcnow()}},
                upsert=True
            )

    if log_message and LOG_CHANNEL_ID:
        try:
            await client.send_message(chat_id=LOG_CHANNEL_ID, text=log_message, parse_mode="Markdown")
            logger.info(f"Sent log message to channel {LOG_CHANNEL_ID}.")
        except Exception as e:
            logger.error(f"Failed to send log message to channel {LOG_CHANNEL_ID}: {e}")

@app.on_message(filters.command("games"))
async def games_command(client: Client, message: Message):
    keyboard = []
    for game_name, game_callback_data in GAMES_LIST:
        keyboard.append([InlineKeyboardButton(game_name, callback_data=f"show_rules_{game_callback_data}")])
    
    reply_markup = InlineKeyboardMarkup(keyboard)
    await message.reply_text("Kaun sa game khelna chahte ho?", reply_markup=reply_markup)

@app.on_message(filters.command("broadcast"))
async def broadcast_command(client: Client, message: Message):
    if message.from_user.id != ADMIN_USER_ID:
        await message.reply_text("Tum is command ko use nahi kar sakte.")
        return

    if not message.command or len(message.command) < 2:
        await message.reply_text("Please message content provide karo broadcast ke liye.")
        return
    
    message_content = " ".join(message.command[1:]) # Skip the command itself
    
    sent_count = 0
    failed_count = 0
    
    if groups_collection:
        all_groups = groups_collection.find({"active": True})
        
        for group in all_groups:
            try:
                await client.send_message(chat_id=group["_id"], text=message_content)
                sent_count += 1
                await asyncio.sleep(0.1) # Small delay to avoid hitting Telegram API limits
            except Exception as e:
                logger.error(f"Could not send message to group {group['_id']}: {e}")
                failed_count += 1
                if "chat not found" in str(e).lower() or "bot was blocked by the user" in str(e).lower():
                    groups_collection.update_one({"_id": group["_id"]}, {"$set": {"active": False}})
    else:
        logger.warning("Groups collection not initialized, cannot broadcast.")

    await message.reply_text(f"Broadcast complete. Sent to {sent_count} groups, {failed_count} failed.")

@app.on_message(filters.command("endgame") & filters.group)
async def endgame_command(client: Client, message: Message):
    chat_id = message.chat.id
    user_id = message.from_user.id

    if not await is_admin(chat_id, user_id, client):
        await message.reply_text("Sirf group admin hi game end kar sakte hain.")
        return

    if chat_id in active_games:
        game_state = active_games[chat_id]
        if game_state.get("timer_task"):
            game_state["timer_task"].cancel()
            logger.info(f"Cancelled timer task for group {chat_id}.")
        del active_games[chat_id]
        if game_states_collection:
            game_states_collection.delete_one({"_id": chat_id})
        await message.reply_text("Game khatam kar diya gaya hai.")
        logger.info(f"Game ended for group {chat_id} by admin.")
    else:
        await message.reply_text("Iss group mein koi active game nahi hai.")

@app.on_message(filters.command("leaderboard"))
async def leaderboard_command(client: Client, message: Message):
    group_id = message.chat.id if message.chat.type in ["group", "supergroup"] else None
    
    if group_id:
        group_leaders = await get_leaderboard(group_id)
        if group_leaders:
            group_lb_text = "**Iss Group Ke Top Khiladi:**\n"
            for i, user_data in enumerate(group_leaders):
                score = user_data.get('group_scores', {}).get(str(group_id), 0)
                group_lb_text += f"{i+1}. {user_data.get('username', 'Unknown')} - {score} points\n"
        else:
            group_lb_text = "**Iss group mein abhi koi score nahi.**"
        await message.reply_text(group_lb_text, parse_mode="Markdown")

    world_leaders = await get_leaderboard()
    if world_leaders:
        world_lb_text = "\n\n**Worldwide Top Khiladi:**\n"
        for i, user_data in enumerate(world_leaders):
            world_lb_text += f"{i+1}. {user_data.get('username', 'Unknown')} - {user_data.get('total_score', 0)} points\n"
    else:
        world_lb_text = "\n\n**Worldwide abhi koi score nahi.**"
    await message.reply_text(world_lb_text, parse_mode="Markdown")

@app.on_message(filters.command("mystats"))
async def mystats_command(client: Client, message: Message):
    user_id = message.from_user.id
    if users_collection:
        user_data = users_collection.find_one({"user_id": user_id})
    else:
        user_data = None
        logger.warning("Users collection not initialized, cannot fetch user stats.")

    if user_data:
        stats_text = f"**{user_data.get('username', 'Tumhare')} Stats:**\n" \
                     f"Total Score: {user_data.get('total_score', 0)} points\n"
        
        if user_data.get('group_scores') and groups_collection:
            stats_text += "\n**Group-wise Scores:**\n"
            for group_id_str, score in user_data['group_scores'].items():
                try:
                    group_info = groups_collection.find_one({"_id": int(group_id_str)})
                    group_name = group_info.get("name", f"Group ID: {group_id_str}") if group_info else f"Group ID: {group_id_str}"
                    stats_text += f"- {group_name}: {score} points\n"
                except ValueError:
                    stats_text += f"- Invalid Group ID ({group_id_str}): {score} points\n"
        
        await message.reply_text(stats_text, parse_mode="Markdown")
    else:
        await message.reply_text("Tumne abhi tak koi game nahi khela hai ya tumhara data nahi mila.")

# --- Pyrogram Callback Query Handlers ---
@app.on_callback_query()
async def button_handler(client: Client, query: CallbackQuery):
    await query.answer()
    chat_id = query.message.chat.id
    user = query.from_user
    data = query.data
    
    if data.startswith("show_rules_"):
        game_type_code = data.replace("show_rules_", "")
        game_name = next((name for name, code in GAMES_LIST if code == game_type_code), "Unknown Game")
        
        rules = ""
        if game_type_code == "quiz":
            rules = "Quiz: Bot questions poochega, sabse pehle sahi jawab do. Har 20 seconds mein naya question ya poll."
        elif game_type_code == "wordchain":
            rules = "Shabd Shrinkhala: Bot ek shabd dega. Agla player uske aakhri akshar se naya shabd banayega. Galat jawab dene par ya samay par jawab na dene par player game se bahar."
        elif game_type_code == "guessing":
            rules = "Andaaz Lagaao: Bot ek jumbled word ya hint dega. Sahi shabd guess karo. Jo sabse pehle sahi guess karega, woh round jeetega."
        elif game_type_code == "number_guessing":
            rules = "Sankhya Anuamaan: Bot 1 se 100 ke beech ek number socha hai. Tum guess karo, bot batayega higher/lower. Jo sabse kam koshish mein sahi guess karega, use zyada points milenge."
            
        keyboard = [[InlineKeyboardButton(f"{game_name} Start Karo!", callback_data=f"start_game_{game_type_code}")]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await query.edit_message_text(f"**{game_name} Ke Niyam:**\n\n{rules}\n\nGame shuru karne ke liye button dabao.",
                                      parse_mode="Markdown", reply_markup=reply_markup)

    elif data.startswith("start_game_"):
        game_type_code = data.replace("start_game_", "")
        
        if chat_id in active_games:
            await query.edit_message_text("Iss group mein pehle se hi ek game chal raha hai. Use /endgame se khatam karo.")
            return

        active_games[chat_id] = {
            "game_type": game_type_code,
            "players": [],
            "status": "waiting_for_players",
            "current_round": 0,
            "timer_task": None,
            "last_activity_time": datetime.utcnow()
        }
        await save_game_state_to_db(chat_id)
        
        await query.edit_message_text(f"**{next((name for name, code in GAMES_LIST if code == game_type_code), 'Game')} Shuru Ho Raha Hai!**\n\n1 minute mein game shuru hoga. Judne ke liye 'Mein Khelunga' button dabao.\n\n**Khiladi:**",
                                      parse_mode="Markdown",
                                      reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Mein Khelunga!", callback_data=f"join_game_{chat_id}")]]))
        
        active_games[chat_id]["timer_task"] = asyncio.create_task(
            start_game_countdown(chat_id, game_type_code, query.message, client)
        )
        logger.info(f"Starting countdown for game {game_type_code} in group {chat_id}.")

    elif data.startswith("join_game_"):
        game_id = int(data.replace("join_game_", ""))
        
        if game_id not in active_games or active_games[game_id]["status"] != "waiting_for_players":
            await query.answer("Maaf karna, ab tum game join nahi kar sakte ya game shuru ho gaya hai.", show_alert=True)
            return

        player_data = {"user_id": user.id, "username": user.full_name}
        if player_data not in active_games[game_id]["players"]:
            active_games[game_id]["players"].append(player_data)
            await save_game_state_to_db(game_id)
            
            player_list_text = "\n".join([p["username"] for p in active_games[game_id]["players"]])
            try:
                await query.edit_message_text(f"**{next((name for name, code in GAMES_LIST if code == active_games[game_id]['game_type']), 'Game')} Shuru Ho Raha Hai!**\n\n1 minute mein game shuru hoga. Judne ke liye 'Mein Khelunga' button dabao.\n\n**Khiladi:**\n{player_list_text}",
                                              parse_mode="Markdown",
                                              reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Mein Khelunga!", callback_data=f"join_game_{game_id}")]]))
            except Exception as e:
                await client.send_message(chat_id=game_id, text=f"{user.full_name} ne game join kar liya hai. Ab kul khiladi: {len(active_games[game_id]['players'])}",
                                               reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Mein Khelunga!", callback_data=f"join_game_{game_id}")]]))

            await query.answer(f"{user.first_name} ne game join kar liya hai!")
            logger.info(f"User {user.full_name} joined game in group {game_id}.")
        else:
            await query.answer("Tum pehle hi game mein shamil ho.", show_alert=True)

# --- Pyrogram Message Handlers (for general text and game answers) ---

@app.on_message(filters.text & ~filters.command & filters.group)
async def handle_text_messages(client: Client, message: Message):
    chat_id = message.chat.id
    game_state = active_games.get(chat_id)
    
    if game_state and game_state["status"] == "in_progress":
        if game_state["game_type"] == "quiz":
            await handle_quiz_answer_text(message, client)
        elif game_state["game_type"] == "wordchain":
            await handle_wordchain_answer(message, client)
        elif game_state["game_type"] == "guessing":
            await handle_guessing_answer(message, client)
        elif game_state["game_type"] == "number_guessing":
            await handle_number_guess(message, client)

@app.on_poll_answer()
async def handle_poll_answer_main(client: Client, poll_answer: PollAnswer):
    await handle_quiz_poll_answer(poll_answer, client)

# --- Flask Server Function ---
def run_flask_server():
    """Starts the Flask server in a separate thread."""
    # Koyeb 0.0.0.0 पर bind करने की मांग करता है
    logger.info("Starting Flask server on port 8080...")
    # Flask को 8080 पर चलाने के लिए, gunicorn का उपयोग करें
    # Flask की अपनी डेवलपमेंट सर्वर को सीधे प्रोडक्शन में उपयोग न करें
    # यदि आप इसे सीधे Koyeb पर चला रहे हैं और Gunicorn की आवश्यकता नहीं है
    # तो flask_app_instance.run(host="0.0.0.0", port=8080) का उपयोग करें
    try:
        from gunicorn.app.base import BaseApplication

        class StandaloneApplication(BaseApplication):
            def __init__(self, app, options=None):
                self.application = app
                self.options = options or {}
                super().__init__()

            def load_config(self):
                for key, value in self.options.items():
                    if key in self.cfg.settings and value is not None:
                        self.cfg.set(key.lower(), value)

            def load_wsgi(self):
                return self.application

        options = {
            'bind': '%s:%s' % ('0.0.0.0', '8080'), # Koyeb पर पोर्ट 8080 पर चलाएं
            'workers': 1, # सामान्यत: एक वर्कर ही पर्याप्त होता है
            'accesslog': '-', # stdout पर लॉग करें
            'errorlog': '-', # stderr पर लॉग करें
            'loglevel': 'info'
        }
        StandaloneApplication(flask_app_instance, options).run()
    except ImportError:
        logger.warning("Gunicorn not found, falling back to Flask's built-in server. This is not recommended for production.")
        # यदि gunicorn इंस्टॉल नहीं है, तो Flask के बिल्ट-इन सर्वर का उपयोग करें (केवल डेवलपमेंट के लिए)
        flask_app_instance.run(host="0.0.0.0", port=8080)
    except Exception as e:
        logger.error(f"Error starting Flask server: {e}")


# --- Main Bot Startup Function ---
async def start_telegram_bot_and_flask():
    """Main function to initialize and start the Pyrogram bot and Flask server."""
    
    # MongoDB collections को initialize करें
    try:
        init_mongo_collections()
    except Exception as e:
        logger.critical(f"CRITICAL ERROR: Could not initialize MongoDB collections. Bot cannot start: {e}")
        # MongoDB connection failure पर bot को शुरू न करें
        return 

    # Flask सर्वर को एक अलग थ्रेड में शुरू करें
    # Koyeb पर, HTTP पोर्ट आमतौर पर 8080 होता है।
    # सुनिश्चित करें कि आपकी Koyeb सर्विस कॉन्फ़िगरेशन में पोर्ट 8080 पर HTTP रूट है।
    flask_thread = Thread(target=run_flask_server)
    flask_thread.daemon = True # Flask thread को main thread के साथ खत्म होने दें
    flask_thread.start()
    logger.info("Flask server thread started.")

    # Load active game states from DB
    await load_game_state_from_db()

    logger.info("Pyrogram bot application initializing...")
    await app.start() # Bot को शुरू करें
    logger.info("Pyrogram bot started successfully.")

    # Bot के शुरू होने के बाद, यदि कोई खेल सक्रिय था, तो उनके टाइमर को फिर से शुरू करें
    for chat_id, game_state in active_games.items():
        if game_state["status"] == "in_progress":
            logger.info(f"Re-initializing timer for active game in chat {chat_id} (Type: {game_state['game_type']}).")
            if game_state["game_type"] == "quiz":
                game_state["timer_task"] = asyncio.create_task(
                    send_next_quiz_question_with_timer(chat_id, app)
                )
            elif game_state["game_type"] == "wordchain":
                 game_state["timer_task"] = asyncio.create_task(
                    turn_timer(chat_id, 60, app, "wordchain")
                )
            elif game_state["game_type"] == "guessing":
                game_state["timer_task"] = asyncio.create_task(
                    send_next_guess_item(chat_id, app)
                )
            elif game_state["game_type"] == "number_guessing":
                game_state["timer_task"] = asyncio.create_task(
                    auto_end_game_timer(chat_id, app)
                )
            # सुनिश्चित करें कि timer_task को सक्रिय खेलों के लिए सहेजा जा रहा है
            await save_game_state_to_db(chat_id)
        else:
            # यदि गेम 'waiting_for_players' स्थिति में था और बॉट बंद हो गया, तो उसे रद्द कर दें
            if game_state["status"] == "waiting_for_players":
                logger.info(f"Game {game_state['game_type']} in chat {chat_id} was waiting for players. Clearing its state.")
                del active_games[chat_id]
                if game_states_collection:
                    game_states_collection.delete_one({"_id": chat_id})

    await idle() # Bot को अनिश्चित काल तक चलते रहने दें

    logger.info("Pyrogram bot stopping...")
    await app.stop() # Bot को सुरक्षित रूप से बंद करें
    logger.info("Pyrogram bot stopped.")


if __name__ == "__main__":
    # asyncio इवेंट लूप को चलाएं
    try:
        asyncio.run(start_telegram_bot_and_flask())
    except KeyboardInterrupt:
        logger.info("Bot stopped by user (Ctrl+C).")
    except Exception as e:
        logger.critical(f"An unhandled exception occurred during bot execution: {e}", exc_info=True)

