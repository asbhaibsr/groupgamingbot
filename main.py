# main.py

import threading
import asyncio
import logging
import os
from dotenv import load_dotenv # Naya import

# Local files import karna
from bot import run_bot
from server import run_server

# Environment variables ko load karna
load_dotenv() # .env file se variables load karega

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

async def main():
    """Main function jo Flask server aur bot ko run karta hai."""
    
    flask_thread = threading.Thread(target=start_flask_server_in_thread, daemon=True)
    flask_thread.start()
    logger.info("Flask server thread started.")

    await asyncio.sleep(5) 
    
    logger.info("Attempting to run Telegram Bot...")
    try:
        await run_bot()
    except Exception as e:
        logger.critical(f"Telegram Bot failed to run: {e}")
        os._exit(1) 

if __name__ == "__main__":
    logger.info("Application starting...")
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Application interrupted by user.")
    except Exception as e:
        logger.critical(f"Unhandled exception in main application loop: {e}")
