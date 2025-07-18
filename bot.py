# bot.py

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, MessageHandler, filters, PollAnswerHandler
from pymongo import MongoClient
import logging
import os
import asyncio
import random
from datetime import datetime, timedelta

# --- Configuration ---
# Environment variables se load karein, ye main.py se pass kiye ja sakte hain ya yahan bhi load ho sakte hain
# Hum inko globally declare kar rahe hain, main.py inko set karega ya environment se lenge
BOT_TOKEN = os.getenv("YOUR_BOT_TOKEN")
MONGO_URI = os.getenv("YOUR_MONGO_URI")
ADMIN_USER_ID = int(os.getenv("ADMIN_USER_ID", 0)) # Default to 0, should be set in .env
CONTENT_CHANNEL_ID = int(os.getenv("CONTENT_CHANNEL_ID", 0)) # Default to 0, should be set in .env
LOG_CHANNEL_ID = int(os.getenv("LOG_CHANNEL_ID", 0)) # Default to 0, should be set in .env

DB_NAME = "telegram_games_db"

# Logging setup
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

# MongoDB client initialization (Globally declared but initialized in initialize_telegram_bot_application for safety)
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
        # ensure_index is deprecated, create_index is current
        users_collection.create_index([("last_updated", 1)], expireAfterSeconds=365 * 24 * 60 * 60) 
        logger.info("TTL index on 'users' collection created/updated.")
    except Exception as e:
        logger.critical(f"Failed to connect to MongoDB or set up indexes in bot.py: {e}")
        # Re-raise the exception to be caught by the calling function (initialize_telegram_bot_application)
        raise 

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

active_games = {} # Stores current game state for each chat_id

# --- Helper Functions ---

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
            # MongoDB stores keys as strings in dictionaries, so group_id needs to be stringified
            return list(users_collection.find().sort(f"group_scores.{group_id}", -1).limit(10))
        else:
            return list(users_collection.find().sort("total_score", -1).limit(10))
    except Exception as e:
        logger.error(f"Error getting leaderboard: {e}")
        return []

async def is_admin(chat_id: int, user_id: int, bot_instance: Application.bot):
    try:
        # Get chat members to check admin status
        chat_member = await bot_instance.get_chat_member(chat_id, user_id)
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
            # Remove timer_task before saving, as it's not JSON serializable
            # We recreate it on load if necessary, or let auto-end handle it
            temp_game_state = active_games[chat_id].copy()
            if "timer_task" in temp_game_state:
                del temp_game_state["timer_task"]

            game_states_collection.update_one(
                {"_id": chat_id}, # Use chat_id as the unique ID for the game state
                {"$set": temp_game_state},
                upsert=True # Create if not exists, update if exists
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
        # Re-initialize timer tasks if necessary for long-running games after restart
        # Note: If telegram_app._bot is not available yet, this might cause issues on initial load.
        # This part assumes `telegram_app` and `telegram_app._bot` are available when called.
        # A safer approach would be to pass `bot_instance` to `load_game_state_from_db`
        # or call it after `application.build()`
        # For now, we'll keep it as is, expecting `initialize_telegram_bot_application` to manage this.
        for chat_id, game_state in active_games.items():
            if game_state.get("status") == "in_progress":
                # For simplicity, if a timer was active, restart the auto-end timer
                # More complex games like WordChain might need to re-evaluate their turn timers
                # Ensure `telegram_app` is available and has `_bot` attribute here
                # We defer starting these tasks until we have the `bot_instance` for sure.
                pass # We will handle task re-creation after the bot application is fully built and started.
        logger.info(f"Loaded {len(active_games)} active games from DB. Timer tasks will be re-initialized after bot start.")
    except Exception as e:
        logger.error(f"Error loading game states from DB: {e}")


# --- Command Handlers ---
async def start_command(update: Update, context: Application.bot): 
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
            # Use context.bot (Update.effective_chat.bot is more robust in handlers)
            await context.bot.send_message(chat_id=LOG_CHANNEL_ID, text=log_message, parse_mode="Markdown")
            logger.info(f"Sent log message to channel {LOG_CHANNEL_ID}.")
        except Exception as e:
            logger.error(f"Failed to send log message to channel {LOG_CHANNEL_ID}: {e}")

async def games_command(update: Update, context: Application.bot):
    keyboard = []
    for game_name, game_callback_data in GAMES_LIST:
        keyboard.append([InlineKeyboardButton(game_name, callback_data=f"show_rules_{game_callback_data}")])
    
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text("Kaun sa game khelna chahte ho?", reply_markup=reply_markup)

async def broadcast_command(update: Update, context: Application.bot):
    if update.effective_user.id != ADMIN_USER_ID:
        await update.message.reply_text("Tum is command ko use nahi kar sakte.")
        return

    if not context.args:
        await update.message.reply_text("Please message content provide karo broadcast ke liye.")
        return
    
    message_content = " ".join(context.args)
    
    sent_count = 0
    failed_count = 0
    
    if groups_collection:
        # Use find() to iterate, not find_one()
        all_groups = groups_collection.find({"active": True})
        
        for group in all_groups:
            try:
                # Use context.bot for sending message inside handler
                await context.bot.send_message(chat_id=group["_id"], text=message_content)
                sent_count += 1
                await asyncio.sleep(0.1) # Small delay to avoid hitting Telegram API limits
            except Exception as e:
                logger.error(f"Could not send message to group {group['_id']}: {e}")
                failed_count += 1
                # If bot is blocked or chat not found, mark group as inactive
                if "chat not found" in str(e).lower() or "bot was blocked by the user" in str(e).lower():
                    groups_collection.update_one({"_id": group["_id"]}, {"$set": {"active": False}})
    else:
        logger.warning("Groups collection not initialized, cannot broadcast.")

    await update.message.reply_text(f"Broadcast complete. Sent to {sent_count} groups, {failed_count} failed.")

async def endgame_command(update: Update, context: Application.bot):
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id

    # Check if the user is an admin in the current group
    if not await is_admin(chat_id, user_id, context.bot): 
        await update.message.reply_text("Sirf group admin hi game end kar sakte hain.")
        return

    if chat_id in active_games:
        game_state = active_games[chat_id]
        if game_state.get("timer_task"):
            game_state["timer_task"].cancel() # Cancel any pending game timers
            logger.info(f"Cancelled timer task for group {chat_id}.")
        del active_games[chat_id] # Remove game from active games
        if game_states_collection:
            game_states_collection.delete_one({"_id": chat_id}) # Remove game state from DB
        await update.message.reply_text("Game khatam kar diya gaya hai.")
        logger.info(f"Game ended for group {chat_id} by admin.")
    else:
        await update.message.reply_text("Iss group mein koi active game nahi hai.")

async def leaderboard_command(update: Update, context: Application.bot):
    # Determine if it's a group or private chat for group-specific leaderboard
    group_id = update.effective_chat.id if update.effective_chat.type in ["group", "supergroup"] else None
    
    if group_id:
        group_leaders = await get_leaderboard(group_id)
        if group_leaders:
            group_lb_text = "**Iss Group Ke Top Khiladi:**\n"
            for i, user_data in enumerate(group_leaders):
                # Ensure group_scores key exists and retrieve the score for the current group
                # Convert group_id to string for dictionary key lookup
                score = user_data.get('group_scores', {}).get(str(group_id), 0)
                group_lb_text += f"{i+1}. {user_data.get('username', 'Unknown')} - {score} points\n"
        else:
            group_lb_text = "**Iss group mein abhi koi score nahi.**"
        await update.message.reply_text(group_lb_text, parse_mode="Markdown")

    # Always show worldwide leaderboard
    world_leaders = await get_leaderboard()
    if world_leaders:
        world_lb_text = "\n\n**Worldwide Top Khiladi:**\n"
        for i, user_data in enumerate(world_leaders):
            world_lb_text += f"{i+1}. {user_data.get('username', 'Unknown')} - {user_data.get('total_score', 0)} points\n"
    else:
        world_lb_text = "\n\n**Worldwide abhi koi score nahi.**"
    await update.message.reply_text(world_lb_text, parse_mode="Markdown")

async def mystats_command(update: Update, context: Application.bot):
    user_id = update.effective_user.id
    if users_collection:
        user_data = users_collection.find_one({"user_id": user_id})
    else:
        user_data = None
        logger.warning("Users collection not initialized, cannot fetch user stats.")

    if user_data:
        stats_text = f"**{user_data.get('username', 'Tumhare')} Stats:**\n" \
                     f"Total Score: {user_data.get('total_score', 0)} points\n"
        
        # Display group-wise scores if available
        if user_data.get('group_scores') and groups_collection:
            stats_text += "\n**Group-wise Scores:**\n"
            for group_id_str, score in user_data['group_scores'].items():
                # Fetch group name from groups_collection
                try:
                    group_info = groups_collection.find_one({"_id": int(group_id_str)})
                    group_name = group_info.get("name", f"Group ID: {group_id_str}") if group_info else f"Group ID: {group_id_str}"
                    stats_text += f"- {group_name}: {score} points\n"
                except ValueError: # In case group_id_str is not a valid int
                    stats_text += f"- Invalid Group ID ({group_id_str}): {score} points\n"
        
        await update.message.reply_text(stats_text, parse_mode="Markdown")
    else:
        await update.message.reply_text("Tumne abhi tak koi game nahi khela hai ya tumhara data nahi mila.")

# --- Callback Query Handlers ---
async def button_handler(update: Update, context: Application.bot):
    query = update.callback_query
    await query.answer() # Always answer the callback query to remove loading state
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

        # Initialize game state
        active_games[chat_id] = {
            "game_type": game_type_code,
            "players": [],
            "status": "waiting_for_players",
            "current_round": 0,
            "timer_task": None, # To hold the asyncio task for game timers
            "last_activity_time": datetime.utcnow() # Track last activity for auto-end
        }
        await save_game_state_to_db(chat_id)
        
        await query.edit_message_text(f"**{next((name for name, code in GAMES_LIST if code == game_type_code), 'Game')} Shuru Ho Raha Hai!**\n\n1 minute mein game shuru hoga. Judne ke liye 'Mein Khelunga' button dabao.\n\n**Khiladi:**",
                                      parse_mode="Markdown",
                                      reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Mein Khelunga!", callback_data=f"join_game_{chat_id}")]]))
        
        # Start a countdown for the game
        # `context.bot` works in handlers, for outside or global access use `telegram_app._bot`
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
            
            # Update the message to show joined players
            player_list_text = "\n".join([p["username"] for p in active_games[game_id]["players"]])
            try:
                await query.edit_message_text(f"**{next((name for name, code in GAMES_LIST if code == active_games[game_id]['game_type']), 'Game')} Shuru Ho Raha Hai!**\n\n1 minute mein game shuru hoga. Judne ke liye 'Mein Khelunga' button dabao.\n\n**Khiladi:**\n{player_list_text}",
                                              parse_mode="Markdown",
                                              reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Mein Khelunga!", callback_data=f"join_game_{game_id}")]]))
            except Exception as e:
                # Message might have been edited by another player, just send a new message
                await context.bot.send_message(chat_id=game_id, text=f"{user.full_name} ne game join kar liya hai. Ab kul khiladi: {len(active_games[game_id]['players'])}",
                                               reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Mein Khelunga!", callback_data=f"join_game_{game_id}")]]))

            await query.answer(f"{user.first_name} ne game join kar liya hai!")
            logger.info(f"User {user.full_name} joined game in group {game_id}.")
        else:
            await query.answer("Tum pehle hi game mein shamil ho.", show_alert=True)


# --- Game Logic Functions ---
async def start_game_countdown(chat_id: int, game_type_code: str, message_to_edit: Update.message, bot_instance: Application.bot):
    await asyncio.sleep(60) # Wait for 60 seconds for players to join
    
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
        
        # Start the specific game
        if game_type_code == "quiz":
            await start_quiz_game(chat_id, bot_instance)
        elif game_type_code == "wordchain":
            await start_wordchain_game(chat_id, bot_instance)
        elif game_type_code == "guessing":
            await start_guessing_game(chat_id, bot_instance)
        elif game_type_code == "number_guessing":
            await start_number_guessing_game(chat_id, bot_instance)
    
    # Start auto-end timer for general inactivity after initial game start
    if chat_id in active_games: # Ensure game is still active before starting auto-end timer
        active_games[chat_id]["timer_task"] = asyncio.create_task(auto_end_game_timer(chat_id, bot_instance))


async def auto_end_game_timer(chat_id: int, bot_instance: Application.bot):
    # This timer should run in a loop to periodically check inactivity
    while chat_id in active_games and active_games[chat_id]["status"] == "in_progress":
        game_state = active_games.get(chat_id)
        if not game_state:
            break # Game might have ended elsewhere

        last_activity = game_state.get("last_activity_time", datetime.utcnow())
        time_since_last_activity = (datetime.utcnow() - last_activity).total_seconds()

        # Check for inactivity threshold (e.g., 5 minutes = 300 seconds)
        if time_since_last_activity >= 300: 
            await bot_instance.send_message(chat_id=chat_id, text="Game mein 5 minute se koi activity nahi. Game khatam kar diya gaya.")
            # Clean up game state
            del active_games[chat_id]
            if game_states_collection:
                game_states_collection.delete_one({"_id": chat_id})
            logger.info(f"Game auto-ended due to inactivity in group {chat_id}.")
            break # Exit the loop as game is ended
        
        # If active, sleep for a shorter interval (e.g., 30 seconds) and recheck
        await asyncio.sleep(30) 
    
    logger.info(f"Auto-end timer for group {chat_id} stopped.")


quiz_questions_cache = {} # Cache for quiz questions per chat to avoid refetching

async def start_quiz_game(chat_id: int, bot_instance: Application.bot):
    questions = await get_channel_content("quiz")
    if not questions:
        await bot_instance.send_message(chat_id=chat_id, text="Quiz questions nahi mil paaye. Game abhi shuru nahi ho sakta.")
        del active_games[chat_id]
        if game_states_collection:
            game_states_collection.delete_one({"_id": chat_id})
        logger.warning(f"Quiz game failed to start in {chat_id}: no questions.")
        return
    
    # Select a subset of questions for the game
    quiz_questions_cache[chat_id] = random.sample(questions, min(len(questions), 10)) # Max 10 questions per game
    active_games[chat_id]["current_round"] = 0
    active_games[chat_id]["quiz_data"] = quiz_questions_cache[chat_id]
    active_games[chat_id]["current_question"] = {} # To store details of the current question/poll
    active_games[chat_id]["answered_this_round"] = False # To prevent multiple answers in text-based quiz
    active_games[chat_id]["last_activity_time"] = datetime.utcnow() # Reset activity timer
    await save_game_state_to_db(chat_id)

    # Start the quiz round by sending the first question
    active_games[chat_id]["timer_task"] = asyncio.create_task(
        send_next_quiz_question_with_timer(chat_id, bot_instance)
    )

async def send_next_quiz_question_with_timer(chat_id: int, bot_instance: Application.bot):
    while chat_id in active_games and active_games[chat_id]["status"] == "in_progress" and active_games[chat_id]["game_type"] == "quiz":
        game_state = active_games.get(chat_id)
        if not game_state:
            break # Game ended
        
        if game_state["current_round"] >= len(game_state["quiz_data"]):
            await bot_instance.send_message(chat_id=chat_id, text="Quiz khatam! Sabhi sawal pooche ja chuke hain.")
            del active_games[chat_id]
            if game_states_collection:
                game_states_collection.delete_one({"_id": chat_id})
            logger.info(f"Quiz game ended in group {chat_id}: all questions asked.")
            break # Exit loop
        
        question_data = game_state["quiz_data"][game_state["current_round"]]
        
        if "options" in question_data and question_data["options"]:
            # Send as a poll if options are provided
            message = await bot_instance.send_poll(
                chat_id=chat_id,
                question=question_data["text"],
                options=question_data["options"],
                is_anonymous=False, # Make poll non-anonymous so we can track user answers
                type='quiz', # Specific quiz poll type
                correct_option_id=question_data["correct_option_id"],
                explanation=question_data.get("explanation", ""),
                open_period=20 # Poll will be open for 20 seconds
            )
            game_state["current_question"] = {
                "type": "poll",
                "message_id": message.message_id,
                "poll_id": message.poll.id,
                "correct_answer_text": question_data["options"][question_data["correct_option_id"]], # Store correct answer for logging/reference
                "correct_option_id": question_data["correct_option_id"]
            }
        else:
            # Send as a text question if no options (direct answer expected)
            await bot_instance.send_message(chat_id=chat_id, text=f"**Sawal {game_state['current_round'] + 1}:**\n\n{question_data['text']}", parse_mode="Markdown")
            game_state["current_question"] = {
                "type": "text",
                "correct_answer": question_data["answer"].lower() # Store correct answer (lowercase for comparison)
            }
        
        game_state["answered_this_round"] = False # Reset for the new question
        game_state["last_activity_time"] = datetime.utcnow() # Update activity time
        await save_game_state_to_db(chat_id)

        await asyncio.sleep(20) # Wait for 20 seconds for answers
        
        # After timer, check if answered (for text questions). For polls, Telegram closes them automatically.
        if game_state["current_question"].get("type") == "text" and not game_state["answered_this_round"]:
            await bot_instance.send_message(chat_id=chat_id, text=f"Samay samapt! Sahi jawab tha: **{game_state['current_question']['correct_answer'].upper()}**")

        game_state["current_round"] += 1
        # Loop continues to next question or breaks if game is over
    logger.info(f"Quiz question sending loop ended for group {chat_id}.")


async def handle_quiz_answer(update: Update, context: Application.bot):
    chat_id = update.effective_chat.id
    user = update.effective_user
    game_state = active_games.get(chat_id)

    if not game_state or game_state["game_type"] != "quiz" or game_state["status"] != "in_progress":
        return
    # Only allow answers from players who joined the game
    if user.id not in [p["user_id"] for p in game_state["players"]]:
        return 
    # This handler is for TEXT answers, not for POLL answers
    if game_state["current_question"].get("type") == "poll":
        return 

    # Prevent multiple correct answers for a single text-based question round
    if game_state["answered_this_round"]:
        await update.message.reply_text("Iss sawal ka jawab pehle hi diya ja chuka hai.", reply_to_message_id=update.message.message_id)
        return 

    current_q = game_state["current_question"]
    user_answer = update.message.text.lower()
    
    if user_answer == current_q["correct_answer"]:
        await update.message.reply_text(f"Sahi jawab, {user.first_name}! Tumhe 10 points mile.", reply_to_message_id=update.message.message_id)
        await update_user_score(user.id, user.full_name, chat_id, 10)
        game_state["answered_this_round"] = True # Mark round as answered
        game_state["last_activity_time"] = datetime.utcnow() # Update activity time
        await save_game_state_to_db(chat_id)
        logger.info(f"Quiz: User {user.full_name} gave correct answer in group {chat_id}.")
        # No explicit call to send_next_quiz_question_with_timer here, as it's handled by its own timer loop.
        # This only processes the current answer.

async def handle_poll_answer(update: Update, context: Application.bot):
    poll_answer = update.poll_answer
    user_id = poll_answer.user.id
    user_name = poll_answer.user.full_name

    # Iterate through active games to find which game this poll answer belongs to
    for chat_id, game_state in active_games.items():
        if game_state.get("game_type") == "quiz" and game_state.get("status") == "in_progress":
            current_q = game_state.get("current_question", {})
            if current_q.get("type") == "poll" and current_q.get("poll_id") == poll_answer.poll_id:
                
                # Ensure the user is a player in this game
                if user_id not in [p["user_id"] for p in game_state["players"]]:
                    logger.info(f"Poll answer from non-player {user_name} in group {chat_id}.")
                    return

                # Check if this user already answered correctly for this poll (optional, Telegram usually handles this for polls)
                if game_state["answered_this_round"]: # If someone already answered correctly
                     logger.info(f"Poll: Already answered this round in group {chat_id}.")
                     return
                
                correct_option_id = current_q.get("correct_option_id")
                # Check if the selected option is the correct one
                if correct_option_id is not None and correct_option_id in poll_answer.option_ids:
                    await context.bot.send_message(chat_id=chat_id, text=f"Sahi jawab, {user_name}! Tumhe 10 points mile.")
                    await update_user_score(user_id, user_name, chat_id, 10)
                    game_state["answered_this_round"] = True # Mark round as answered
                    game_state["last_activity_time"] = datetime.utcnow()
                    await save_game_state_to_db(chat_id)
                    logger.info(f"Quiz Poll: User {user_name} gave correct answer in group {chat_id}.")
                else:
                    await context.bot.send_message(chat_id=chat_id, text=f"Galat jawab, {user_name}!")
                    logger.info(f"Quiz Poll: User {user_name} gave wrong answer in group {chat_id}.")

                break # Found the game, exit loop

async def start_wordchain_game(chat_id: int, bot_instance: Application.bot):
    words_data = await get_channel_content("wordchain")
    if not words_data:
        await bot_instance.send_message(chat_id=chat_id, text="Word Chain words nahi mil paaye. Game abhi shuru nahi ho sakta.")
        del active_games[chat_id]
        if game_states_collection:
            game_states_collection.delete_one({"_id": chat_id})
        logger.warning(f"WordChain game failed to start in {chat_id}: no words.")
        return

    start_word_obj = random.choice(words_data)
    start_word = start_word_obj["question"].strip().lower() # Assuming 'question' field holds the word

    active_games[chat_id]["current_word"] = start_word
    active_games[chat_id]["turn_index"] = 0 # Index of the current player whose turn it is
    active_games[chat_id]["last_activity_time"] = datetime.utcnow() 
    await save_game_state_to_db(chat_id)
    
    player_count = len(active_games[chat_id]["players"])
    if player_count == 0:
        await bot_instance.send_message(chat_id=chat_id, text="Word Chain ke liye khiladi nahi hain. Game band.")
        del active_games[chat_id]
        if game_states_collection:
            game_states_collection.delete_one({"_id": chat_id})
        logger.info(f"Word Chain game ended in {chat_id}: no players after join.")
        return

    # Shuffle players to randomize starting turn
    random.shuffle(active_games[chat_id]["players"])

    current_player = active_games[chat_id]["players"][active_games[chat_id]["turn_index"]]

    await bot_instance.send_message(
        chat_id=chat_id,
        text=f"**Shabd Shrinkhala Shuru!**\n\nPehla shabd: **{start_word.upper()}**\n\n{current_player['username']} ki baari hai. '{start_word[-1].upper()}' se shuru hone wala shabd batao."
    )
    # Start the turn timer for the first player
    active_games[chat_id]["timer_task"] = asyncio.create_task(
        turn_timer(chat_id, 60, bot_instance, "wordchain") # 60 seconds per turn
    )
    logger.info(f"WordChain game started in group {chat_id}. First word: {start_word}")

async def handle_wordchain_answer(update: Update, context: Application.bot):
    chat_id = update.effective_chat.id
    user = update.effective_user
    game_state = active_games.get(chat_id)

    if not game_state or game_state["game_type"] != "wordchain" or game_state["status"] != "in_progress":
        return
    
    # Ensure game has players
    if not game_state["players"]:
        logger.warning(f"WordChain: No players found in game state for {chat_id}.")
        return

    current_player = game_state["players"][game_state["turn_index"]]
    # Ensure it's the current player's turn
    if user.id != current_player["user_id"]:
        await update.message.reply_text("Abhi tumhari baari nahi hai.", reply_to_message_id=update.message.message_id)
        return

    user_word = update.message.text.strip().lower()
    last_char_of_prev_word = game_state["current_word"][-1].lower()

    if user_word.startswith(last_char_of_prev_word) and len(user_word) > 1 and user_word.isalpha(): # Simple check for valid word
        # Correct answer
        await update_user_score(user.id, user.full_name, chat_id, 5)
        await update.message.reply_text(f"Sahi! '{user_word.upper()}' ab naya shabd hai. {user.first_name} ko 5 points mile.", reply_to_message_id=update.message.message_id)
        
        game_state["current_word"] = user_word
        game_state["turn_index"] = (game_state["turn_index"] + 1) % len(game_state["players"]) # Next player's turn
        game_state["last_activity_time"] = datetime.utcnow() 
        await save_game_state_to_db(chat_id)

        # Cancel current turn timer and start a new one for the next player
        if game_state.get("timer_task"):
            game_state["timer_task"].cancel()
        
        next_player = game_state["players"][game_state["turn_index"]]
        await context.bot.send_message(
            chat_id=chat_id,
            text=f"{next_player['username']} ki baari. '{user_word[-1].upper()}' se shuru hone wala shabd batao."
        )
        game_state["timer_task"] = asyncio.create_task(turn_timer(chat_id, 60, context.bot, "wordchain"))
        logger.info(f"WordChain: User {user.full_name} gave correct word '{user_word}' in group {chat_id}.")

    else:
        # Incorrect answer - player is eliminated
        await update.message.reply_text(f"Galat shabd! '{last_char_of_prev_word.upper()}' se shuru hona chahiye tha ya shabd valid nahi hai. {user.first_name} game se bahar ho gaya.", reply_to_message_id=update.message.message_id)
        
        # Remove player from the list
        game_state["players"] = [p for p in game_state["players"] if p["user_id"] != user.id]
        
        game_state["last_activity_time"] = datetime.utcnow() 
        await save_game_state_to_db(chat_id)

        if game_state.get("timer_task"):
            game_state["timer_task"].cancel()

        if len(game_state["players"]) < 2:
            await context.bot.send_message(chat_id=chat_id, text="Khel khatam! Sirf ek khiladi bacha hai ya koi nahi bacha.")
            del active_games[chat_id]
            if game_states_collection:
                game_states_collection.delete_one({"_id": chat_id})
            logger.info(f"WordChain game ended in {chat_id}: not enough players.")
        else:
            # Adjust turn_index if player was removed (to ensure it stays within bounds)
            # If the current turn_index is now out of bounds, reset it to 0
            if game_state["turn_index"] >= len(game_state["players"]):
                game_state["turn_index"] = 0
            
            next_player = game_state["players"][game_state["turn_index"]]
            await context.bot.send_message(
                chat_id=chat_id,
                text=f"{next_player['username']} ki baari. '{game_state['current_word'][-1].upper()}' se shuru hone wala shabd batao."
            )
            game_state["timer_task"] = asyncio.create_task(turn_timer(chat_id, 60, context.bot, "wordchain"))
            logger.info(f"WordChain: User {user.full_name} failed. Next turn for {next_player['username']} in group {chat_id}.")

async def turn_timer(chat_id: int, duration: int, bot_instance: Application.bot, game_type: str):
    await asyncio.sleep(duration)
    
    game_state = active_games.get(chat_id)
    # Check if game is still active and of the correct type
    if game_state and game_state["status"] == "in_progress" and game_state["game_type"] == game_type:
        
        if game_type == "wordchain":
            if not game_state["players"]:
                logger.warning(f"WordChain: No players found for timer in group {chat_id}.")
                return # Should have been handled by game end, but a safety check

            current_player = game_state["players"][game_state["turn_index"]]
            await bot_instance.send_message(chat_id=chat_id, text=f"{current_player['username']} ne jawab nahi diya! Woh game se bahar.")
            
            # Remove timed-out player
            game_state["players"].pop(game_state["turn_index"]) 
            game_state["last_activity_time"] = datetime.utcnow() 
            await save_game_state_to_db(chat_id)

            if len(game_state["players"]) < 2:
                await bot_instance.send_message(chat_id=chat_id, text="Khel khatam! Sirf ek khiladi bacha hai ya koi nahi bacha.")
                del active_games[chat_id]
                if game_states_collection:
                    game_states_collection.delete_one({"_id": chat_id})
                logger.info(f"WordChain game ended in {chat_id} due to timeout: not enough players.")
            else:
                # Adjust index if player was removed
                if game_state["turn_index"] >= len(game_state["players"]):
                    game_state["turn_index"] = 0 # Loop back to start if necessary
                next_player = game_state["players"][game_state["turn_index"]]
                await bot_instance.send_message(
                    chat_id=chat_id,
                    text=f"{next_player['username']} ki baari. '{game_state['current_word'][-1].upper()}' se shuru hone wala shabd batao."
                )
                # Start new timer for the next player's turn
                game_state["timer_task"] = asyncio.create_task(turn_timer(chat_id, duration, bot_instance, "wordchain"))
                logger.info(f"WordChain: {current_player['username']} timed out. Next turn for {next_player['username']} in group {chat_id}.")
        
        elif game_type == "guessing":
             # This timer is for the entire guessing round, if not guessed
            if not game_state["guessed_this_round"]:
                correct_answer = game_state["current_guess_item"]["answer"]
                await bot_instance.send_message(chat_id=chat_id, text=f"Samay samapt! Sahi jawab tha: **{correct_answer.upper()}**.")
            
            game_state["current_round"] += 1
            game_state["last_activity_time"] = datetime.utcnow() 
            await save_game_state_to_db(chat_id)
            # Move to next round
            active_games[chat_id]["timer_task"] = asyncio.create_task(send_next_guess_item(chat_id, bot_instance)) # This handles next round or game end
            logger.info(f"Guessing game round timed out in {chat_id}. Moving to next round.")


async def start_guessing_game(chat_id: int, bot_instance: Application.bot):
    guesses = await get_channel_content("guessing")
    if not guesses:
        await bot_instance.send_message(chat_id=chat_id, text="Guessing game content nahi mil paaya. Game abhi shuru nahi ho sakta.")
        del active_games[chat_id]
        if game_states_collection:
            game_states_collection.delete_one({"_id": chat_id})
        logger.warning(f"Guessing game failed to start in {chat_id}: no content.")
        return

    active_games[chat_id]["guessing_data"] = random.sample(guesses, min(len(guesses), 5)) # 5 rounds per game
    active_games[chat_id]["current_round"] = 0
    active_games[chat_id]["current_guess_item"] = {}
    active_games[chat_id]["attempts"] = {} # To track attempts per user per round
    active_games[chat_id]["guessed_this_round"] = False # To ensure only one correct guess per round
    active_games[chat_id]["last_activity_time"] = datetime.utcnow() 
    await save_game_state_to_db(chat_id)

    active_games[chat_id]["timer_task"] = asyncio.create_task(send_next_guess_item(chat_id, bot_instance))

async def send_next_guess_item(chat_id: int, bot_instance: Application.bot):
    game_state = active_games.get(chat_id)
    if not game_state or game_state["status"] != "in_progress" or game_state["game_type"] != "guessing":
        return

    if game_state["current_round"] >= len(game_state["guessing_data"]):
        await bot_instance.send_message(chat_id=chat_id, text="Guessing game khatam! Sabhi items guess kiye ja chuke hain.")
        del active_games[chat_id]
        if game_states_collection:
            game_states_collection.delete_one({"_id": chat_id})
        logger.info(f"Guessing game ended in group {chat_id}: all rounds complete.")
        return

    guess_item = game_state["guessing_data"][game_state["current_round"]]
    
    # Display the hint/question for the current round
    await bot_instance.send_message(chat_id=chat_id, text=f"**Round {game_state['current_round'] + 1}:**\n\nIs shabd/phrase ko guess karo: `{guess_item['question']}`", parse_mode="Markdown")
    
    game_state["current_guess_item"] = {
        "question": guess_item["question"],
        "answer": guess_item["answer"].lower()
    }
    game_state["guessed_this_round"] = False # Reset for new round
    # Initialize attempts for all current players
    game_state["attempts"] = {str(p["user_id"]): 0 for p in game_state["players"]} 
    game_state["last_activity_time"] = datetime.utcnow() 
    await save_game_state_to_db(chat_id)

    # Start a timer for this guessing round
    if game_state.get("timer_task"):
        game_state["timer_task"].cancel()
    # The `turn_timer` is now a generic timer that takes game_type
    game_state["timer_task"] = asyncio.create_task(turn_timer(chat_id, 60, bot_instance, "guessing")) # 60 seconds per round
    logger.info(f"Guessing game: new round {game_state['current_round']+1} started in {chat_id}.")

async def handle_guessing_answer(update: Update, context: Application.bot):
    chat_id = update.effective_chat.id
    user = update.effective_user
    game_state = active_games.get(chat_id)

    if not game_state or game_state["game_type"] != "guessing" or game_state["status"] != "in_progress":
        return
    if user.id not in [p["user_id"] for p in game_state["players"]]:
        return
    if game_state["guessed_this_round"]: # If someone already guessed correctly this round
        return 

    user_guess = update.message.text.strip().lower()
    correct_answer = game_state["current_guess_item"]["answer"]

    if user_guess == correct_answer:
        await update_user_score(user.id, user.full_name, chat_id, 15) # Award points
        await update.message.reply_text(f"Shandar, {user.first_name}! Tumne sahi guess kiya: **{correct_answer.upper()}**! Tumhe 15 points mile.", reply_to_message_id=update.message.message_id)
        game_state["guessed_this_round"] = True # Mark round as complete
        
        if game_state.get("timer_task"):
            game_state["timer_task"].cancel() # Cancel the round timer
        
        game_state["current_round"] += 1 # Move to next round
        game_state["last_activity_time"] = datetime.utcnow() 
        await save_game_state_to_db(chat_id)
        # Manually trigger next item, timer would have done it if not guessed
        active_games[chat_id]["timer_task"] = asyncio.create_task(send_next_guess_item(chat_id, context.bot))
        logger.info(f"Guessing game: User {user.full_name} guessed correctly in {chat_id}.")
    else:
        # Incorrect guess
        # Convert user.id to string for dictionary key consistency
        user_id_str = str(user.id)
        game_state["attempts"][user_id_str] = game_state["attempts"].get(user_id_str, 0) + 1 # Increment attempts
        await update.message.reply_text("Galat guess. Phir se koshish karo!", reply_to_message_id=update.message.message_id)
        game_state["last_activity_time"] = datetime.utcnow()
        await save_game_state_to_db(chat_id) # Save updated attempts
        logger.info(f"Guessing game: User {user.full_name} made wrong guess in {chat_id}. Attempts: {game_state['attempts'][user_id_str]}.")

async def start_number_guessing_game(chat_id: int, bot_instance: Application.bot):
    secret_number = random.randint(1, 100) # Choose a random number between 1 and 100
    active_games[chat_id]["secret_number"] = secret_number
    active_games[chat_id]["guesses_made"] = {} # To track number of guesses per user
    active_games[chat_id]["last_activity_time"] = datetime.utcnow() 
    await save_game_state_to_db(chat_id)
    
    await bot_instance.send_message(
        chat_id=chat_id,
        text=f"**Sankhya Anuamaan Shuru!**\n\nMaine 1 se 100 ke beech ek number socha hai. Guess karo!"
    )
    # Use the general auto-end game timer for inactivity
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
        if not (1 <= user_guess <= 100):
            await update.message.reply_text("Kripya 1 se 100 ke beech ek number type karein.", reply_to_message_id=update.message.message_id)
            return
    except ValueError:
        await update.message.reply_text("Kripya ek valid number type karein.", reply_to_message_id=update.message.message_id)
        return

    secret_number = game_state["secret_number"]
    
    # Increment guess count for the user
    # Convert user.id to string for dictionary key consistency with MongoDB if needed later
    user_id_str = str(user.id)
    game_state["guesses_made"][user_id_str] = game_state["guesses_made"].get(user_id_str, 0) + 1 
    game_state["last_activity_time"] = datetime.utcnow() 
    await save_game_state_to_db(chat_id)
    
    if user_guess == secret_number:
        guesses_count = game_state["guesses_made"][user_id_str]
        # Points awarded based on fewer guesses (e.g., 100 - (guesses * 5), min 10 points)
        points = 100 - (guesses_count * 5)
        points = max(10, points) # Minimum points
        await update_user_score(user.id, user.full_name, chat_id, points)
        await update.message.reply_text(f"Sahi jawab, {user.first_name}! Number **{secret_number}** tha! Tumhe {points} points mile. (Koshish: {guesses_count})", reply_to_message_id=update.message.message_id)
        
        if game_state.get("timer_task"):
            game_state["timer_task"].cancel()
        del active_games[chat_id] # End the game for this chat
        if game_states_collection:
            game_states_collection.delete_one({"_id": chat_id})
        logger.info(f"Number Guessing: User {user.full_name} won in group {chat_id}. Game ended.")

    elif user_guess < secret_number:
        await update.message.reply_text("Mera number isse **bada** hai.", reply_to_message_id=update.message.message_id)
    else: # user_guess > secret_number
        await update.message.reply_text("Mera number isse **chhota** hai.", reply_to_message_id=update.message.message_id)
    logger.info(f"Number Guessing: User {user.full_name} guessed {user_guess} in group {chat_id}.")


# --- Message Distribution ---
async def handle_text_messages(update: Update, context: Application.bot):
    chat_id = update.effective_chat.id
    game_state = active_games.get(chat_id)
    
    # Direct message to appropriate game handler if an active game is running
    if game_state and game_state["status"] == "in_progress":
        if game_state["game_type"] == "quiz":
            await handle_quiz_answer(update, context)
        elif game_state["game_type"] == "wordchain":
            await handle_wordchain_answer(update, context)
        elif game_state["game_type"] == "guessing":
            await handle_guessing_answer(update, context)
        elif game_state["game_type"] == "number_guessing":
            await handle_number_guess(update, context)


# --- Main Bot Initialization Function ---
async def initialize_telegram_bot_application():
    """Telegram Bot Application को initialize और configure करता है।"""
    logger.info("Initializing Telegram Bot Application...")
    
    # BOT_TOKEN और MONGO_URI की जाँच करें
    if not BOT_TOKEN:
        logger.critical("CRITICAL ERROR: BOT_TOKEN environment variable not set. Bot cannot start.")
        return None
    if not MONGO_URI:
        logger.critical("CRITICAL ERROR: MONGO_URI environment variable not set. Bot cannot start without MongoDB.")
        return None

    # MongoDB collections को initialize करें
    try:
        init_mongo_collections()
    except Exception as e:
        logger.critical(f"CRITICAL ERROR: Could not initialize MongoDB collections. Bot cannot start: {e}")
        return None # Return None to indicate failure

    application = Application.builder().token(BOT_TOKEN).build()
    
    # Application को initialize करें
    await application.initialize() 
    await load_game_state_from_db() # Load active game states from DB (for persistence across restarts)

    # Re-initialize timer tasks for active games after bot application is built and ready
    # This loop was previously inside load_game_state_from_db, but needs `bot_instance`
    for chat_id, game_state in active_games.items():
        if game_state.get("status") == "in_progress":
            # Pass the `application.bot` instance to the timer functions
            game_state["timer_task"] = asyncio.create_task(
                auto_end_game_timer(chat_id, application.bot) 
            )
            logger.info(f"Re-initialized auto-end timer for active game in group {chat_id} on bot restart.")


    # Handlers add karein
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("games", games_command))
    application.add_handler(CommandHandler("broadcast", broadcast_command))
    application.add_handler(CommandHandler("endgame", endgame_command))
    application.add_handler(CommandHandler("leaderboard", leaderboard_command))
    application.add_handler(CommandHandler("mystats", mystats_command))
    application.add_handler(CallbackQueryHandler(button_handler))
    # Message handler for all text messages that are not commands
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text_messages))
    application.add_handler(PollAnswerHandler(handle_poll_answer)) # Handler for poll answers
    
    logger.info("Telegram Bot Application initialized.")
    return application

