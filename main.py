# main.py

import threading
import asyncio
import logging
import os
from dotenv import load_dotenv

# Local files import karna
from bot import run_bot
from server import run_server

# Environment variables ko load karna
load_dotenv()

# Main logger setup
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

def start_flask_server_in_thread():
    """Flask server ko ek alag thread mein run karta hai."""
    logger.info("Starting Flask server in a separate thread...")
    try:
        run_server()
    except Exception as e:
        logger.critical(f"Flask server failed to start: {e}")
        os._exit(1) # Force exit

def start_telegram_bot_in_thread():
    """Telegram Bot ko ek alag thread mein run karta hai."""
    logger.info("Attempting to run Telegram Bot in a separate thread...")
    try:
        # run_bot() ek async function hai, isliye ise naye event loop mein chalayenge
        asyncio.run(run_bot()) 
    except Exception as e:
        logger.critical(f"Telegram Bot failed to run: {e}")
        os._exit(1)

async def main():
    """Main function jo Flask server aur bot ko run karta hai."""
    
    # Flask server thread start karein
    flask_thread = threading.Thread(target=start_flask_server_in_thread, daemon=True)
    flask_thread.start()
    logger.info("Flask server thread started.")

    # Telegram bot thread start karein
    # ab isko async function ke andar chalane ki zaroorat nahi
    telegram_bot_thread = threading.Thread(target=start_telegram_bot_in_thread, daemon=True)
    telegram_bot_thread.start()
    logger.info("Telegram Bot thread started.")

    # Application ko chalte rehne ke liye infinite loop ya kuch aur karein
    # Agar ye main function exit ho gaya, to daemon threads bhi band ho jayenge
    # Iske liye ya to KeyboardInterrupt ka wait karein, ya ek blocking call use karein
    # Ek simple infinite loop rakhte hain taaki main thread alive rahe
    while True:
        await asyncio.sleep(3600) # Har ghante check karein, ya CTRL+C ka wait karein

if __name__ == "__main__":
    logger.info("Application starting...")
    try:
        # main() ko seedhe asyncio.run() se chalayein
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Application interrupted by user.")
    except Exception as e:
        logger.critical(f"Unhandled exception in main application loop: {e}")

