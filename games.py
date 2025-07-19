import random
import asyncio

# BaseGame class, jahan common game logic hoga
class BaseGame:
    def __init__(self, game_id, group_id, question, answer, game_type="base"):
        self.game_id = game_id
        self.group_id = group_id
        self.question = question
        self.answer = answer.upper()
        self.game_type = game_type
        self.players = []
        self.current_player_index = 0
        self.status = "waiting_for_players"
        self.join_window_end_time = asyncio.get_event_loop().time() + 60
        self.last_activity_time = asyncio.get_event_loop().time()
        self.turn_timeout = 30

    def add_player(self, user_id, username):
        if not any(player['id'] == user_id for player in self.players):
            self.players.append({
                "id": user_id,
                "username": username,
                "score": 0,
                "turn_order": len(self.players)
            })
            return True
        return False

    def get_current_player(self):
        if self.players:
            return self.players[self.current_player_index]
        return None

    def next_turn(self):
        if self.players:
            self.current_player_index = (self.current_player_index + 1) % len(self.players)

    def is_answer_correct(self, user_answer):
        return user_answer.upper() == self.answer

    def get_initial_message(self):
        remaining_time = int(self.join_window_end_time - asyncio.get_event_loop().time())
        if remaining_time < 0: remaining_time = 0

        return f"Naya **{self.game_type} Game** shuru ho raha hai!\n\n" \
               f"Sawal: **{self.question}**\n\n" \
               f"Join karne ke liye **Game Join Karein** button par click karein.\n" \
               f"Aapke paas **{remaining_time} seconds** hain join karne ke liye!"

    def get_game_data_for_db(self):
        # Yahan par WordChain aur Guessing specific attributes bhi shamil karein
        data = {
            "_id": self.game_id,
            "group_id": self.group_id,
            "game_type": self.game_type,
            "question": self.question,
            "answer": self.answer,
            "players": self.players,
            "current_player_index": self.current_player_index,
            "status": self.status,
            "join_window_end_time": self.join_window_end_time,
            "last_activity_time": self.last_activity_time,
            "turn_timeout": self.turn_timeout
        }
        if isinstance(self, WordChainGame):
            data["last_word_played"] = self.last_word_played
        elif isinstance(self, GuessingGame):
            data["guessed_letters"] = list(self.guessed_letters) # Sets ko list mein convert karein
        return data

# WordChainGame class
class WordChainGame(BaseGame):
    def __init__(self, game_id, group_id, question, answer):
        super().__init__(game_id, group_id, question, answer, "wordchain")
        self.last_word_played = None

    def is_answer_correct(self, user_answer):
        user_answer_upper = user_answer.upper()
        
        if not super().is_answer_correct(user_answer):
            return False

        if self.last_word_played:
            if not user_answer_upper.startswith(self.last_word_played[-1]):
                return False
        
        return True

    def update_last_word(self, word):
        self.last_word_played = word.upper()

    def get_initial_message(self):
        base_msg = super().get_initial_message()
        return base_msg.replace("Sawal:", "Chain shuru karein:")

# GuessingGame class
class GuessingGame(BaseGame):
    def __init__(self, game_id, group_id, question, answer):
        super().__init__(game_id, group_id, question, answer, "guessing")
        self.guessed_letters = set()
        self.display_word_template = "_ " * len(self.answer)

    def is_answer_correct(self, user_answer):
        user_answer_upper = user_answer.upper()
        
        if user_answer_upper == self.answer:
            return True
        
        if len(user_answer_upper) == 1 and user_answer_upper.isalpha():
            if user_answer_upper in self.answer and user_answer_upper not in self.guessed_letters:
                self.guessed_letters.add(user_answer_upper)
                return True
        return False

    def get_display_word(self):
        displayed = ""
        for char in self.answer:
            if char in self.guessed_letters:
                displayed += char
            elif char == " ":
                displayed += " "
            else:
                displayed += "_"
            displayed += " "
        return displayed.strip()

    def get_initial_message(self):
        base_msg = super().get_initial_message()
        return base_msg.replace(f"Sawal: {self.question}", f"Chupa hua shabd: `{self.get_display_word()}` ({len(self.answer)} akshar)")


# WordCorrectionGame class
class WordCorrectionGame(BaseGame):
    def __init__(self, game_id, group_id, question, answer):
        super().__init__(game_id, group_id, question, answer, "wordcorrection")

    def get_initial_message(self):
        base_msg = super().get_initial_message()
        return base_msg.replace("Sawal:", "Is shabd ko sahi karein:")

# Game factory function
def create_game(game_type, game_id, group_id, question, answer):
    if game_type == "wordchain":
        return WordChainGame(game_id, group_id, question, answer)
    elif game_type == "guessing":
        return GuessingGame(game_id, group_id, question, answer)
    elif game_type == "wordcorrection":
        return WordCorrectionGame(game_id, group_id, question, answer)
    else:
        return None

