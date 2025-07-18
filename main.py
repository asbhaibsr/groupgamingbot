# main.py

import threading
import logging
import os
from dotenv import load_dotenv

# bot.py से मुख्य फंक्शन और logger इंपोर्ट करें
from bot import start_telegram_bot, logger, flask_app_instance

if __name__ == "__main__":
    # Environment variables load karein (जैसे .env से)
    try:
        load_dotenv() # .env file ko load karein
        logger.info(".env file loaded.")
    except ImportError:
        logger.warning("python-dotenv not installed. Environment variables must be set manually.")
    except Exception as e:
        logger.warning(f"Error loading .env file: {e}")

    logger.info("Starting Flask health check server in a separate thread...")
    # Flask app को एक अलग थ्रेड में चलाएं
    port = int(os.getenv("PORT", 8000))
    flask_thread = threading.Thread(target=lambda: flask_app_instance.run(host='0.0.0.0', port=port, debug=False))
    flask_thread.start()
    logger.info("Flask server thread started.")


    logger.info("Starting Pyrogram bot...")
    # Pyrogram bot को शुरू करें (यह blocking call है)
    start_telegram_bot()

    logger.info("Pyrogram bot stopped. Exiting application.")
    # End of bot code. Thank you for using! Made with ❤️ by @asbhaibsr
