import os
import logging
import asyncio
import uuid
import re
from datetime import datetime, timedelta
import random

from flask import Flask
from threading import Thread

import telegram
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder, CommandHandler, ContextTypes, MessageHandler, filters, CallbackQueryHandler
)
from telegram.constants import ParseMode

from dotenv import load_dotenv

from database import MongoDB
from games import create_game, BaseGame, WordChainGame, GuessingGame, WordCorrectionGame

# Environment variables load karein
load_dotenv()

# --- Configuration ---
BOT_TOKEN = os.getenv("BOT_TOKEN")
MONGO_URI = os.getenv("MONGO_URI")
GAME_CHANNEL_ID = int(os.getenv("GAME_CHANNEL_ID")) if os.getenv("GAME_CHANNEL_ID") else 0
LOG_CHANNEL_ID = int(os.getenv("LOG_CHANNEL_ID")) if os.getenv("LOG_CHANNEL_ID") else 0
OWNER_USER_ID = int(os.getenv("OWNER_USER_ID")) if os.getenv("OWNER_USER_ID") else 0

# Game content storage limits
MAX_GAME_CONTENT_ENTRIES = 1000 # Max entries in game_content collection
DELETE_PERCENTAGE_ON_FULL = 0.50 # If 100% full, delete this percentage (e.g., 0.50 means 50%)

# Logger setup
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Flask App
app = Flask(__name__)

@app.route('/')
def health_check():
    """Koyeb health check ke liye simple endpoint."""
    return "Bot is running!", 200

# --- Global Variables ---
# db_manager ko yahan initialize karein taaki uska connection status check ho sake
db_manager = MongoDB()

# Active games ko track karne ke liye dictionary: {group_id: game_instance}
active_games = {}

# --- Helper Functions ---
async def send_log_message(context: ContextTypes.DEFAULT_TYPE, message: str):
    """Log channel par messages bhejta hai."""
    if LOG_CHANNEL_ID:
        try:
            await context.bot.send_message(chat_id=LOG_CHANNEL_ID, text=message)
        except Exception as e:
            logger.error(f"Failed to send log message to channel {LOG_CHANNEL_ID}: {e}")
    else:
        logger.warning("LOG_CHANNEL_ID not set, skipping log message.")

async def fetch_game_data_from_channel(context: ContextTypes.DEFAULT_TYPE, game_type: str):
    """
    MongoDB mein store kiye gaye message ID se specific game data ko Telegram channel se fetch karta hai.
    """
    if not GAME_CHANNEL_ID:
        logger.error("GAME_CHANNEL_ID not set. Cannot fetch game data from channel.")
        return None, None

    if not db_manager.connected: # Add this check here as well
        logger.error("MongoDB not connected. Cannot fetch game data.")
        return None, None

    try:
        # MongoDB se random game message ID prapt karein
        message_id_to_fetch = db_manager.get_random_game_message_id(game_type)
        if not message_id_to_fetch:
            logger.warning(f"No game content found in DB for type: {game_type}. Please add game content using /addgame.")
            return None, None

        # Telegram Bot API ka upyog karke message ko fetch karein
        message = await context.bot.get_message(chat_id=GAME_CHANNEL_ID, message_id=message_id_to_fetch)

        if message and message.text:
            # Game data format: /gametype\nque. [question]\nans. [answer]
            # Regular expression to match the format
            match = re.search(r"/(wordchain|guessing|wordcorrection)\nque\.\s*(.*?)\nans\.\s*(.*)", message.text, re.DOTALL | re.IGNORECASE)
            
            if match and match.group(1).lower() == game_type:
                question = match.group(2).strip()
                answer = match.group(3).strip()
                logger.info(f"Fetched game data from channel (via ID {message_id_to_fetch}): Type={game_type}, Q={question}, A={answer}")
                return question, answer
            else:
                logger.error(f"Message ID {message_id_to_fetch} se parsed data invalid format mein: {message.text[:50]}...")
                return None, None
        else:
            logger.error(f"Message ID {message_id_to_fetch} channel {GAME_CHANNEL_ID} mein nahi mila ya khali hai.")
            return None, None

    except telegram.error.BadRequest as e:
        if "message not found" in str(e).lower():
            logger.warning(f"Message ID {message_id_to_fetch} channel {GAME_CHANNEL_ID} mein nahi mila. Shayad delete ho gaya.")
        else:
            logger.error(f"Telegram API error while fetching message {message_id_to_fetch}: {e}")
        return None, None
    except Exception as e:
        logger.error(f"Error fetching game data from channel using stored message ID: {e}")
        return None, None

async def check_and_manage_game_content_storage(context: ContextTypes.DEFAULT_TYPE):
    """
    MongoDB game_content collection mein entries ki sankhya check karta hai.
    Agar ye MAX_GAME_CONTENT_ENTRIES tak pahunchta hai, to purani entries ko delete karta hai
    MongoDB aur Telegram channel dono se.
    """
    if not db_manager.connected: # Add this check here
        logger.error("MongoDB not connected. Skipping game content storage management.")
        return

    current_count = db_manager.get_game_content_count()
    if current_count >= MAX_GAME_CONTENT_ENTRIES:
        count_to_delete = int(MAX_GAME_CONTENT_ENTRIES * DELETE_PERCENTAGE_ON_FULL)
        if count_to_delete == 0: # Ensure at least 1 is deleted if count is small
            count_to_delete = 1
            
        logger.info(f"Game content collection {current_count}/{MAX_GAME_CONTENT_ENTRIES} entries tak pahunch gaya hai. {count_to_delete} oldest entries delete kar raha hu.")
        await send_log_message(context, f"Game content storage full. Deleting {count_to_delete} oldest entries.")

        telegram_message_ids_to_delete = db_manager.delete_oldest_game_content(count_to_delete)
        
        for msg_id in telegram_message_ids_to_delete:
            try:
                await context.bot.delete_message(chat_id=GAME_CHANNEL_ID, message_id=msg_id)
                logger.info(f"Deleted Telegram message ID {msg_id} from channel {GAME_CHANNEL_ID}.")
            except telegram.error.BadRequest as e:
                if "message can't be deleted" in str(e).lower() or "message not found" in str(e).lower():
                    logger.warning(f"Telegram message ID {msg_id} ko delete nahi kar paya (shayad pehle hi delete ho gaya ya admin permission nahi).")
                else:
                    logger.error(f"Error deleting Telegram message ID {msg_id} from channel: {e}")
            except Exception as e:
                logger.error(f"Unexpected error while deleting Telegram message ID {msg_id}: {e}")
        await send_log_message(context, f"{len(telegram_message_ids_to_delete)} game entries successfully deleted from channel and DB.")
    else:
        logger.info(f"Game content count: {current_count}/{MAX_GAME_CONTENT_ENTRIES}. No deletion needed.")


async def send_game_join_alerts(context: ContextTypes.DEFAULT_TYPE, game: BaseGame):
    # ... (यह फंक्शन पहले जैसा ही रहेगा) ...
    try:
        if game.status != "waiting_for_players":
            return

        current_time = asyncio.get_event_loop().time()
        time_left = int(game.join_window_end_time - current_time)
        chat_id = game.group_id

        if 60 >= time_left > 40:
            await context.bot.send_message(chat_id=chat_id, text=f"**{time_left} seconds remaining** to join the game! Use `/join`", parse_mode=ParseMode.MARKDOWN)
        elif 40 >= time_left > 20:
            await context.bot.send_message(chat_id=chat_id, text=f"**{time_left} seconds remaining** to join! Last call!", parse_mode=ParseMode.MARKDOWN)
        elif 20 >= time_left > 0:
            await context.bot.send_message(chat_id=chat_id, text=f"**{time_left} seconds remaining! Game starting soon!**", parse_mode=ParseMode.MARKDOWN)
        elif time_left <= 0 and game.status == "waiting_for_players":
            if len(game.players) >= 1:
                game.status = "in_progress"
                game.last_activity_time = current_time
                await context.bot.send_message(
                    chat_id=chat_id,
                    text=f"Time's up! Game **{game.__class__.__name__}** has started with {len(game.players)} players!\n"
                         f"First turn: **{game.get_current_player()['username']}**\n\n"
                         f"Sawal: {game.question}" + (f" (Current: `{game.get_display_word()}`)" if isinstance(game, GuessingGame) else "")
                )
                if db_manager.connected: # Save game state only if connected
                    db_manager.save_game_state(game.get_game_data_for_db())
                context.job_queue.run_once(
                    lambda ctx: check_turn_timeout(ctx, game.game_id),
                    game.turn_timeout,
                    data={"game_id": game.game_id, "chat_id": chat_id},
                    name=f"turn_timeout_{game.game_id}"
                )
            else:
                await context.bot.send_message(chat_id=chat_id, text="Not enough players joined. Game cancelled.")
                if chat_id in active_games:
                    del active_games[chat_id]
                if db_manager.connected: # Delete game state only if connected
                    db_manager.delete_game_state(game.game_id)
                await send_log_message(context, f"Game {game.game_id} in group {chat_id} cancelled due to no players.")
            return

        next_schedule_delay = min(time_left, 20)
        if next_schedule_delay > 0:
            context.job_queue.run_once(
                lambda ctx: send_game_join_alerts(ctx, game),
                next_schedule_delay,
                data={"game_id": game.game_id, "chat_id": chat_id},
                name=f"join_alert_{game.game_id}"
            )
        else:
            context.job_queue.run_once(
                lambda ctx: send_game_join_alerts(ctx, game),
                1,
                data={"game_id": game.game_id, "chat_id": chat_id},
                name=f"join_alert_{game.game_id}"
            )

    except Exception as e:
        logger.error(f"Error in send_game_join_alerts for game {game.game_id}: {e}")

async def check_turn_timeout(context: ContextTypes.DEFAULT_TYPE, game_id: str):
    # ... (यह फंक्शन पहले जैसा ही रहेगा) ...
    chat_id = context.job.data["chat_id"]
    if chat_id in active_games:
        game = active_games[chat_id]
        if game.status == "in_progress":
            time_since_last_activity = asyncio.get_event_loop().time() - game.last_activity_time
            if time_since_last_activity >= game.turn_timeout:
                current_player = game.get_current_player()
                if current_player:
                    await context.bot.send_message(
                        chat_id=chat_id,
                        text=f"**{current_player['username']}**, aapne jawab nahi diya! Aapki baari gayi."
                    )
                    game.next_turn()
                    game.last_activity_time = asyncio.get_event_loop().time()
                    if db_manager.connected: # Save game state only if connected
                        db_manager.save_game_state(game.get_game_data_for_db())
                    await context.bot.send_message(
                        chat_id=chat_id,
                        text=f"Agli baari **{game.get_current_player()['username']}** ki hai.\nSawal: {game.question}" + (f" (Current: `{game.get_display_word()}`)" if isinstance(game, GuessingGame) else ""),
                        parse_mode=ParseMode.MARKDOWN
                    )
                    for job in context.job_queue.get_jobs_by_name(f"turn_timeout_{game.game_id}"):
                        job.schedule_removal()
                    context.job_queue.run_once(
                        lambda ctx: check_turn_timeout(ctx, game.game_id),
                        game.turn_timeout,
                        data={"game_id": game.game_id, "chat_id": chat_id},
                        name=f"turn_timeout_{game.game_id}"
                    )
                else:
                    await context.bot.send_message(chat_id=chat_id, text="Game stuck: No current player found.")
                    await end_game_logic(context, chat_id, "stuck")
            else:
                remaining_time = game.turn_timeout - time_since_last_activity
                if remaining_time > 0:
                    context.job_queue.run_once(
                        lambda ctx: check_turn_timeout(ctx, game.game_id),
                        remaining_time + 1,
                        data={"game_id": game.game_id, "chat_id": chat_id},
                        name=f"turn_timeout_{game.game_id}"
                    )
    else:
        logger.info(f"Turn timeout job for game {game_id} cancelled as game no longer active.")
        for job in context.job_queue.get_jobs_by_name(f"turn_timeout_{game_id}"):
            job.schedule_removal()

async def end_game_logic(context: ContextTypes.DEFAULT_TYPE, chat_id: int, reason: str):
    # ... (यह फंक्शन पहले जैसा ही रहेगा) ...
    if chat_id in active_games:
        game = active_games[chat_id]
        game_id = game.game_id
        game_type = game.__class__.__name__

        await context.bot.send_message(chat_id=chat_id, text=f"Game **{game_type}** ({game_id}) khatm ho gaya hai! Reason: {reason}", parse_mode=ParseMode.MARKDOWN)

        if game.players:
            results_msg = "Game Results:\n"
            sorted_players = sorted(game.players, key=lambda p: p['score'], reverse=True)
            for i, player in enumerate(sorted_players):
                if db_manager.connected: # Update stats only if connected
                    db_manager.update_user_stats(
                        player['id'],
                        player['username'],
                        {"games_played": 1, "games_won": 1 if i == 0 else 0, "total_score": player['score']}
                    )
                results_msg += f"{i+1}. {player['username']}: {player['score']} points\n"
            await context.bot.send_message(chat_id=chat_id, text=results_msg)
        else:
            await context.bot.send_message(chat_id=chat_id, text="Khel mein koi player nahi tha.")

        if db_manager.connected: # Delete game state only if connected
            db_manager.delete_game_state(game_id)
        del active_games[chat_id]

        for job in context.job_queue.get_jobs_by_name(f"join_alert_{game_id}"):
            job.schedule_removal()
        for job in context.job_queue.get_jobs_by_name(f"turn_timeout_{game_id}"):
            job.schedule_removal()

        await send_log_message(context, f"Game {game_id} in group {chat_id} ended. Reason: {reason}")
    else:
        await context.bot.send_message(chat_id=chat_id, text="Koi active game nahi hai jise khatm kiya ja sake.")

# --- Command Handlers ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    # ... (यह फंक्शन पहले जैसा ही रहेगा) ...
    if update.effective_user is None:
        logger.warning("Start command received with no effective user.")
        return

    user = update.effective_user
    welcome_message = (
        f"Namaste **{user.first_name}**!\n\n"
        "Main aapka group gaming bot hu. Yahan aap mazedaar games khel sakte hain!\n"
        "Commands ki list ke liye `/games` type karein."
    )
    await update.message.reply_text(welcome_message, parse_mode=ParseMode.MARKDOWN)
    await send_log_message(context, f"User {user.id} ({user.username}) started the bot in chat {update.effective_chat.id}.")

async def games(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    # ... (यह फंक्शन पहले जैसा ही रहेगा) ...
    keyboard = [
        [InlineKeyboardButton("Wordchain Game", callback_data="start_game_wordchain")],
        [InlineKeyboardButton("Guessing Game", callback_data="start_game_guessing")],
        [InlineKeyboardButton("Word Correction Game", callback_data="start_game_wordcorrection")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    rules_message = (
        "**Games List & Rules:**\n\n"
        "1.  **Wordchain Game:** Ek shabd se shuru karein. Agla player pichhle shabd ke aakhri akshar se shuru hone wala naya shabd batayega.\n"
        "2.  **Guessing Game:** Chhupe hue shabd ko letters ya poora shabd guess karke dhundo.\n"
        "3.  **Word Correction Game:** Galat spelling wale shabd ko sahi karein.\n\n"
        "Kisi bhi game ko shuru karne ke liye niche diye gaye button par click karein."
    )
    await update.message.reply_text(rules_message, reply_markup=reply_markup, parse_mode=ParseMode.MARKDOWN)

async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    # ... (यह फंक्शन पहले जैसा ही रहेगा) ...
    query = update.callback_query
    await query.answer()

    chat_id = query.message.chat_id
    user = query.from_user

    if chat_id < 0: # Group chats ke liye
        if query.data.startswith("start_game_"):
            game_type = query.data.replace("start_game_", "")
            await start_new_game(update, context, game_type, chat_id)
        elif query.data == "join_game":
            await join_game(update, context, user)
    else: # Private chat ke liye
        await query.edit_message_text("Games sirf groups mein khele ja sakte hain.")

async def start_new_game(update: Update, context: ContextTypes.DEFAULT_TYPE, game_type: str, chat_id: int):
    """Naya game shuru karta hai."""
    if chat_id in active_games:
        await update.effective_message.reply_text("Is group mein pehle se ek game chal raha hai! Use `/endgame` se khatm karein.")
        return
    
    if not db_manager.connected: # Add this check
        await update.effective_message.reply_text("Database se connect nahi ho paya. Game shuru nahi kar sakte.")
        logger.error(f"Cannot start new game in group {chat_id}: MongoDB not connected.")
        return

    # Game channel se data fetch karein
    question, answer = await fetch_game_data_from_channel(context, game_type)
    if not question or not answer:
        await update.effective_message.reply_text("Game data nahi mil paya. Kripya channel mein game data sahi format mein add karein using `/addgame`.")
        await send_log_message(context, f"Failed to start game {game_type} in group {chat_id}: No data from channel/DB.")
        return

    game_id = str(uuid.uuid4())
    new_game = create_game(game_type, game_id, chat_id, question, answer)

    if new_game:
        active_games[chat_id] = new_game
        db_manager.save_game_state(new_game.get_game_data_for_db())

        join_button = InlineKeyboardButton("Game Join Karein", callback_data="join_game")
        reply_markup = InlineKeyboardMarkup([[join_button]])

        await update.effective_message.reply_text(
            new_game.get_initial_message(),
            reply_markup=reply_markup,
            parse_mode=ParseMode.MARKDOWN
        )
        await send_log_message(context, f"Game {game_type} ({game_id}) started in group {chat_id}.")

        context.job_queue.run_once(
            lambda ctx: send_game_join_alerts(ctx, new_game),
            1,
            data={"game_id": game_id, "chat_id": chat_id},
            name=f"join_alert_{game_id}"
        )
    else:
        await update.effective_message.reply_text("Invalid game type specified.")
        await send_log_message(context, f"Invalid game type '{game_type}' requested in group {chat_id}.")

async def join_game(update: Update, context: ContextTypes.DEFAULT_TYPE, user):
    # ... (यह फंक्शन पहले जैसा ही रहेगा) ...
    chat_id = update.effective_chat.id
    if chat_id in active_games:
        game = active_games[chat_id]
        if game.status == "waiting_for_players":
            if game.add_player(user.id, user.first_name):
                await update.effective_message.reply_text(f"**{user.first_name}** game mein jud gaya hai!", parse_mode=ParseMode.MARKDOWN)
                if db_manager.connected: # Save game state only if connected
                    db_manager.save_game_state(game.get_game_data_for_db())
            else:
                await update.effective_message.reply_text(f"**{user.first_name}**, aap pehle se hi game mein hain.", parse_mode=ParseMode.MARKDOWN)
        else:
            await update.effective_message.reply_text("Yeh game abhi join nahi kiya ja sakta ya shuru ho chuka hai.")
    else:
        await update.effective_message.reply_text("Is group mein koi active game nahi hai jise join kiya ja sake. `/games` se naya shuru karein.")

async def endgame(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    # ... (यह फंक्शन पहले जैसा ही रहेगा) ...
    chat_id = update.effective_chat.id
    await end_game_logic(context, chat_id, "Command se khatm kiya gaya")

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    # ... (यह फंक्शन पहले जैसा ही रहेगा) ...
    chat_id = update.effective_chat.id
    
    if update.effective_user is None:
        logger.warning(f"Message received in chat {chat_id} with no effective user. Ignoring.")
        return

    user_id = update.effective_user.id
    text = update.message.text

    if chat_id in active_games:
        game = active_games[chat_id]

        if game.status == "in_progress" and game.get_current_player()['id'] == user_id:
            if game.is_answer_correct(text):
                current_player = game.get_current_player()
                current_player['score'] += 10
                await update.message.reply_text(f"Sahi jawab, **{current_player['username']}**! Aapko 10 points mile hain.")
                
                if isinstance(game, GuessingGame) and game.get_display_word().replace(" ", "") == game.answer:
                    await update.message.reply_text(f"Shabd mil gaya! **{game.answer}**\n\nGame khatm!", parse_mode=ParseMode.MARKDOWN)
                    await end_game_logic(context, chat_id, "Sahi jawab")
                    return
                
                if isinstance(game, WordChainGame):
                    game.update_last_word(text)

                game.last_activity_time = asyncio.get_event_loop().time()
                game.next_turn()
                if db_manager.connected: # Save game state only if connected
                    db_manager.save_game_state(game.get_game_data_for_db())

                await update.message.reply_text(
                    f"Agli baari **{game.get_current_player()['username']}** ki hai.\n"
                    f"Sawal: {game.question}" + (f" (Current: `{game.get_display_word()}`)" if isinstance(game, GuessingGame) else ""),
                    parse_mode=ParseMode.MARKDOWN
                )
                for job in context.job_queue.get_jobs_by_name(f"turn_timeout_{game.game_id}"):
                    job.schedule_removal()
                context.job_queue.run_once(
                    lambda ctx: check_turn_timeout(ctx, game.game_id),
                    game.turn_timeout,
                    data={"game_id": game.game_id, "chat_id": chat_id},
                    name=f"turn_timeout_{game.game_id}"
                )

            else:
                await update.message.reply_text("Galat jawab. Koshish karte rahiye!")
                game.next_turn()
                game.last_activity_time = asyncio.get_event_loop().time()
                if db_manager.connected: # Save game state only if connected
                    db_manager.save_game_state(game.get_game_data_for_db())
                await update.message.reply_text(
                    f"Agli baari **{game.get_current_player()['username']}** ki hai.\n"
                    f"Sawal: {game.question}" + (f" (Current: `{game.get_display_word()}`)" if isinstance(game, GuessingGame) else ""),
                    parse_mode=ParseMode.MARKDOWN
                )
                for job in context.job_queue.get_jobs_by_name(f"turn_timeout_{game.game_id}"):
                    job.schedule_removal()
                context.job_queue.run_once(
                    lambda ctx: check_turn_timeout(ctx, game.game_id),
                    game.turn_timeout,
                    data={"game_id": game.game_id, "chat_id": chat_id},
                    name=f"turn_timeout_{game.game_id}"
                )

        elif game.status == "waiting_for_players":
            pass
        elif game.status == "ended":
            pass
        else:
            if game.get_current_player() and game.get_current_player()['id'] != user_id:
                await update.message.reply_text(f"Abhi **{game.get_current_player()['username']}** ki baari hai.", parse_mode=ParseMode.MARKDOWN)

async def my_stats(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    # ... (यह फंक्शन पहले जैसा ही रहेगा) ...
    if update.effective_user is None:
        logger.warning("My stats command received with no effective user.")
        await update.message.reply_text("Aapke stats display nahi kiye ja sakte kyunki user information available nahi hai.")
        return

    if not db_manager.connected: # Add this check
        await update.message.reply_text("Database se connect nahi ho paya. Stats retrieve nahi kar sakte.")
        logger.error("Cannot retrieve user stats: MongoDB not connected.")
        return

    user_id = update.effective_user.id
    username = update.effective_user.first_name

    stats = db_manager.get_user_stats(user_id)
    if stats:
        message = (
            f"**{username}'s Stats:**\n"
            f"Games Khele: {stats.get('games_played', 0)}\n"
            f"Games Jeete: {stats.get('games_won', 0)}\n"
            f"Sahi Jawab: {stats.get('correct_answers', 0)}\n"
            f"Total Score: {stats.get('total_score', 0)}"
        )
    else:
        message = f"**{username}**, aapne abhi tak koi game nahi khela hai."
    await update.message.reply_text(message, parse_mode=ParseMode.MARKDOWN)

async def leaderboard(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    # ... (यह फंक्शन पहले जैसा ही रहेगा) ...
    if not db_manager.connected: # Add this check
        await update.message.reply_text("Database se connect nahi ho paya. Leaderboard retrieve nahi kar sakte.")
        logger.error("Cannot retrieve leaderboard: MongoDB not connected.")
        return

    leaderboard_data = db_manager.get_leaderboard(limit=10, worldwide=True)

    if leaderboard_data:
        message = "**Global Leaderboard (Top 10):**\n"
        for i, user in enumerate(leaderboard_data):
            message += f"{i+1}. {user.get('username', 'N/A')}: {user.get('total_score', 0)} points ({user.get('games_won', 0)} wins)\n"
    else:
        message = "Leaderboard abhi khali hai. Khelna shuru karein!"
    await update.message.reply_text(message, parse_mode=ParseMode.MARKDOWN)

async def broadcast_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    # ... (यह फंक्शन पहले जैसा ही रहेगा) ...
    if update.effective_user is None:
        logger.warning("Broadcast command received with no effective user. Cannot check owner ID.")
        await update.message.reply_text("Broadcast command execute nahi ho sakta kyunki user information available nahi hai.")
        return

    if update.effective_user.id != OWNER_USER_ID:
        await update.message.reply_text("Aapke paas is command ko use karne ki permission nahi hai.")
        return

    if not context.args:
        await update.message.reply_text("Kripya broadcast karne ke liye message dein. Usage: `/broadcast <your message>`")
        return

    broadcast_text = " ".join(context.args)
    
    await update.message.reply_text("Broadcast functionality is a placeholder. It needs actual chat_id fetching logic from DB.")
    await send_log_message(context, f"Owner broadcast attempted: {broadcast_text}")

# --- NEW: Add Game Content Command ---
async def add_game_content_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Owner ke liye game content ko Telegram channel par post karne aur uski ID ko DB mein save karne ka command.
    Format: /addgame /gametype\nque. [question]\nans. [answer]
    """
    if update.effective_user.id != OWNER_USER_ID:
        await update.message.reply_text("Aapke paas is command ko use karne ki permission nahi hai.")
        return

    if not GAME_CHANNEL_ID:
        await update.message.reply_text("GAME_CHANNEL_ID .env mein set nahi hai. Game content add nahi kar sakte.")
        await send_log_message(context, "Attempt to add game content failed: GAME_CHANNEL_ID not set.")
        return
    
    if not db_manager.connected: # Add this check
        await update.message.reply_text("Database se connect nahi ho paya. Game content add nahi kar sakte.")
        logger.error("Cannot add game content: MongoDB not connected.")
        return

    full_message_text = update.message.text
    # Command part ko hata dein: "/addgame "
    game_data_text = full_message_text[len("/addgame "):].strip()

    if not game_data_text:
        await update.message.reply_text("Kripya game content format mein dein.\n"
                                        "Usage: `/addgame /gametype\\nque. [question]\\nans. [answer]`\n"
                                        "Example: `/addgame /wordchain\\nque. A_ P_ L_\\nans. APPLE`")
        return

    match = re.search(r"/(wordchain|guessing|wordcorrection)\nque\.\s*(.*?)\nans\.\s*(.*)", game_data_text, re.DOTALL | re.IGNORECASE)

    if match:
        game_type = match.group(1).lower()
        question = match.group(2).strip()
        answer = match.group(3).strip()

        try:
            # Game data ko game channel par post karein
            posted_message = await context.bot.send_message(
                chat_id=GAME_CHANNEL_ID,
                text=game_data_text,
                parse_mode=ParseMode.MARKDOWN # Agar aapke question/answer mein markdown hai
            )
            
            # Post ki gayi message ki ID ko MongoDB mein save karein
            game_doc = {
                "game_type": game_type,
                "question": question, # Sirf reference ke liye, bot ise sidha message se parse karega
                "answer": answer,     # Sirf reference ke liye
                "game_message_id": posted_message.message_id,
                "created_at": datetime.now() # Kab add kiya gaya
            }
            if db_manager.add_game_content(game_doc):
                await update.message.reply_text(f"Game content successfully added to channel and DB! Message ID: `{posted_message.message_id}`")
                await send_log_message(context, f"Game content added by owner {update.effective_user.id}: Type={game_type}, Msg ID={posted_message.message_id}")
                
                # Check for storage limit after adding
                await check_and_manage_game_content_storage(context)
            else:
                await update.message.reply_text("Game content add karne mein error aayi (MongoDB issue).")
                await send_log_message(context, f"Failed to add game content to DB for owner {update.effective_user.id}.")

        except telegram.error.BadRequest as e:
            if "not an administrator of the chat" in str(e).lower() or "bot is not a member of the channel" in str(e).lower():
                await update.message.reply_text("Bot channel mein admin nahi hai ya channel mein nahi hai. Kripya bot ko channel mein 'Post Messages' aur 'Delete Messages' permissions ke saath admin banayein.")
                await send_log_message(context, f"Bot lacks channel permissions for {GAME_CHANNEL_ID}: {e}")
            else:
                await update.message.reply_text(f"Telegram API error: {e}")
                await send_log_message(context, f"Telegram API error while adding game content: {e}")
        except Exception as e:
            await update.message.reply_text(f"An unexpected error occurred: {e}")
            await send_log_message(context, f"Unexpected error in add_game_content_command: {e}")
    else:
        await update.message.reply_text("Game content format invalid. Kripya sahi format use karein.\n"
                                        "Example: `/addgame /wordchain\\nque. A_ P_ L_\\nans. APPLE`")


# --- Bot Initialization ---
def run_bot():
    """Bot ko initialize aur start karta hai."""
    application = ApplicationBuilder().token(BOT_TOKEN).build()

    # Commands
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("games", games))
    application.add_handler(CommandHandler("endgame", endgame))
    application.add_handler(CommandHandler("join", join_game))
    application.add_handler(CommandHandler("mystats", my_stats))
    application.add_handler(CommandHandler("leaderboard", leaderboard))
    application.add_handler(CommandHandler("broadcast", broadcast_message))
    application.add_handler(CommandHandler("addgame", add_game_content_command)) # NEW Handler

    # Message and Callback Handlers
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    application.add_handler(CallbackQueryHandler(button_callback))
    
    # Reload existing game states on startup
    # Yeh part tabhi run karein jab db_manager.connected ho
    if db_manager.connected: # IMPORTANT: Added check here
        existing_games_collection = db_manager.get_collection("game_states")
        if existing_games_collection: # Ensure collection was retrieved successfully
            for game_data in existing_games_collection.find({}): # Now this is safe
                try:
                    game_instance = create_game(
                        game_data["game_type"],
                        game_data["_id"],
                        game_data["group_id"],
                        game_data["question"],
                        game_data["answer"]
                    )
                    if game_instance:
                        # Load remaining properties
                        game_instance.players = game_data.get("players", [])
                        game_instance.current_player_index = game_data.get("current_player_index", 0)
                        game_instance.status = game_data.get("status", "waiting_for_players")
                        game_instance.join_window_end_time = game_data.get("join_window_end_time", 0)
                        game_instance.last_activity_time = game_data.get("last_activity_time", 0)
                        game_instance.turn_timeout = game_data.get("turn_timeout", 30)

                        if game_instance.game_type == "wordchain": # Specific for wordchain
                            game_instance.last_word_played = game_data.get("last_word_played")
                        elif game_instance.game_type == "guessing": # Specific for guessing
                            game_instance.guessed_letters = set(game_data.get("guessed_letters", []))

                        active_games[game_instance.group_id] = game_instance
                        logger.info(f"Loaded active game {game_instance.game_id} in group {game_instance.group_id}.")
                        
                        # Re-schedule jobs if game is still active
                        if game_instance.status == "waiting_for_players":
                            context = ContextTypes.DEFAULT_TYPE(application=application, chat_id=game_instance.group_id)
                            application.job_queue.run_once(
                                lambda ctx: send_game_join_alerts(ctx, game_instance),
                                max(1, int(game_instance.join_window_end_time - asyncio.get_event_loop().time())),
                                data={"game_id": game_instance.game_id, "chat_id": game_instance.group_id},
                                name=f"join_alert_{game_instance.game_id}"
                            )
                        elif game_instance.status == "in_progress":
                            context = ContextTypes.DEFAULT_TYPE(application=application, chat_id=game_instance.group_id)
                            application.job_queue.run_once(
                                lambda ctx: check_turn_timeout(ctx, game_instance.game_id),
                                max(1, int(game_instance.turn_timeout - (asyncio.get_event_loop().time() - game_instance.last_activity_time))),
                                data={"game_id": game_instance.game_id, "chat_id": game_instance.group_id},
                                name=f"turn_timeout_{game_instance.game_id}"
                            )

                    else:
                        logger.error(f"Failed to create game instance for loaded data: {game_data}")
                except Exception as e:
                    logger.error(f"Error loading game state {game_data.get('_id')}: {e}")
        else:
            logger.warning("Could not retrieve 'game_states' collection on startup. Skipping game state reload.")
    else:
        logger.warning("MongoDB not connected. Skipping existing game states reload.")


    application.run_polling(allowed_updates=Update.ALL_TYPES)


# --- Flask Server aur Bot ko run karna ---
if __name__ == "__main__":
    if not BOT_TOKEN or not MONGO_URI:
        logger.error("Essential environment variables (BOT_TOKEN, MONGO_URI) are not set. Exiting.")
        exit(1)
        
    if not GAME_CHANNEL_ID or not LOG_CHANNEL_ID or not OWNER_USER_ID:
        logger.error("Essential channel/owner IDs (GAME_CHANNEL_ID, LOG_CHANNEL_ID, OWNER_USER_ID) are not set correctly. Please check .env file.")
        exit(1)

    # MongoDB connection check yahan pehle karein
    if not db_manager.connected:
        logger.error("Failed to connect to MongoDB. Exiting.")
        exit(1)
    
    flask_thread = Thread(target=lambda: app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080))))
    flask_thread.start()
    logger.info("Flask server started in a separate thread.")
    
    # Run the bot only if MongoDB connection is successful
    run_bot()

