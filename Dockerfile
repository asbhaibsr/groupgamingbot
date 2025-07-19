FROM python:3.11-slim

WORKDIR /app

# System dependencies
RUN apt-get update && apt-get install -y \
    gcc \
    python3-dev \
    && rm -rf /var/lib/apt/lists/*

# Install requirements with NO CACHE
COPY requirements.txt .
RUN pip install --no-cache-dir --force-reinstall -r requirements.txt

# Copy app
COPY . .

# Verify installation
RUN python -c "import pyrogram; print(f'Pyrogram Version: {pyrogram.__version__}')"
RUN python -c "from pyrogram import __version__; assert __version__ == '2.0.106', f'Wrong version: {__version__}'"

CMD ["python", "bot.py"]
