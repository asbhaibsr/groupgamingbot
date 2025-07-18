# server.py

from flask import Flask, jsonify
import logging
import asyncio
import os # Added for PORT env var

# Flask app initialize karna
app = Flask(__name__)

# Logger setup for server
server_logger = logging.getLogger(__name__)
server_logger.setLevel(logging.INFO) 
handler = logging.StreamHandler()
formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
handler.setFormatter(formatter)
server_logger.addHandler(handler)


@app.route('/')
def home():
    """Basic home route."""
    server_logger.info("Home route accessed.")
    return "Telegram Game Bot is running!"

@app.route('/healthz')
def health_check():
    """Koyeb/Kubernetes health check endpoint."""
    server_logger.info("Health check endpoint accessed.")
    return jsonify(status="ok"), 200

def run_server():
    """Flask server ko run karta hai."""
    server_logger.info("Starting Flask server for health checks...")
    app.run(host='0.0.0.0', port=os.getenv("PORT", 8000)) 

if __name__ == "__main__":
    run_server()
