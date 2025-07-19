import os
import logging
import asyncio
import uuid
import re
from datetime import datetime, timedelta

from flask import Flask
from threading import Thread

# Import telegram library correctly
import telegram
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder, CommandHandler, ContextTypes, MessageHandler, filters, CallbackQueryHandler # CallbackQueryHandler bhi import karein
)
from telegram.constants import ParseMode

from dotenv import load_dotenv

from database import MongoDB
from games import create_game, BaseGame, WordChainGame, GuessingGame, WordCorrectionGame

# Environment variables load karein
load_dotenv()

# --- Configuration ---
BOT_TOKEN = os.getenv("BOT_TOKEN")
MONGO_URI = os.getenv("MONGO_URI") # Already handled in database.py but good for clarity
GAME_CHANNEL_ID = int(os.getenv("GAME_CHANNEL_ID")) if os.getenv("GAME_CHANNEL_ID") else 0
LOG_CHANNEL_ID = int(os.getenv("LOG_CHANNEL_ID")) if os.getenv("LOG_CHANNEL_ID") else 0
OWNER_USER_ID = int(os.getenv("OWNER_USER_ID")) if os.getenv("OWNER_USER_ID") else 0

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
db_manager = MongoDB() # MongoDB instance banate hi connect() call hoga

# Active games ko track karne ke liye dictionary: {group_id: game_instance}
active_games = {}

# --- Helper Functions ---
async def send_log_message(context: ContextTypes.DEFAULT_TYPE, message: str):
    """Log channel par messages bhejta hai."""
    if LOG_CHANNEL_ID: # Ensure log channel ID is set
        try:
            await context.bot.send_message(chat_id=LOG_CHANNEL_ID, text=message)
        except Exception as e:
            logger.error(f"Failed to send log message to channel {LOG_CHANNEL_ID}: {e}")
    else:
        logger.warning("LOG_CHANNEL_ID not set, skipping log message.")


async def fetch_game_data_from_channel(context: ContextTypes.DEFAULT_TYPE, game_type: str):
    """
    Game channel se naya game data fetch karta hai.
    Format: /gametype - qus. [question] ans. [answer]
    NOTE: Yeh abhi hardcoded hai. Aapko yahan channel reading logic implement karna hoga.
    """
    try:
        if game_type == "wordchain":
            qus_list = ["A _ P _ L _", "B_N_N_", "C_T_ _ _ _"]
            ans_list = ["APPLE", "BANANA", "COMPUTER"]
        elif game_type == "guessing":
            qus_list = ["_____", "______", "_______"]
            ans_list = ["PYTHON", "JAVASCRIPT", "TELEGRAM"]
        elif game_type == "wordcorrection":
            qus_list = ["Telegrm", "Pythn", "Flaks"]
            ans_list = ["TELEGRAM", "PYTHON", "FLASK"]
        else:
            return None, None

        # Randomly choose one for now.
        idx = random.randint(0, len(qus_list) - 1)
        question = qus_list[idx]
        answer = ans_list[idx]

        logger.info(f"Fetched game data from (simulated) channel: Type={game_type}, Q={question}, A={answer}")
        return question, answer

    except Exception as e:
        logger.error(f"Error fetching game data from channel: {e}")
        return None, None

async def send_game_join_alerts(context: ContextTypes.DEFAULT_TYPE, game: BaseGame):
    """Game join hone ke alerts bhejta hai."""
    try:
        if game.status != "waiting_for_players":
            return # Agar game shuru ho gaya hai to alerts na bhejein

        # Abhi ka samay lein
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
            if len(game.players) >= 1: # Minimum 1 player to start
                game.status = "in_progress"
                game.last_activity_time = current_time
                await context.bot.send_message(
                    chat_id=chat_id,
                    text=f"Time's up! Game **{game.__class__.__name__}** has started with {len(game.players)} players!\n"
                         f"First turn: **{game.get_current_player()['username']}**\n\n"
                         f"Sawal: {game.question}"
                )
                db_manager.save_game_state(game.get_game_data_for_db())
                # Schedule the first turn timeout check
                context.job_queue.run_once(
                    lambda ctx: check_turn_timeout(ctx, game.game_id),
                    game.turn_timeout,
                    data={"game_id": game.game_id, "chat_id": chat_id},
                    name=f"turn_timeout_{game.game_id}"
                )
            else:
                await context.bot.send_message(chat_id=chat_id, text="Not enough players joined. Game cancelled.")
                if chat_id in active_games: # Ensure it exists before deleting
                    del active_games[chat_id]
                db_manager.delete_game_state(game.game_id)
                await send_log_message(context, f"Game {game.game_id} in group {chat_id} cancelled due to no players.")
            return # Job khatm

        # Schedule next alert (if needed)
        if game.status == "waiting_for_players":
            # Agla alert 20 second baad hoga, jab tak time_left positive ho
            next_schedule_delay = min(time_left, 20)
            if next_schedule_delay > 0:
                context.job_queue.run_once(
                    lambda ctx: send_game_join_alerts(ctx, game),
                    next_schedule_delay,
                    data={"game_id": game.game_id, "chat_id": chat_id},
                    name=f"join_alert_{game.game_id}"
                )
            else:
                # Agar delay zero ya negative hai, matlab time khatm ho gaya hai, turant call karein
                context.job_queue.run_once(
                    lambda ctx: send_game_join_alerts(ctx, game),
                    1, # Thoda sa delay de dein taaki event loop process kar sake
                    data={"game_id": game.game_id, "chat_id": chat_id},
                    name=f"join_alert_{game.game_id}"
                )


    except Exception as e:
        logger.error(f"Error in send_game_join_alerts for game {game.game_id}: {e}")

async def check_turn_timeout(context: ContextTypes.DEFAULT_TYPE, game_id: str):
    """Check karta hai ki player ne samay par jawab diya ya nahi."""
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
                    # Penalize or just move to next turn
                    game.next_turn()
                    game.last_activity_time = asyncio.get_event_loop().time()
                    db_manager.save_game_state(game.get_game_data_for_db())
                    await context.bot.send_message(
                        chat_id=chat_id,
                        text=f"Agli baari **{game.get_current_player()['username']}** ki hai.\nSawal: {game.question}"
                    )
                    # Schedule next timeout check
                    context.job_queue.run_once(
                        lambda ctx: check_turn_timeout(ctx, game.game_id),
                        game.turn_timeout,
                        data={"game_id": game.game_id, "chat_id": chat_id},
                        name=f"turn_timeout_{game.game_id}"
                    )
                else:
                    await context.bot.send_message(chat_id=chat_id, text="Game stuck: No current player found.")
                    await end_game_logic(context, chat_id, "stuck") # Force end
            else:
                # Still within timeout, re-schedule check for when timeout expires
                remaining_time = game.turn_timeout - time_since_last_activity
                if remaining_time > 0: # Sirf positive remaining time ke liye schedule karein
                    context.job_queue.run_once(
                        lambda ctx: check_turn_timeout(ctx, game.game_id),
                        remaining_time + 1, # Add a small buffer
                        data={"game_id": game.game_id, "chat_id": chat_id},
                        name=f"turn_timeout_{game.game_id}"
                    )
    else:
        logger.info(f"Turn timeout job for game {game_id} cancelled as game no longer active.")
        # Job ko cancel karein
        for job in context.job_queue.get_jobs_by_name(f"turn_timeout_{game_id}"):
            job.schedule_removal()

async def end_game_logic(context: ContextTypes.DEFAULT_TYPE, chat_id: int, reason: str):
    """Game ko khatm karne ka logic."""
    if chat_id in active_games:
        game = active_games[chat_id]
        game_id = game.game_id
        game_type = game.__class__.__name__

        await context.bot.send_message(chat_id=chat_id, text=f"Game **{game_type}** ({game_id}) khatm ho gaya hai! Reason: {reason}", parse_mode=ParseMode.MARKDOWN)

        # Leaderboard aur stats update karein
        if game.players:
            results_msg = "Game Results:\n"
            sorted_players = sorted(game.players, key=lambda p: p['score'], reverse=True)
            for i, player in enumerate(sorted_players):
                # Update user stats in DB
                db_manager.update_user_stats(
                    player['id'],
                    player['username'],
                    {"games_played": 1, "games_won": 1 if i == 0 else 0, "total_score": player['score']}
                )
                results_msg += f"{i+1}. {player['username']}: {player['score']} points\n"
            await context.bot.send_message(chat_id=chat_id, text=results_msg)
        else:
            await context.bot.send_message(chat_id=chat_id, text="Khel mein koi player nahi tha.")

        db_manager.delete_game_state(game_id) # Game data ko MongoDB se delete karein
        del active_games[chat_id] # Active games se hatayein

        # Saare pending jobs ko remove karein
        for job in context.job_queue.get_jobs_by_name(f"join_alert_{game_id}"):
            job.schedule_removal()
        for job in context.job_queue.get_jobs_by_name(f"turn_timeout_{game_id}"):
            job.schedule_removal()

        await send_log_message(context, f"Game {game_id} in group {chat_id} ended. Reason: {reason}")
    else:
        await context.bot.send_message(chat_id=chat_id, text="Koi active game nahi hai jise khatm kiya ja sake.")

# --- Command Handlers ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/start command."""
    user = update.effective_user
    welcome_message = (
        f"Namaste **{user.first_name}**!\n\n"
        "Main aapka group gaming bot hu. Yahan aap mazedaar games khel sakte hain!\n"
        "Commands ki list ke liye `/games` type karein."
    )
    await update.message.reply_text(welcome_message, parse_mode=ParseMode.MARKDOWN)
    await send_log_message(context, f"User {user.id} ({user.username}) started the bot in chat {update.effective_chat.id}.")

async def games(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/games command - Games ki list aur rules dikhata hai."""
    keyboard = [
        [InlineKeyboardButton("Wordchain Game", callback_data="start_game_wordchain")],
        [InlineKeyboardButton("Guessing Game", callback_data="start_game_guessing")],
        [InlineKeyboardButton("Word Correction Game", callback_data="start_game_wordcorrection")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    rules_message = (
        "**Games List & Rules:**\n\n"
        "1.  **Wordchain Game:** Ek shabd se shuru karein. Agla player pichhle shabd ke aakhri akshar se shuru hone wala naya shabd batayega. (Abhi ke liye, aapko 'ans' section mein diye gaye exact word ka anumaan lagana hoga.)\n"
        "2.  **Guessing Game:** Chhupe hue shabd ko letters ya poora shabd guess karke dhundo.\n"
        "3.  **Word Correction Game:** Galat spelling wale shabd ko sahi karein.\n\n"
        "Kisi bhi game ko shuru karne ke liye niche diye gaye button par click karein."
    )
    await update.message.reply_text(rules_message, reply_markup=reply_markup, parse_mode=ParseMode.MARKDOWN)

async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Inline button clicks ko handle karta hai."""
    query = update.callback_query
    await query.answer() # Callback query ko acknowledge karein

    chat_id = query.message.chat_id
    user = query.from_user

    if chat_id < 0: # Only allow games in groups
        if query.data.startswith("start_game_"):
            game_type = query.data.replace("start_game_", "")
            await start_new_game(update, context, game_type, chat_id)
        elif query.data == "join_game":
            await join_game(update, context, user)
    else:
        await query.edit_message_text("Games sirf groups mein khele ja sakte hain.")

async def start_new_game(update: Update, context: ContextTypes.DEFAULT_TYPE, game_type: str, chat_id: int):
    """Naya game shuru karta hai."""
    if chat_id in active_games:
        await update.effective_message.reply_text("Is group mein pehle se ek game chal raha hai! Use `/endgame` se khatm karein.")
        return

    # Game channel se data fetch karein
    question, answer = await fetch_game_data_from_channel(context, game_type)
    if not question or not answer:
        await update.effective_message.reply_text("Game data nahi mil paya. Kripya channel mein data check karein.")
        await send_log_message(context, f"Failed to start game {game_type} in group {chat_id}: No data from channel.")
        return

    game_id = str(uuid.uuid4()) # Unique game ID
    new_game = create_game(game_type, game_id, chat_id, question, answer)

    if new_game:
        active_games[chat_id] = new_game
        db_manager.save_game_state(new_game.get_game_data_for_db()) # Initial state save karein

        join_button = InlineKeyboardButton("Game Join Karein", callback_data="join_game")
        reply_markup = InlineKeyboardMarkup([[join_button]])

        await update.effective_message.reply_text(
            new_game.get_initial_message(),
            reply_markup=reply_markup,
            parse_mode=ParseMode.MARKDOWN
        )
        await send_log_message(context, f"Game {game_type} ({game_id}) started in group {chat_id}.")

        # Join alerts schedule karein
        context.job_queue.run_once(
            lambda ctx: send_game_join_alerts(ctx, new_game),
            1, # Pehla alert 1 sec baad (immediately trigger first check)
            data={"game_id": game_id, "chat_id": chat_id},
            name=f"join_alert_{game_id}"
        )
    else:
        await update.effective_message.reply_text("Invalid game type specified.")
        await send_log_message(context, f"Invalid game type '{game_type}' requested in group {chat_id}.")

async def join_game(update: Update, context: ContextTypes.DEFAULT_TYPE, user):
    """/join command ya button se game join karna."""
    chat_id = update.effective_chat.id
    if chat_id in active_games:
        game = active_games[chat_id]
        if game.status == "waiting_for_players":
            if game.add_player(user.id, user.first_name):
                await update.effective_message.reply_text(f"**{user.first_name}** game mein jud gaya hai!", parse_mode=ParseMode.MARKDOWN)
                db_manager.save_game_state(game.get_game_data_for_db()) # Update players list in DB
            else:
                await update.effective_message.reply_text(f"**{user.first_name}**, aap pehle se hi game mein hain.", parse_mode=ParseMode.MARKDOWN)
        else:
            await update.effective_message.reply_text("Yeh game abhi join nahi kiya ja sakta ya shuru ho chuka hai.")
    else:
        await update.effective_message.reply_text("Is group mein koi active game nahi hai jise join kiya ja sake. `/games` se naya shuru karein.")

async def endgame(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/endgame command - Current game ko khatm karta hai."""
    chat_id = update.effective_chat.id
    await end_game_logic(context, chat_id, "Command se khatm kiya gaya")

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Users ke messages ko handle karta hai (game answers ke liye)."""
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    text = update.message.text

    if chat_id in active_games:
        game = active_games[chat_id]

        if game.status == "in_progress" and game.get_current_player()['id'] == user_id:
            # Current player ki baari hai
            if game.is_answer_correct(text):
                current_player = game.get_current_player()
                current_player['score'] += 10 # Example score
                await update.message.reply_text(f"Sahi jawab, **{current_player['username']}**! Aapko 10 points mile hain.")
                
                # Agar Guessing Game hai aur shabd poora ho gaya
                if isinstance(game, GuessingGame) and game.get_display_word() == game.answer:
                    await update.message.reply_text(f"Shabd mil gaya! **{game.answer}**\n\nGame khatm!")
                    await end_game_logic(context, chat_id, "Sahi jawab")
                    return
                
                # Agar Wordchain Game hai toh agle shabd ki condition set karein
                if isinstance(game, WordChainGame):
                    game.update_last_word(text) # Update the last word for the chain

                game.last_activity_time = asyncio.get_event_loop().time() # Activity update karein
                game.next_turn()
                db_manager.save_game_state(game.get_game_data_for_db())

                await update.message.reply_text(
                    f"Agli baari **{game.get_current_player()['username']}** ki hai.\n"
                    f"Sawal: {game.question}" + (f" (Current: `{game.get_display_word()}`)" if isinstance(game, GuessingGame) else "")
                )
                # Reset turn timeout
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
                # Incorrect answer, but turn might not pass. Depends on game rules.
                # For now, turn passes on incorrect answer as well.
                # (You might want to change this logic based on game type)
                game.next_turn()
                game.last_activity_time = asyncio.get_event_loop().time()
                db_manager.save_game_state(game.get_game_data_for_db())
                await update.message.reply_text(
                    f"Agli baari **{game.get_current_player()['username']}** ki hai.\n"
                    f"Sawal: {game.question}" + (f" (Current: `{game.get_display_word()}`)" if isinstance(game, GuessingGame) else "")
                )
                # Reset turn timeout
                for job in context.job_queue.get_jobs_by_name(f"turn_timeout_{game.game_id}"):
                    job.schedule_removal()
                context.job_queue.run_once(
                    lambda ctx: check_turn_timeout(ctx, game.game_id),
                    game.turn_timeout,
                    data={"game_id": game.game_id, "chat_id": chat_id},
                    name=f"turn_timeout_{game.game_id}"
                )

        elif game.status == "waiting_for_players":
            # If game is waiting for players, regular messages don't affect it
            pass
        elif game.status == "ended":
            # Game is ended, ignore messages related to game
            pass
        else:
            # Not current player's turn
            if game.get_current_player() and game.get_current_player()['id'] != user_id:
                await update.message.reply_text(f"Abhi **{game.get_current_player()['username']}** ki baari hai.")
    # Agar group mein koi active game nahi hai, to messages ko ignore karein ya kuch aur karein.

async def my_stats(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/my_stats command - User ke personal statistics dikhata hai."""
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
    """/leaderboard command - Global leaderboard dikhata hai."""
    leaderboard_data = db_manager.get_leaderboard(limit=10, worldwide=True) # Top 10 users

    if leaderboard_data:
        message = "**Global Leaderboard (Top 10):**\n"
        for i, user in enumerate(leaderboard_data):
            message += f"{i+1}. {user.get('username', 'N/A')}: {user.get('total_score', 0)} points ({user.get('games_won', 0)} wins)\n"
    else:
        message = "Leaderboard abhi khali hai. Khelna shuru karein!"
    await update.message.reply_text(message, parse_mode=ParseMode.MARKDOWN)

async def broadcast_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/broadcast command - Owner ke liye messages broadcast karne ke liye."""
    if update.effective_user.id != OWNER_USER_ID:
        await update.message.reply_text("Aapke paas is command ko use karne ki permission nahi hai.")
        return

    if not context.args:
        await update.message.reply_text("Kripya broadcast karne ke liye message dein. Usage: `/broadcast <your message>`")
        return

    broadcast_text = " ".join(context.args)
    
    # NOTE: Is functionality ke liye, aapko apne database mein saare groups aur user IDs ko store karna hoga
    # jahan bot active hai. Phir un sabhi par iterate karke message bhejna hoga.
    # Abhi ke liye, yeh sirf ek placeholder hai aur sabhi chats par message nahi bhejega.
    # Real implementation mein aapko ek 'chats' collection banani padegi.
    
    await update.message.reply_text("Broadcast functionality is a placeholder. It needs actual chat_id fetching logic from DB.")
    await send_log_message(context, f"Owner broadcast attempted: {broadcast_text}")

# --- Bot Initialization ---
def run_bot():
    """Bot ko initialize aur start karta hai."""
    application = ApplicationBuilder().token(BOT_TOKEN).build()

    # Handlers
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("games", games))
    application.add_handler(CommandHandler("endgame", endgame))
    application.add_handler(CommandHandler("join", join_game)) # Direct /join command
    application.add_handler(CommandHandler("mystats", my_stats))
    application.add_handler(CommandHandler("leaderboard", leaderboard))
    application.add_handler(CommandHandler("broadcast", broadcast_message))

    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    application.add_handler(CallbackQueryHandler(button_callback)) # Inline button callbacks

    # Bot ko start karein
    logger.info("Starting Telegram bot polling...")
    application.run_polling(allowed_updates=Update.ALL_TYPES)


# --- Flask Server aur Bot ko run karna ---
if __name__ == "__main__":
    # Check for essential environment variables
    if not BOT_TOKEN or not MONGO_URI:
        logger.error("Essential environment variables (BOT_TOKEN, MONGO_URI) are not set. Exiting.")
        exit(1)
        
    if not GAME_CHANNEL_ID or not LOG_CHANNEL_ID or not OWNER_USER_ID: # Check if IDs are actually set
        logger.error("Essential channel/owner IDs (GAME_CHANNEL_ID, LOG_CHANNEL_ID, OWNER_USER_ID) are not set correctly. Please check .env file.")
        exit(1)

    # MongoDB connection test using the 'connected' attribute
    if not db_manager.connected: # Ab yeh check sahi hai
        logger.error("Failed to connect to MongoDB. Exiting.")
        exit(1)
    
    # Flask server ko alag thread mein chalao
    flask_thread = Thread(target=lambda: app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080))))
    flask_thread.start()
    logger.info("Flask server started in a separate thread.")

    # Bot ko run karein (blocking call)
    run_bot()

