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
    from bot import initialize_telegram_bot_application, logger # logger को भी इंपोर्ट करें
except ImportError:
    print("Error: bot.py not found or has import errors. Please ensure bot.py is in the same directory.")
    sys.exit(1)

# Flask app initialization
app = Flask(__name__)

# Flask server के लिए logging level set करें (optional)
# werkzeug Flask का internal WSGI server है
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
    # Bot application initialize हो चुकी है और चल रही है या नहीं, ये चेक करें
    # telegram_app.running प्रॉपर्टी बताती है कि बॉट का polling/webhook लूप चल रहा है या नहीं
    bot_status = telegram_app is not None and telegram_app.running
    return jsonify({"status": "healthy", "bot_running": bot_status}), 200

# --- Telegram Bot Startup in a separate Thread ---
async def run_telegram_bot_tasks():
    """Async function to run the Telegram Bot polling and keep it alive."""
    global telegram_app
    try:
        telegram_app = await initialize_telegram_bot_application()
        if telegram_app:
            await telegram_app.start() # Bot polling शुरू करें
            logger.info("Telegram Bot Polling started.")
            
            # `Application.start()` polling को शुरू करता है और इसे अपने आंतरिक इवेंट लूप में चलाता है।
            # इस थ्रेड को तब तक जीवित रखने के लिए जब तक मुख्य प्रोग्राम चलता है,
            # हम एक `asyncio.Future()` पर `await` करते हैं जो कभी खत्म नहीं होता है।
            # यह सुनिश्चित करता है कि थ्रेड का इवेंट लूप सक्रिय रहे और बॉट अपडेट प्राप्त करता रहे।
            await asyncio.Future() 
            
            logger.info("Telegram Bot Polling stopped (thread ending).")
            # graceful shutdown के लिए:
            await telegram_app.stop() 
            logger.info("Telegram Bot Application stopped.")
        else:
            logger.error("Telegram Bot Application failed to initialize. Bot will not run.")
    except asyncio.CancelledError:
        # यह तब होता है जब थ्रेड बंद हो जाता है (जैसे जब मुख्य प्रोग्राम बंद होता है)
        logger.info("Telegram Bot Polling task was cancelled.")
        if telegram_app:
            await telegram_app.stop()
            logger.info("Telegram Bot Application stopped gracefully after cancellation.")
    except Exception as e:
        logger.error(f"Error running Telegram Bot Polling tasks: {e}", exc_info=True) # exc_info=True से traceback भी दिखेगा

def start_bot_in_thread():
    """Wrapper function to start the Telegram Bot in a new thread."""
    try:
        # प्रत्येक थ्रेड का अपना asyncio इवेंट लूप होना चाहिए
        new_loop = asyncio.new_event_loop()
        asyncio.set_event_loop(new_loop)
        new_loop.run_until_complete(run_telegram_bot_tasks())
    except Exception as e:
        logger.error(f"Error setting up asyncio loop for bot thread: {e}", exc_info=True)


# --- Main entry point ---
if __name__ == '__main__':
    # Environment variables load karein (जैसे .env से)
    try:
        from dotenv import load_dotenv
        load_dotenv() # .env file ko load karein
        logger.info(".env file loaded.")
    except ImportError:
        logger.warning("python-dotenv not installed. Environment variables must be set manually.")
    except Exception as e:
        logger.warning(f"Error loading .env file: {e}")


    # Bot को एक अलग thread में shuru karein
    # daemon=True यह सुनिश्चित करता है कि जब Flask मुख्य थ्रेड बंद हो जाए, तो बॉट थ्रेड भी स्वचालित रूप से बंद हो जाए
    bot_thread = threading.Thread(target=start_bot_in_thread, daemon=True)
    bot_thread.start()
    logger.info("Telegram Bot thread started.")

    # Flask server shuru karein
    logger.info("Starting Flask server...")
    port = int(os.getenv("PORT", 8000)) # PORT environment variable से load करे
    app.run(host='0.0.0.0', port=port, debug=False)

    # Note: `app.run()` एक blocking call है, isliye iske baad ka code server band hone par hi chalega.
    logger.info("Flask server stopped. Exiting application.")

