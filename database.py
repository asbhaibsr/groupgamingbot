import os
from pymongo import MongoClient, ASCENDING
import logging
from datetime import datetime

logger = logging.getLogger(__name__)

class MongoDB:
    def __init__(self):
        self.client = None
        self.db = None
        self.connected = False
        self.connect()

    def connect(self):
        mongo_uri = os.getenv("MONGO_URI")
        if not mongo_uri:
            logger.error("MONGO_URI environment variable not set.")
            self.connected = False
            return

        try:
            self.client = MongoClient(mongo_uri)
            self.client.admin.command('ping') # Test connection
            self.db = self.client.get_database("telegram_games_db")
            self.connected = True
            logger.info("MongoDB connected successfully!")
            self._ensure_indexes()
        except Exception as e:
            logger.error(f"Could not connect to MongoDB: {e}")
            self.connected = False

    def _ensure_indexes(self):
        """Collections ke liye zaroori indexes banata hai."""
        if self.db:
            # game_states collection ke liye index
            self.db.game_states.create_index([("group_id", ASCENDING)], unique=True, name="group_id_idx")
            logger.info("Index created for game_states.group_id")

            # user_stats collection ke liye index
            self.db.user_stats.create_index([("user_id", ASCENDING)], unique=True, name="user_id_idx")
            logger.info("Index created for user_stats.user_id")

            # game_content collection ke liye index
            # game_message_id ko unique index banate hain takki duplicate na ho
            self.db.game_content.create_index([("game_message_id", ASCENDING)], unique=True, name="game_message_id_idx")
            # created_at par index takki oldest entries ko delete kar saken
            self.db.game_content.create_index([("created_at", ASCENDING)], name="created_at_idx")
            logger.info("Indexes created for game_content collection.")


    def get_collection(self, collection_name):
        if self.db:
            return self.db[collection_name]
        return None

    # --- Game State Management (existing) ---
    def save_game_state(self, game_data):
        if self.connected:
            game_states = self.get_collection("game_states")
            try:
                game_states.replace_one(
                    {"_id": game_data["_id"]},
                    game_data,
                    upsert=True
                )
                logger.info(f"Game state for {game_data['_id']} saved/updated.")
                return True
            except Exception as e:
                logger.error(f"Error saving game state: {e}")
        return False

    def get_game_state(self, game_id):
        if self.connected:
            game_states = self.get_collection("game_states")
            try:
                return game_states.find_one({"_id": game_id})
            except Exception as e:
                logger.error(f"Error getting game state for {game_id}: {e}")
        return None

    def delete_game_state(self, game_id):
        if self.connected:
            game_states = self.get_collection("game_states")
            try:
                result = game_states.delete_one({"_id": game_id})
                if result.deleted_count > 0:
                    logger.info(f"Game state for {game_id} deleted.")
                    return True
                else:
                    logger.warning(f"Game state for {game_id} not found for deletion.")
            except Exception as e:
                logger.error(f"Error deleting game state for {game_id}: {e}")
        return False
    
    # --- User Stats Management (existing) ---
    def update_user_stats(self, user_id, username, stats_update):
        if self.connected:
            user_stats = self.get_collection("user_stats")
            try:
                # $inc: increment values, $set: set username (in case it changes)
                user_stats.update_one(
                    {"user_id": user_id},
                    {"$set": {"username": username}, "$inc": stats_update},
                    upsert=True
                )
                logger.info(f"User stats for {user_id} ({username}) updated.")
                return True
            except Exception as e:
                logger.error(f"Error updating user stats for {user_id}: {e}")
        return False

    def get_user_stats(self, user_id):
        if self.connected:
            user_stats = self.get_collection("user_stats")
            try:
                return user_stats.find_one({"user_id": user_id})
            except Exception as e:
                logger.error(f"Error getting user stats for {user_id}: {e}")
        return None

    def get_leaderboard(self, limit=10, worldwide=True):
        if self.connected:
            user_stats = self.get_collection("user_stats")
            try:
                # Sort by total_score in descending order
                leaderboard = list(user_stats.find().sort("total_score", -1).limit(limit))
                return leaderboard
            except Exception as e:
                logger.error(f"Error getting leaderboard: {e}")
        return []

    # --- Game Content Management (NEW/UPDATED) ---
    def add_game_content(self, game_data):
        """
        Naye game content ko database mein add karta hai, jisme Telegram message ID bhi shamil hai.
        game_data format: {
            "game_type": "wordchain",
            "question": "...",
            "answer": "...",
            "game_message_id": <Telegram message ID>,
            "created_at": <timestamp>
        }
        """
        if self.db is not None:
            game_content_col = self.get_collection("game_content")
            try:
                # game_message_id unique hai, replace_one se upsert karein
                game_content_col.replace_one(
                    {"game_message_id": game_data["game_message_id"]},
                    game_data,
                    upsert=True
                )
                logger.info(f"Game content added/updated for message ID: {game_data['game_message_id']}")
                return True
            except Exception as e:
                logger.error(f"Error adding game content: {e}")
                return False
        return False

    def get_random_game_message_id(self, game_type):
        """
        Ek random game content item ka Telegram message ID retrieve karta hai game_type ke hisaab se.
        """
        if self.db is not None:
            game_content_col = self.get_collection("game_content")
            # Aggregation pipeline to get a random document
            pipeline = [
                {"$match": {"game_type": game_type}},
                {"$sample": {"size": 1}}
            ]
            result = list(game_content_col.aggregate(pipeline))
            if result:
                logger.info(f"Fetched random game message ID for type {game_type}")
                return result[0].get("game_message_id") # Sirf message ID return karein
            logger.warning(f"No game content found in DB for type: {game_type}")
        return None

    def get_game_content_count(self):
        """game_content collection mein documents ki sankhya return karta hai."""
        if self.db is not None:
            game_content_col = self.get_collection("game_content")
            return game_content_col.estimated_document_count()
        return 0

    def delete_oldest_game_content(self, count_to_delete):
        """
        oldest game content entries ko delete karta hai (Telegram message IDs).
        Return karta hai deleted message IDs ki list.
        """
        if self.db is not None:
            game_content_col = self.get_collection("game_content")
            try:
                # Oldest documents ko fetch karein by created_at
                oldest_entries = list(game_content_col.find().sort("created_at", ASCENDING).limit(count_to_delete))
                
                if oldest_entries:
                    delete_ids = [entry["_id"] for entry in oldest_entries]
                    telegram_message_ids_to_delete = [entry["game_message_id"] for entry in oldest_entries]

                    result = game_content_col.delete_many({"_id": {"$in": delete_ids}})
                    logger.info(f"Deleted {result.deleted_count} oldest game content entries from MongoDB.")
                    return telegram_message_ids_to_delete
                return []
            except Exception as e:
                logger.error(f"Error deleting oldest game content: {e}")
                return []
        return []

