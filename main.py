# main.py

import multiprocessing # multiprocessing module import karein
import logging
import os
# asyncio को यहाँ इम्पोर्ट करने की ज़रूरत नहीं है अगर आप इसे सीधे main process में इस्तेमाल नहीं कर रहे हैं।
# लेकिन अगर आपके run_telegram_bot_process फंक्शन में asyncio.run() है, तो bot.py को
# asyncio इम्पोर्ट करना होगा। main.py में सीधे इसकी आवश्यकता नहीं है,
# लेकिन clarity के लिए इसे रहने दिया जा सकता है।
import time # time.sleep का उपयोग करने के लिए

from dotenv import load_dotenv

# Local files import karna
# सुनिश्चित करें कि 'bot.py' में 'run_bot' async function है
# और 'server.py' में 'run_server' synchronous function है
from bot import run_bot
from server import run_server

# Environment variables ko load karna
load_dotenv()

# Main logger setup
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

# Flask server ko alag process mein chalane ke liye function
def run_flask_process():
    """Flask server ko ek alag process mein run karta hai."""
    logger.info("Starting Flask server in a separate process...")
    try:
        run_server() # server.py mein run_server ek synchronous function hai
    except Exception as e:
        logger.critical(f"Flask server failed to start: {e}")
        os._exit(1) # Process ko exit kar de, poore application ko nahi

# Telegram Bot ko alag process mein chalane ke liye function
def run_telegram_bot_process():
    """Telegram Bot ko ek alag process mein run karta hai."""
    logger.info("Starting Telegram Bot in a separate process...")
    try:
        # यहाँ asyncio को इम्पोर्ट किया जाता है और run_bot को चलाया जाता है।
        # यह सुनिश्चित करता है कि Telegram bot एक नए, स्वतंत्र asyncio इवेंट लूप में चले।
        import asyncio
        asyncio.run(run_bot())
    except Exception as e:
        logger.critical(f"Telegram Bot failed to run: {e}")
        os._exit(1) # Process ko exit kar de, poore application ko nahi

if __name__ == "__main__":
    # यह ब्लॉक सुनिश्चित करता है कि कोड केवल तभी चले जब स्क्रिप्ट को सीधे चलाया जाए
    # न कि जब इसे किसी और मॉड्यूल में इम्पोर्ट किया जाए।
    # Multiprocessing के लिए यह ज़रूरी है।

    logger.info("Application starting...")

    # Flask server process start karein
    flask_process = multiprocessing.Process(target=run_flask_process)
    # daemon=True सेट करने से parent process बंद होने पर child process भी बंद हो जाएगा।
    # Production environments में आप इसे manage करना चाह सकते हैं।
    flask_process.daemon = True
    flask_process.start()
    logger.info("Flask server process started.")

    # Telegram Bot process start karein
    telegram_bot_process = multiprocessing.Process(target=run_telegram_bot_process)
    telegram_bot_process.daemon = True
    telegram_bot_process.start()
    logger.info("Telegram Bot process started.")

    try:
        # Main process को जीवित रखने के लिए, हम एक infinite loop का उपयोग करते हैं
        # जो CPU को व्यस्त नहीं करता। यह सुनिश्चित करता है कि daemon child processes चलते रहें।
        logger.info("Main application loop running. Press Ctrl+C to stop.")
        while True:
            time.sleep(1) # हर सेकंड एक बार रुकेगा, CPU usage कम रखेगा

    except KeyboardInterrupt:
        logger.info("Application interrupted by user (Ctrl+C detected).")
        # Processes को terminate करें जब user Ctrl+C दबाए
        # terminate() एक forceful termination है, जो cleanup ठीक से नहीं कर सकता।
        # हालांकि, daemon processes के लिए यह अक्सर स्वीकार्य होता है।
        flask_process.terminate()
        telegram_bot_process.terminate()
        
        # processes के खत्म होने का इंतज़ार करें (optional, cleanup ke liye)
        # अगर daemon=True है, तो ये कॉल हमेशा ब्लॉक नहीं करेंगे।
        flask_process.join()
        telegram_bot_process.join()
        
        logger.info("Application processes terminated.")
        os._exit(0) # Safely exit the main process

    except Exception as e:
        logger.critical(f"Unhandled exception in main application loop: {e}")
        # Processes को terminate करें अगर कोई unhandled exception हो
        flask_process.terminate()
        telegram_bot_process.terminate()
        flask_process.join()
        telegram_bot_process.join()
        os._exit(1) # Error ke saath exit
