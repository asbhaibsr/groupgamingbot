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
        """
        MongoDB se connect karta hai aur connection ki sthiti set karta hai.
        MONGO_URI environment variable se connection string leta hai.
        """
        mongo_uri = os.getenv("MONGO_URI")
        if not mongo_uri:
            logger.error("MONGO_URI environment variable not set. Please set it in your .env file.")
            self.connected = False
            return

        try:
            self.client = MongoClient(mongo_uri)
            # Connection ko test karne ke liye admin database ko ping karein.
            self.client.admin.command('ping') 
            self.db = self.client.get_database("telegram_games_db") # Apne database ka naam yahan define karein
            self.connected = True
            logger.info("MongoDB connected successfully!")
            self._ensure_indexes()
        except Exception as e:
            logger.error(f"Could not connect to MongoDB: {e}")
            self.connected = False # Connection fail hone par False set karein

    def _ensure_indexes(self):
        """
        Zaroori collections ke liye indexes banata hai.
        Agar indexes banane mein koi error aati hai, to bhi connection ko True rakhta hai.
        """
        if self.db:
            try:
                # 'game_states' collection ke liye index
                self.db.game_states.create_index([("group_id", ASCENDING)], unique=True, name="group_id_idx")
                logger.info("Index created for game_states.group_id")

                # 'user_stats' collection ke liye index
                self.db.user_stats.create_index([("user_id", ASCENDING)], unique=True, name="user_id_idx")
                logger.info("Index created for user_stats.user_id")

                # 'game_content' collection ke liye indexes
                # 'game_message_id' par unique index takki duplicate na ho
                self.db.game_content.create_index([("game_message_id", ASCENDING)], unique=True, name="game_message_id_idx")
                # 'created_at' par index takki sabse purani entries ko delete kar saken
                self.db.game_content.create_index([("created_at", ASCENDING)], name="created_at_idx")
                logger.info("Indexes created for game_content collection.")
            except Exception as e:
                # Agar index creation mein error aaye, to bhi MongoDB connection ko active rakhein,
                # kyuki initial connection successful raha hai.
                logger.error(f"Error ensuring MongoDB indexes: {e}. The database connection remains active.")
        else:
            logger.warning("Cannot ensure indexes: MongoDB not connected.")


    def get_collection(self, collection_name):
        """
        Diye gaye naam se MongoDB collection return karta hai, agar database connected hai.
        """
        if self.connected: # self.db is not None ki jagah self.connected use karein for consistency
            return self.db[collection_name]
        logger.warning(f"Attempted to get collection '{collection_name}' but MongoDB is not connected.")
        return None

    # --- Game State Management ---
    def save_game_state(self, game_data):
        """Game state ko database mein save ya update karta hai."""
        if self.connected:
            game_states = self.get_collection("game_states")
            if game_states is None: return False
            try:
                game_states.replace_one(
                    {"_id": game_data["_id"]},
                    game_data,
                    upsert=True
                )
                logger.info(f"Game state for {game_data['_id']} saved/updated.")
                return True
            except Exception as e:
                logger.error(f"Error saving game state for {game_data['_id']}: {e}")
        return False

    def get_game_state(self, game_id):
        """Diye gaye game ID se game state retrieve karta hai."""
        if self.connected:
            game_states = self.get_collection("game_states")
            if game_states is None: return None
            try:
                return game_states.find_one({"_id": game_id})
            except Exception as e:
                logger.error(f"Error getting game state for {game_id}: {e}")
        return None

    def delete_game_state(self, game_id):
        """Diye gaye game ID se game state delete karta hai."""
        if self.connected:
            game_states = self.get_collection("game_states")
            if game_states is None: return False
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
    
    # --- User Stats Management ---
    def update_user_stats(self, user_id, username, stats_update):
        """User ke stats ko update karta hai."""
        if self.connected:
            user_stats = self.get_collection("user_stats")
            if user_stats is None: return False
            try:
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
        """Diye gaye user ID se user stats retrieve karta hai."""
        if self.connected:
            user_stats = self.get_collection("user_stats")
            if user_stats is None: return None
            try:
                return user_stats.find_one({"user_id": user_id})
            except Exception as e:
                logger.error(f"Error getting user stats for {user_id}: {e}")
        return None

    def get_leaderboard(self, limit=10, worldwide=True):
        """Top players ka leaderboard retrieve karta hai."""
        if self.connected:
            user_stats = self.get_collection("user_stats")
            if user_stats is None: return []
            try:
                leaderboard = list(user_stats.find().sort("total_score", -1).limit(limit))
                return leaderboard
            except Exception as e:
                logger.error(f"Error getting leaderboard: {e}")
        return []

    # --- Game Content Management ---
    def add_game_content(self, game_data):
        """Naye game content ko database mein add karta hai."""
        if self.connected:
            game_content_col = self.get_collection("game_content")
            if game_content_col is None: return False
            try:
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
        """Random game content item ka Telegram message ID retrieve karta hai."""
        if self.connected:
            game_content_col = self.get_collection("game_content")
            if game_content_col is None: return None
            # Aggregation pipeline to get a random document
            pipeline = [
                {"$match": {"game_type": game_type}},
                {"$sample": {"size": 1}}
            ]
            result = list(game_content_col.aggregate(pipeline))
            if result:
                logger.info(f"Fetched random game message ID for type {game_type}")
                return result[0].get("game_message_id")
            logger.warning(f"No game content found in DB for type: {game_type}")
        return None

    def get_game_content_count(self):
        """'game_content' collection mein documents ki sankhya return karta hai."""
        if self.connected:
            game_content_col = self.get_collection("game_content")
            if game_content_col is None: return 0
            return game_content_col.estimated_document_count()
        return 0

    def delete_oldest_game_content(self, count_to_delete):
        """
        Sabse purani game content entries ko delete karta hai.
        Deleted message IDs ki list return karta hai.
        """
        if self.connected:
            game_content_col = self.get_collection("game_content")
            if game_content_col is None: return []
            try:
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

