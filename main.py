# main.py

import multiprocessing # multiprocessing module import karein
import logging
import os
import asyncio # asyncio bhi import karein, bhale hi seedhe use na karein main process mein
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
        # Process ko exit kar de, poore application ko nahi
        os._exit(1)

# Telegram Bot ko alag process mein chalane ke liye function
def run_telegram_bot_process():
    """Telegram Bot ko ek alag process mein run karta hai."""
    logger.info("Starting Telegram Bot in a separate process...")
    try:
        # run_bot() ek async function hai, ise seedhe asyncio.run() se chalayein
        # naye process mein iska apna main thread hoga
        asyncio.run(run_bot())
    except Exception as e:
        logger.critical(f"Telegram Bot failed to run: {e}")
        # Process ko exit kar de, poore application ko nahi
        os._exit(1)

if __name__ == "__main__":
    # यह ब्लॉक सुनिश्चित करता है कि कोड केवल तभी चले जब स्क्रिप्ट को सीधे चलाया जाए
    # न कि जब इसे किसी और मॉड्यूल में इम्पोर्ट किया जाए।
    # Multiprocessing के लिए यह ज़रूरी है।

    logger.info("Application starting...")

    # Flask server process start karein
    flask_process = multiprocessing.Process(target=run_flask_process)
    # daemon=True set karne se parent process band hone par child process bhi band ho jayega.
    # Production environments mein aap ise manage karna chah sakte hain.
    flask_process.daemon = True 
    flask_process.start()
    logger.info("Flask server process started.")

    # Telegram Bot process start karein
    telegram_bot_process = multiprocessing.Process(target=run_telegram_bot_process)
    telegram_bot_process.daemon = True
    telegram_bot_process.start()
    logger.info("Telegram Bot process started.")

    try:
        # Main process ko chalte rehne de, jab tak sub-processes chal rahe hain
        # या Ctrl+C ना दबाया जाए।
        # .join() method parent process को child processes के खत्म होने का इंतज़ार कराता है।
        # Daemon processes के लिए, .join() केवल तभी उपयोगी है जब आप चाहते हैं कि parent
        # child के खत्म होने का इंतज़ार करे; अन्यथा, parent के खत्म होने पर child भी खत्म हो जाएगा।
        # हम यहाँ एक infinite loop रखेंगे ताकि main process चलता रहे और daemon children को alive रखे।
        
        logger.info("Main application loop running. Press Ctrl+C to stop.")
        while True:
            # हर 1 घंटे में एक बार Sleep करें, ताकि CPU usage कम हो
            # और main process बस alive रहे।
            # आप इसे अपनी ज़रूरत के अनुसार बदल सकते हैं।
            asyncio.sleep(3600) 

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
