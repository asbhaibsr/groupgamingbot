import os
from pymongo import MongoClient
from pymongo.errors import ConnectionFailure, OperationFailure

class MongoDB:
    def __init__(self):
        self.client = None
        self.db = None
        # MongoDB se connect karne ki koshish karein jab object banaya jata hai
        self.connected = self.connect()

    def connect(self):
        """
        MongoDB se connect karta hai.
        Safalta par True, vifalta par False return karta hai.
        """
        mongo_uri = os.getenv("MONGO_URI")
        if not mongo_uri:
            print("Error: MONGO_URI environment variable not set.")
            return False

        try:
            self.client = MongoClient(mongo_uri)
            # Ping command se connection check karein
            self.client.admin.command('ping')
            self.db = self.client["telegram_game_bot"] # Apne database ka naam
            print("Successfully connected to MongoDB!")
            return True # Safal connection
        except ConnectionFailure as e:
            print(f"MongoDB connection failed: {e}")
            self.client = None
            self.db = None
            return False # Vifal connection
        except OperationFailure as e:
            print(f"MongoDB authentication/operation failed: {e}")
            self.client = None
            self.db = None
            return False # Vifal connection
        except Exception as e:
            print(f"An unexpected error occurred during MongoDB connection: {e}")
            self.client = None
            self.db = None
            return False # Vifal connection

    def get_collection(self, collection_name):
        """Ek specific collection return karta hai."""
        # 'if self.db:' ko 'if self.db is not None:' se badla gaya
        if self.db is not None:
            return self.db[collection_name]
        print("Error: MongoDB not connected.")
        return None

    # Game State ke liye functions
    def save_game_state(self, game_data):
        """Game ki current state save karta hai."""
        # 'if self.db:' ko 'if self.db is not None:' se badla gaya
        if self.db is not None:
            games_col = self.get_collection("game_states")
            games_col.update_one(
                {"_id": game_data["_id"]}, # Unique ID for each game instance
                {"$set": game_data},
                upsert=True # Agar ID nahi hai to naya document banayega
            )
            print(f"Game state saved for game ID: {game_data['_id']}")
            return True
        return False

    def get_game_state(self, game_id):
        """Ek specific game ki state retrieve karta hai."""
        # 'if self.db:' ko 'if self.db is not None:' se badla gaya
        if self.db is not None:
            games_col = self.get_collection("game_states")
            return games_col.find_one({"_id": game_id})
        return None

    def delete_game_state(self, game_id):
        """Game khatm hone ke baad uski state delete karta hai."""
        # 'if self.db:' ko 'if self.db is not None:' se badla gaya
        if self.db is not None:
            games_col = self.get_collection("game_states")
            result = games_col.delete_one({"_id": game_id})
            if result.deleted_count > 0:
                print(f"Game state deleted for game ID: {game_id}")
                return True
            print(f"No game state found with ID: {game_id}")
        return False

    # User Stats ke liye functions
    def update_user_stats(self, user_id, username, stats_update):
        """User ke stats ko update karta hai."""
        # 'if self.db:' ko 'if self.db is not None:' se badla gaya
        if self.db is not None:
            users_col = self.get_collection("user_stats")
            users_col.update_one(
                {"_id": user_id},
                {"$set": {"username": username}, "$inc": stats_update}, # $inc se values badhati hain
                upsert=True
            )
            print(f"User stats updated for user ID: {user_id}")
            return True
        return False

    def get_user_stats(self, user_id):
        """Ek user ke stats retrieve karta hai."""
        # 'if self.db:' ko 'if self.db is not None:' se badla gaya
        if self.db is not None:
            users_col = self.get_collection("user_stats")
            return users_col.find_one({"_id": user_id})
        return None

    def get_leaderboard(self, limit=10, worldwide=True):
        """Leaderboard data retrieve karta hai."""
        # 'if self.db:' ko 'if self.db is not None:' se badla gaya
        if self.db is not None:
            users_col = self.get_collection("user_stats")
            # Yahan aap apne scoring logic ke hisaab se sort kar sakte hain
            if worldwide:
                leaderboard = users_col.find().sort("total_score", -1).limit(limit)
            else:
                # Group specific leaderboard ke liye aapko game_state mein players ke score track karne honge
                # Ya fir users_col mein group ID ke hisab se filtering
                leaderboard = users_col.find().sort("total_score", -1).limit(limit) # Filhaal worldwide jaisa hi
            return list(leaderboard)
        return []

# Example usage (testing purpose, will be used by main.py)
if __name__ == "__main__":
    from dotenv import load_dotenv
    load_dotenv() # .env file se variables load karein

    db_manager = MongoDB()

    if db_manager.connected:
        # Test saving game state
        test_game_id = "test_game_123"
        db_manager.save_game_state({
            "_id": test_game_id,
            "group_id": 12345,
            "game_type": "wordchain",
            "current_question": "A _ P _ L _",
            "correct_answer": "APPLE",
            "players": [],
            "game_status": "in_progress"
        })

        # Test getting game state
        game_state = db_manager.get_game_state(test_game_id)
        if game_state:
            print(f"Retrieved game state: {game_state}")

        # Test updating user stats
        test_user_id = 98765
        db_manager.update_user_stats(test_user_id, "TestUser", {"games_played": 1, "total_score": 10})
        user_stats = db_manager.get_user_stats(test_user_id)
        if user_stats:
            print(f"Retrieved user stats: {user_stats}")

        # Test deleting game state
        db_manager.delete_game_state(test_game_id)
    else:
        print("MongoDB connection failed, cannot run tests.")
