# main.py

from flask import Flask, jsonify
import threading
import asyncio
import logging
import os
import sys

# bot.py से फंक्शन इंपोर्ट करें
# सुनिश्चित करें कि bot.py इस main.py के समान डायरेक्टरी में है
try:
    from bot import initialize_telegram_bot_application, logger
except ImportError:
    print("Error: bot.py not found or has import errors. Please ensure bot.py is in the same directory.")
    sys.exit(1)

# Flask app initialization
app = Flask(__name__)

# Flask server के लिए logging level set करें (optional)
log = logging.getLogger('werkzeug')
log.setLevel(logging.INFO)

# Telegram Bot application instance को स्टोर करने के लिए ग्लोबल वेरिएबल
telegram_app = None

# --- Flask Routes ---
@app.route('/')
def home():
    """Simple home endpoint."""
    return "Hello! Telegram Bot aur Flask server chal rahe hain."

@app.route('/healthz')
def health_check():
    """Health check endpoint jo server aur bot ki status batata hai."""
    bot_status = telegram_app is not None and telegram_app.running
    return jsonify({"status": "healthy", "bot_running": bot_status}), 200

# --- Telegram Bot Startup in a separate Thread ---
async def run_telegram_bot_polling_async():
    """Async function to run the Telegram Bot polling."""
    global telegram_app
    try:
        telegram_app = await initialize_telegram_bot_application()
        if telegram_app:
            await telegram_app.run_polling()
            logger.info("Telegram Bot Polling stopped.")
        else:
            logger.error("Telegram Bot Application failed to initialize.")
    except Exception as e:
        logger.error(f"Error running Telegram Bot Polling: {e}")

def start_bot_in_thread():
    """Wrapper function to start the Telegram Bot in a new thread."""
    # New event loop for the new thread
    try:
        new_loop = asyncio.new_event_loop()
        asyncio.set_event_loop(new_loop)
        new_loop.run_until_complete(run_telegram_bot_polling_async())
    except Exception as e:
        logger.error(f"Error starting Telegram Bot thread: {e}")

# --- Main entry point ---
if __name__ == '__main__':
    # Environment variables set karein (या .env से लोड करें)
    # यह सुनिश्चित करने के लिए कि bot.py भी उन्हें एक्सेस कर सके
    # अगर आप docker-compose या kubernetes में डिप्लॉय कर रहे हैं, तो ये environment variables
    # कंटेनर के startup पर ही सेट होंगे, आपको यहां load_dotenv() की आवश्यकता नहीं होगी।
    # For local development without docker-compose:
    try:
        from dotenv import load_dotenv
        load_dotenv() # .env file ko load karein
        logger.info(".env file loaded.")
    except ImportError:
        logger.warning("python-dotenv not installed. Environment variables must be set manually.")
    except Exception as e:
        logger.warning(f"Error loading .env file: {e}")


    # Bot को एक अलग thread में shuru karein
    bot_thread = threading.Thread(target=start_bot_in_thread, daemon=True) # daemon=True ताकी Flask बंद होने पर ये भी बंद हो जाए
    bot_thread.start()
    logger.info("Telegram Bot thread started.")

    # Flask server shuru karein
    logger.info("Starting Flask server...")
    port = int(os.getenv("PORT", 8000)) # PORT environment variable से load करे
    app.run(host='0.0.0.0', port=port, debug=False)

    # Note: `app.run()` blocking hai, isliye iske baad ka code server band hone par hi chalega.
    logger.info("Flask server stopped. Exiting application.")

