# bot.py

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, MessageHandler, filters, PollAnswerHandler
from pymongo import MongoClient
import logging
import os
import asyncio
import random
from datetime import datetime, timedelta

# --- Configuration (Yahan Bot-specific settings) ---
BOT_TOKEN = os.getenv("YOUR_BOT_TOKEN")
MONGO_URI = os.getenv("YOUR_MONGO_URI")
DB_NAME = "telegram_games_db"
ADMIN_USER_ID = int(os.getenv("ADMIN_USER_ID"))
CONTENT_CHANNEL_ID = int(os.getenv("CONTENT_CHANNEL_ID")) 
LOG_CHANNEL_ID = int(os.getenv("LOG_CHANNEL_ID")) # Log Channel ID

logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

# MongoDB client initialization
try:
    mongo_client = MongoClient(MONGO_URI)
    db = mongo_client[DB_NAME]
    
    users_collection = db["users"]
    groups_collection = db["groups"]
    game_states_collection = db["game_states"]
    channel_content_cache_collection = db["channel_content_cache"]
    logger.info("MongoDB connected successfully.")

    # Setup TTL indexes - यह सिर्फ एक बार बॉट शुरू होने पर चलना चाहिए
    users_collection.create_index([("last_updated", 1)], expireAfterSeconds=365 * 24 * 60 * 60) # 1 saal
    logger.info("TTL index on 'users' collection created/updated.")

except Exception as e:
    logger.error(f"Failed to connect to MongoDB or set up indexes: {e}")
    exit(1)


# --- Game Constants ---
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

active_games = {} 


# --- Helper Functions ---
async def get_channel_content(game_type: str):
    logger.info(f"Fetching content for game_type: {game_type}")
    content = list(channel_content_cache_collection.find({"game_type": game_type}))
    if not content:
        logger.warning(f"No content found for game type: {game_type} in DB.")
    return content

async def update_user_score(user_id: int, username: str, group_id: int, points: int):
    users_collection.update_one(
        {"user_id": user_id},
        {"$inc": {"total_score": points, f"group_scores.{group_id}": points},
         "$set": {"username": username, "last_updated": datetime.utcnow()}},
        upsert=True
    )
    logger.info(f"User {username} ({user_id}) scored {points} points in group {group_id}.")

async def get_leaderboard(group_id: int = None):
    if group_id:
        return list(users_collection.find().sort(f"group_scores.{group_id}", -1).limit(10))
    else:
        return list(users_collection.find().sort("total_score", -1).limit(10))

async def is_admin(chat_id: int, user_id: int, bot_instance: Application.bot):
    try:
        chat_member = await bot_instance.get_chat_member(chat_id, user_id)
        return chat_member.status in ["administrator", "creator"]
    except Exception as e:
        logger.error(f"Error checking admin status for chat {chat_id}, user {user_id}: {e}")
        return False

async def save_game_state_to_db(chat_id: int):
    if chat_id in active_games:
        game_states_collection.update_one(
            {"_id": chat_id},
            {"$set": active_games[chat_id]},
            upsert=True
        )
        logger.info(f"Game state saved for group {chat_id}.")

async def load_game_state_from_db():
    global active_games
    active_games = {doc["_id"]: doc for doc in game_states_collection.find()}
    logger.info(f"Loaded {len(active_games)} active games from DB.")

# --- Command Handlers ---
async def start_command(update: Update, context: Application.bot): # context is now the bot instance
    """Bot start hone par welcome message deta hai aur log channel mein update bhejta hai."""
    user = update.effective_user
    chat = update.effective_chat
    
    await update.message.reply_html(
        f"Hi {user.mention_html()}! Main ek game bot hoon. /games type karke available games dekho."
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
        # Check if group already exists in DB to avoid duplicate logging on restart
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
            {"$set": {"name": chat.title, "active": True}},
            upsert=True
        )

    if log_message and LOG_CHANNEL_ID:
        try:
            await context.bot.send_message(chat_id=LOG_CHANNEL_ID, text=log_message, parse_mode="Markdown")
            logger.info(f"Sent log message to channel {LOG_CHANNEL_ID}.")
        except Exception as e:
            logger.error(f"Failed to send log message to channel {LOG_CHANNEL_ID}: {e}")


async def games_command(update: Update, context):
    keyboard = []
    for game_name, game_callback_data in GAMES_LIST:
        keyboard.append([InlineKeyboardButton(game_name, callback_data=f"show_rules_{game_callback_data}")])
    
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text("Kaun sa game khelna chahte ho?", reply_markup=reply_markup)

async def broadcast_command(update: Update, context):
    if update.effective_user.id != ADMIN_USER_ID:
        await update.message.reply_text("Tum is command ko use nahi kar sakte.")
        return

    if not context.args:
        await update.message.reply_text("Please message content provide karo broadcast ke liye.")
        return
    
    message_content = " ".join(context.args)
    
    sent_count = 0
    failed_count = 0
    
    all_groups = groups_collection.find({"active": True})
    
    for group in all_groups:
        try:
            await context.bot.send_message(chat_id=group["_id"], text=message_content)
            sent_count += 1
            await asyncio.sleep(0.1) # Flood limits se bachne ke liye
        except Exception as e:
            logger.error(f"Could not send message to group {group['_id']}: {e}")
            failed_count += 1
            if "chat not found" in str(e).lower() or "bot was blocked by the user" in str(e).lower():
                groups_collection.update_one({"_id": group["_id"]}, {"$set": {"active": False}})

    await update.message.reply_text(f"Broadcast complete. Sent to {sent_count} groups, {failed_count} failed.")

async def endgame_command(update: Update, context):
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id

    if not await is_admin(chat_id, user_id, context.bot): 
        await update.message.reply_text("Sirf group admin hi game end kar sakte hain.")
        return

    if chat_id in active_games:
        game_state = active_games[chat_id]
        if game_state.get("timer_task"):
            game_state["timer_task"].cancel() 
            logger.info(f"Cancelled timer task for group {chat_id}.")
        del active_games[chat_id] 
        game_states_collection.delete_one({"_id": chat_id}) 
        await update.message.reply_text("Game khatam kar diya gaya hai.")
        logger.info(f"Game ended for group {chat_id} by admin.")
    else:
        await update.message.reply_text("Iss group mein koi active game nahi hai.")

async def leaderboard_command(update: Update, context):
    group_id = update.effective_chat.id if update.effective_chat.type in ["group", "supergroup"] else None
    
    if group_id:
        group_leaders = await get_leaderboard(group_id)
        if group_leaders:
            group_lb_text = "**Iss Group Ke Top Khiladi:**\n"
            for i, user_data in enumerate(group_leaders):
                score = user_data.get('group_scores', {}).get(str(group_id), 0)
                group_lb_text += f"{i+1}. {user_data.get('username', 'Unknown')} - {score} points\n"
        else:
            group_lb_text = "**Iss group mein abhi koi score nahi.**"
        await update.message.reply_text(group_lb_text, parse_mode="Markdown")

    world_leaders = await get_leaderboard()
    if world_leaders:
        world_lb_text = "\n\n**Worldwide Top Khiladi:**\n"
        for i, user_data in enumerate(world_leaders):
            world_lb_text += f"{i+1}. {user_data.get('username', 'Unknown')} - {user_data.get('total_score', 0)} points\n"
    else:
        world_lb_text = "\n\n**Worldwide abhi koi score nahi.**"
    await update.message.reply_text(world_lb_text, parse_mode="Markdown")

async def mystats_command(update: Update, context):
    user_id = update.effective_user.id
    user_data = users_collection.find_one({"user_id": user_id})

    if user_data:
        stats_text = f"**{user_data.get('username', 'Tumhare')} Stats:**\n" \
                     f"Total Score: {user_data.get('total_score', 0)} points\n"
        
        if user_data.get('group_scores'):
            stats_text += "\n**Group-wise Scores:**\n"
            for group_id_str, score in user_data['group_scores'].items():
                group_info = groups_collection.find_one({"_id": int(group_id_str)})
                group_name = group_info.get("name", f"Group ID: {group_id_str}") if group_info else f"Group ID: {group_id_str}"
                stats_text += f"- {group_name}: {score} points\n"
        
        await update.message.reply_text(stats_text, parse_mode="Markdown")
    else:
        await update.message.reply_text("Tumne abhi tak koi game nahi khela hai ya tumhara data nahi mila.")


# --- Callback Query Handlers (Button Clicks) ---
async def button_handler(update: Update, context):
    query = update.callback_query
    await query.answer() 
    chat_id = query.message.chat_id
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
            start_game_countdown(chat_id, game_type_code, query.message, context.bot)
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
            await query.edit_message_text(f"**{next((name for name, code in GAMES_LIST if code == active_games[game_id]['game_type']), 'Game')} Shuru Ho Raha Hai!**\n\n1 minute mein game shuru hoga. Judne ke liye 'Mein Khelunga' button dabao.\n\n**Khiladi:**\n{player_list_text}",
                                          parse_mode="Markdown",
                                          reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Mein Khelunga!", callback_data=f"join_game_{game_id}")]]))
            await query.answer(f"{user.first_name} ne game join kar liya hai!")
            logger.info(f"User {user.full_name} joined game in group {game_id}.")
        else:
            await query.answer("Tum pehle hi game mein shamil ho.", show_alert=True)

# --- Game Logic Functions ---
async def start_game_countdown(chat_id: int, game_type_code: str, message_to_edit: Update.message, bot_instance: Application.bot):
    await asyncio.sleep(60) 
    
    if chat_id in active_games and active_games[chat_id]["status"] == "waiting_for_players":
        active_games[chat_id]["status"] = "in_progress"
        await save_game_state_to_db(chat_id)

        players_count = len(active_games[chat_id]["players"])
        if players_count == 0:
            await message_to_edit.edit_text("Koi khiladi nahi juda. Game cancel kar diya gaya.")
            del active_games[chat_id]
            game_states_collection.delete_one({"_id": chat_id})
            logger.info(f"Game cancelled in group {chat_id}: no players.")
            return

        await message_to_edit.edit_text(f"**{next((name for name, code in GAMES_LIST if code == game_type_code), 'Game')} Shuru!**\n\nKul {players_count} khiladi shamil hain.")
        logger.info(f"Game {game_type_code} started in group {chat_id} with {players_count} players.")
        
        if game_type_code == "quiz":
            await start_quiz_game(chat_id, bot_instance)
        elif game_type_code == "wordchain":
            await start_wordchain_game(chat_id, bot_instance)
        elif game_type_code == "guessing":
            await start_guessing_game(chat_id, bot_instance)
        elif game_type_code == "number_guessing":
            await start_number_guessing_game(chat_id, bot_instance)
    
    active_games[chat_id]["timer_task"] = asyncio.create_task(auto_end_game_timer(chat_id, bot_instance))


async def auto_end_game_timer(chat_id: int, bot_instance: Application.bot):
    game_state = active_games.get(chat_id)
    if not game_state:
        return 

    last_activity = game_state.get("last_activity_time", datetime.utcnow())
    time_since_last_activity = (datetime.utcnow() - last_activity).total_seconds()

    if time_since_last_activity < 300: 
        await asyncio.sleep(300 - time_since_last_activity)
        return await auto_end_game_timer(chat_id, bot_instance) 

    if chat_id in active_games and active_games[chat_id]["status"] == "in_progress":
        await bot_instance.send_message(chat_id=chat_id, text="Game mein 5 minute se koi activity nahi. Game khatam kar diya gaya.")
        del active_games[chat_id]
        game_states_collection.delete_one({"_id": chat_id})
        logger.info(f"Game auto-ended due to inactivity in group {chat_id}.")


# --- Game Specific Logic ---

# Quiz / Trivia
quiz_questions_cache = {} 

async def start_quiz_game(chat_id: int, bot_instance: Application.bot):
    questions = await get_channel_content("quiz")
    if not questions:
        await bot_instance.send_message(chat_id=chat_id, text="Quiz questions nahi mil paaye. Game abhi shuru nahi ho sakta.")
        del active_games[chat_id]
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

    await send_next_quiz_question(chat_id, bot_instance)

async def send_next_quiz_question(chat_id: int, bot_instance: Application.bot):
    game_state = active_games.get(chat_id)
    if not game_state or game_state["status"] != "in_progress" or game_state["game_type"] != "quiz":
        return

    if game_state["current_round"] >= len(game_state["quiz_data"]):
        await bot_instance.send_message(chat_id=chat_id, text="Quiz khatam! Sabhi sawal pooche ja chuke hain.")
        del active_games[chat_id]
        game_states_collection.delete_one({"_id": chat_id})
        logger.info(f"Quiz game ended in group {chat_id}: all questions asked.")
        return

    question_data = game_state["quiz_data"][game_state["current_round"]]
    
    if "options" in question_data: 
        message = await bot_instance.send_poll(
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
            "message_id": message.message_id, # This is the message_id for the poll message
            "poll_id": message.poll.id, # This is the actual poll_id for PollAnswerHandler
            "correct_answer": question_data["options"][question_data["correct_option_id"]],
            "correct_option_id": question_data["correct_option_id"]
        }
    else: 
        await bot_instance.send_message(chat_id=chat_id, text=f"**Sawal {game_state['current_round'] + 1}:**\n\n{question_data['text']}", parse_mode="Markdown")
        game_state["current_question"] = {
            "type": "text",
            "correct_answer": question_data["answer"].lower() 
        }
    
    game_state["answered_this_round"] = False
    game_state["last_activity_time"] = datetime.utcnow() 
    await save_game_state_to_db(chat_id)

    await asyncio.sleep(20) 
    game_state["current_round"] += 1
    await send_next_quiz_question(chat_id, bot_instance)

async def handle_quiz_answer(update: Update, context: Application.bot):
    chat_id = update.effective_chat.id
    user = update.effective_user
    game_state = active_games.get(chat_id)

    if not game_state or game_state["game_type"] != "quiz" or game_state["status"] != "in_progress":
        return
    if user.id not in [p["user_id"] for p in game_state["players"]]:
        return 
    if game_state["answered_this_round"]:
        return 
    if game_state["current_question"].get("type") == "poll":
        return 

    current_q = game_state["current_question"]
    user_answer = update.message.text.lower()
    
    if user_answer == current_q["correct_answer"]:
        await update.message.reply_text(f"Sahi jawab, {user.first_name}! Tumhe 10 points mile.", reply_to_message_id=update.message.message_id)
        await update_user_score(user.id, user.full_name, chat_id, 10)
        game_state["answered_this_round"] = True
        game_state["last_activity_time"] = datetime.utcnow() 
        await save_game_state_to_db(chat_id)
        logger.info(f"Quiz: User {user.full_name} gave correct answer in group {chat_id}.")

async def handle_poll_answer(update: Update, context: Application.bot):
    poll_answer = update.poll_answer
    user_id = poll_answer.user.id
    user_name = poll_answer.user.full_name

    for chat_id, game_state in active_games.items():
        if game_state.get("game_type") == "quiz" and game_state.get("status") == "in_progress":
            current_q = game_state.get("current_question", {})
            # Check if this poll_id belongs to the current active quiz poll in this group
            if current_q.get("type") == "poll" and current_q.get("poll_id") == poll_answer.poll_id:
                
                if user_id not in [p["user_id"] for p in game_state["players"]]:
                    logger.info(f"Poll answer from non-player {user_name}.")
                    return

                if game_state["answered_this_round"]:
                    logger.info(f"Poll: Already answered this round in group {chat_id}.")
                    return
                
                correct_option_id = current_q.get("correct_option_id")
                if correct_option_id is not None and correct_option_id in poll_answer.option_ids:
                    await context.bot.send_message(chat_id=chat_id, text=f"Sahi jawab, {user_name}! Tumhe 10 points mile.")
                    await update_user_score(user_id, user_name, chat_id, 10)
                    game_state["answered_this_round"] = True
                    game_state["last_activity_time"] = datetime.utcnow()
                    await save_game_state_to_db(chat_id)
                    logger.info(f"Quiz Poll: User {user_name} gave correct answer in group {chat_id}.")
                break # Found the game, exit loop
    else:
        logger.warning(f"Poll answer received for unknown poll ID: {poll_answer.poll_id}")


# Word Chain
async def start_wordchain_game(chat_id: int, bot_instance: Application.bot):
    words_data = await get_channel_content("wordchain")
    if not words_data:
        await bot_instance.send_message(chat_id=chat_id, text="Word Chain words nahi mil paaye. Game abhi shuru nahi ho sakta.")
        del active_games[chat_id]
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
        await bot_instance.send_message(chat_id=chat_id, text="Word Chain ke liye khiladi nahi hain. Game band.")
        del active_games[chat_id]
        game_states_collection.delete_one({"_id": chat_id})
        return

    current_player = active_games[chat_id]["players"][active_games[chat_id]["turn_index"]]

    await bot_instance.send_message(
        chat_id=chat_id,
        text=f"**Shabd Shrinkhala Shuru!**\n\nPehla shabd: **{start_word.upper()}**\n\n{current_player['username']} ki baari hai. '{start_word[-1].upper()}' se shuru hone wala shabd batao."
    )
    active_games[chat_id]["timer_task"] = asyncio.create_task(
        turn_timer(chat_id, 60, bot_instance) 
    )
    logger.info(f"WordChain game started in group {chat_id}. First word: {start_word}")

async def handle_wordchain_answer(update: Update, context: Application.bot):
    chat_id = update.effective_chat.id
    user = update.effective_user
    game_state = active_games.get(chat_id)

    if not game_state or game_state["game_type"] != "wordchain" or game_state["status"] != "in_progress":
        return
    
    current_player = game_state["players"][game_state["turn_index"]]
    if user.id != current_player["user_id"]:
        await update.message.reply_text("Abhi tumhari baari nahi hai.", reply_to_message_id=update.message.message_id)
        return

    user_word = update.message.text.strip().lower()
    last_char_of_prev_word = game_state["current_word"][-1].lower()

    if user_word.startswith(last_char_of_prev_word) and len(user_word) > 1: 
        
        await update_user_score(user.id, user.full_name, chat_id, 5) 
        await update.message.reply_text(f"Sahi! '{user_word.upper()}' ab naya shabd hai. {user.first_name} ko 5 points mile.", reply_to_message_id=update.message.message_id)
        
        game_state["current_word"] = user_word
        game_state["turn_index"] = (game_state["turn_index"] + 1) % len(game_state["players"])
        game_state["last_activity_time"] = datetime.utcnow() 
        await save_game_state_to_db(chat_id)

        if game_state.get("timer_task"):
            game_state["timer_task"].cancel()
        
        next_player = game_state["players"][game_state["turn_index"]]
        await context.bot.send_message(
            chat_id=chat_id,
            text=f"{next_player['username']} ki baari. '{user_word[-1].upper()}' se shuru hone wala shabd batao."
        )
        game_state["timer_task"] = asyncio.create_task(turn_timer(chat_id, 60, context.bot))
        logger.info(f"WordChain: User {user.full_name} gave correct word '{user_word}' in group {chat_id}.")

    else:
        await update.message.reply_text(f"Galat shabd! '{last_char_of_prev_word.upper()}' se shuru hona chahiye tha. Ya shabd valid nahi hai. {user.first_name} game se bahar ho gaya.", reply_to_message_id=update.message.message_id)
        
        game_state["players"].pop(game_state["turn_index"])
        game_state["last_activity_time"] = datetime.utcnow() 
        await save_game_state_to_db(chat_id)

        if game_state.get("timer_task"):
            game_state["timer_task"].cancel()

        if len(game_state["players"]) < 2:
            await context.bot.send_message(chat_id=chat_id, text="Khel khatam! Sirf ek khiladi bacha hai ya koi nahi bacha.")
            del active_games[chat_id]
            game_states_collection.delete_one({"_id": chat_id})
            logger.info(f"WordChain game ended in {chat_id}: not enough players.")
        else:
            game_state["turn_index"] %= len(game_state["players"]) 
            next_player = game_state["players"][game_state["turn_index"]]
            await context.bot.send_message(
                chat_id=chat_id,
                text=f"{next_player['username']} ki baari. '{game_state['current_word'][-1].upper()}' se shuru hone wala shabd batao."
            )
            game_state["timer_task"] = asyncio.create_task(turn_timer(chat_id, 60, context.bot))
            logger.info(f"WordChain: User {user.full_name} failed. Next turn for {next_player['username']} in group {chat_id}.")

async def turn_timer(chat_id: int, duration: int, bot_instance: Application.bot):
    await asyncio.sleep(duration)
    
    game_state = active_games.get(chat_id)
    if game_state and game_state["status"] == "in_progress" and game_state["game_type"] == "wordchain":
        current_player = game_state["players"][game_state["turn_index"]]
        await bot_instance.send_message(chat_id=chat_id, text=f"{current_player['username']} ne jawab nahi diya! Woh game se bahar.")
        
        game_state["players"].pop(game_state["turn_index"])
        game_state["last_activity_time"] = datetime.utcnow() 
        await save_game_state_to_db(chat_id)

        if len(game_state["players"]) < 2:
            await bot_instance.send_message(chat_id=chat_id, text="Khel khatam! Sirf ek khiladi bacha hai ya koi nahi bacha.")
            del active_games[chat_id]
            game_states_collection.delete_one({"_id": chat_id})
            logger.info(f"WordChain game ended in {chat_id} due to timeout: not enough players.")
        else:
            game_state["turn_index"] %= len(game_state["players"]) 
            next_player = game_state["players"][game_state["turn_index"]]
            await bot_instance.send_message(
                chat_id=chat_id,
                text=f"{next_player['username']} ki baari. '{game_state['current_word'][-1].upper()}' se shuru hone wala shabd batao."
            )
            game_state["timer_task"] = asyncio.create_task(turn_timer(chat_id, duration, bot_instance))
            logger.info(f"WordChain: {current_player['username']} timed out. Next turn for {next_player['username']} in group {chat_id}.")


# Guessing Game
async def start_guessing_game(chat_id: int, bot_instance: Application.bot):
    guesses = await get_channel_content("guessing")
    if not guesses:
        await bot_instance.send_message(chat_id=chat_id, text="Guessing game content nahi mil paaya. Game abhi shuru nahi ho sakta.")
        del active_games[chat_id]
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

    await send_next_guess_item(chat_id, bot_instance)

async def send_next_guess_item(chat_id: int, bot_instance: Application.bot):
    game_state = active_games.get(chat_id)
    if not game_state or game_state["status"] != "in_progress" or game_state["game_type"] != "guessing":
        return

    if game_state["current_round"] >= len(game_state["guessing_data"]):
        await bot_instance.send_message(chat_id=chat_id, text="Guessing game khatam! Sabhi items guess kiye ja chuke hain.")
        del active_games[chat_id]
        game_states_collection.delete_one({"_id": chat_id})
        logger.info(f"Guessing game ended in group {chat_id}: all rounds complete.")
        return

    guess_item = game_state["guessing_data"][game_state["current_round"]]
    
    await bot_instance.send_message(chat_id=chat_id, text=f"**Round {game_state['current_round'] + 1}:**\n\nIs shabd/phrase ko guess karo: `{guess_item['question']}`", parse_mode="Markdown")
    
    game_state["current_guess_item"] = {
        "question": guess_item["question"],
        "answer": guess_item["answer"].lower()
    }
    game_state["guessed_this_round"] = False
    game_state["attempts"] = {p["user_id"]: 0 for p in game_state["players"]} 
    game_state["last_activity_time"] = datetime.utcnow() 
    await save_game_state_to_db(chat_id)

    if game_state.get("timer_task"):
        game_state["timer_task"].cancel()
    game_state["timer_task"] = asyncio.create_task(guessing_round_timer(chat_id, 60, bot_instance)) 
    logger.info(f"Guessing game: new round {game_state['current_round']+1} started in {chat_id}.")

async def guessing_round_timer(chat_id: int, duration: int, bot_instance: Application.bot):
    await asyncio.sleep(duration)
    game_state = active_games.get(chat_id)
    if game_state and game_state["status"] == "in_progress" and game_state["game_type"] == "guessing" and not game_state["guessed_this_round"]:
        correct_answer = game_state["current_guess_item"]["answer"]
        await bot_instance.send_message(chat_id=chat_id, text=f"Samay samapt! Sahi jawab tha: **{correct_answer.upper()}**.")
        game_state["current_round"] += 1
        game_state["last_activity_time"] = datetime.utcnow() 
        await save_game_state_to_db(chat_id)
        await send_next_guess_item(chat_id, bot_instance) 
        logger.info(f"Guessing game round timed out in {chat_id}. Moving to next round.")

async def handle_guessing_answer(update: Update, context: Application.bot):
    chat_id = update.effective_chat.id
    user = update.effective_user
    game_state = active_games.get(chat_id)

    if not game_state or game_state["game_type"] != "guessing" or game_state["status"] != "in_progress":
        return
    if user.id not in [p["user_id"] for p in game_state["players"]]:
        return
    if game_state["guessed_this_round"]:
        return 

    user_guess = update.message.text.strip().lower()
    correct_answer = game_state["current_guess_item"]["answer"]

    if user_guess == correct_answer:
        await update_user_score(user.id, user.full_name, chat_id, 15) 
        await update.message.reply_text(f"Shandar, {user.first_name}! Tumne sahi guess kiya: **{correct_answer.upper()}**! Tumhe 15 points mile.", reply_to_message_id=update.message.message_id)
        game_state["guessed_this_round"] = True
        
        if game_state.get("timer_task"):
            game_state["timer_task"].cancel()
        
        game_state["current_round"] += 1
        game_state["last_activity_time"] = datetime.utcnow() 
        await save_game_state_to_db(chat_id)
        await send_next_guess_item(chat_id, context.bot) 
        logger.info(f"Guessing game: User {user.full_name} guessed correctly in {chat_id}.")
    else:
        game_state["attempts"][user.id] = game_state["attempts"].get(user.id, 0) + 1
        await update.message.reply_text("Galat guess. Phir se koshish karo!", reply_to_message_id=update.message.message_id)
        await save_game_state_to_db(chat_id) 
        logger.info(f"Guessing game: User {user.full_name} made wrong guess in {chat_id}.")


# Number Guessing
async def start_number_guessing_game(chat_id: int, bot_instance: Application.bot):
    secret_number = random.randint(1, 100) 
    active_games[chat_id]["secret_number"] = secret_number
    active_games[chat_id]["guesses_made"] = {} 
    active_games[chat_id]["last_activity_time"] = datetime.utcnow() 
    await save_game_state_to_db(chat_id)
    
    await bot_instance.send_message(
        chat_id=chat_id,
        text=f"**Sankhya Anuamaan Shuru!**\n\nMaine 1 se 100 ke beech ek number socha hai. Guess karo!"
    )
    active_games[chat_id]["timer_task"] = asyncio.create_task(auto_end_game_timer(chat_id, bot_instance))
    logger.info(f"Number Guessing game started in group {chat_id}. Secret: {secret_number}.")

async def handle_number_guess(update: Update, context: Application.bot):
    chat_id = update.effective_chat.id
    user = update.effective_user
    game_state = active_games.get(chat_id)

    if not game_state or game_state["game_type"] != "number_guessing" or game_state["status"] != "in_progress":
        return
    if user.id not in [p["user_id"] for p in game_state["players"]]:
        return

    try:
        user_guess = int(update.message.text)
    except ValueError:
        await update.message.reply_text("Kripya ek valid number type karein.", reply_to_message_id=update.message.message_id)
        return

    secret_number = game_state["secret_number"]
    
    game_state["guesses_made"][str(user.id)] = game_state["guesses_made"].get(str(user.id), 0) + 1 
    game_state["last_activity_time"] = datetime.utcnow() 
    await save_game_state_to_db(chat_id)
    
    if user_guess == secret_number:
        guesses_count = game_state["guesses_made"][str(user.id)]
        points = 100 - (guesses_count * 5) 
        points = max(10, points) 
        await update_user_score(user.id, user.full_name, chat_id, points)
        await update.message.reply_text(f"Sahi jawab, {user.first_name}! Number **{secret_number}** tha! Tumhe {points} points mile. (Koshish: {guesses_count})", reply_to_message_id=update.message.message_id)
        
        if game_state.get("timer_task"):
            game_state["timer_task"].cancel()
        del active_games[chat_id]
        game_states_collection.delete_one({"_id": chat_id})
        logger.info(f"Number Guessing: User {user.full_name} won in group {chat_id}.")

    elif user_guess < secret_number:
        await update.message.reply_text("Mera number isse **bada** hai.", reply_to_message_id=update.message.message_id)
    else: 
        await update.message.reply_text("Mera number isse **chhota** hai.", reply_to_message_id=update.message.message_id)
    logger.info(f"Number Guessing: User {user.full_name} guessed {user_guess} in group {chat_id}.")


# --- Main Bot Runner Function ---
async def run_bot():
    logger.info("Starting Telegram Bot Application...")
    
    application = Application.builder().token(BOT_TOKEN).build()
    
    # यह सुनिश्चित करने के लिए कि Application ऑब्जेक्ट सही ढंग से इवेंट लूप के साथ इनिशियलाइज़ हो।
    await application.initialize() 

    await load_game_state_from_db()

    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("games", games_command))
    application.add_handler(CommandHandler("broadcast", broadcast_command))
    application.add_handler(CommandHandler("endgame", endgame_command))
    application.add_handler(CommandHandler("leaderboard", leaderboard_command))
    application.add_handler(CommandHandler("mystats", mystats_command))

    application.add_handler(CallbackQueryHandler(button_handler))

    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text_messages))
    application.add_handler(PollAnswerHandler(handle_poll_answer)) 

    await application.start() 
    logger.info("Telegram Bot Application started and polling for updates.")
    await application.run_until_disconnected() 
    logger.info("Telegram Bot Polling has stopped.")


async def handle_text_messages(update: Update, context: Application.bot):
    chat_id = update.effective_chat.id
    game_state = active_games.get(chat_id)
    
    if game_state and game_state["status"] == "in_progress":
        if game_state["game_type"] == "quiz":
            await handle_quiz_answer(update, context)
        elif game_state["game_type"] == "wordchain":
            await handle_wordchain_answer(update, context)
        elif game_state["game_type"] == "guessing":
            await handle_guessing_answer(update, context)
        elif game_state["game_type"] == "number_guessing":
            await handle_number_guess(update, context)
