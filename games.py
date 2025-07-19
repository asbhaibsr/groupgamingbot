import random
import re
import asyncio

# Ye classes abhi basic structure hain, inhe aapko poori tarah implement karna hoga.
# Abhi ye sirf sawal aur jawab validate karenge. Bot ka turn management main.py mein hoga.

class BaseGame:
    def __init__(self, game_id, group_id, question, answer):
        self.game_id = game_id
        self.group_id = group_id
        self.question = question
        self.answer = answer.upper() # Sabhi answers ko uppercase mein rakhein
        self.players = []
        self.current_player_index = 0
        self.status = "waiting_for_players" # waiting_for_players, in_progress, ended
        self.start_time = asyncio.get_event_loop().time()
        self.last_activity_time = self.start_time
        self.turn_timeout = 60 # seconds
        self.join_window_end_time = self.start_time + 60 # 1 minute for joining

    def add_player(self, user_id, username):
        if user_id not in [p['id'] for p in self.players]:
            self.players.append({"id": user_id, "username": username, "score": 0})
            return True
        return False

    def get_current_player(self):
        if self.players:
            return self.players[self.current_player_index]
        return None

    def next_turn(self):
        if self.players:
            self.current_player_index = (self.current_player_index + 1) % len(self.players)
            return self.get_current_player()
        return None

    def is_answer_correct(self, user_answer):
        """Har game type ke liye override kiya jayega."""
        return user_answer.upper() == self.answer

    def get_initial_message(self):
        """Game shuru hone par kya message dikhana hai."""
        return f"Game ID: `{self.game_id}`\n\n**{self.__class__.__name__}** shuru ho gaya hai!\n" \
               f"Sawal: {self.question}\n\n`/join` command se judiye!"

    def get_current_turn_message(self):
        player = self.get_current_player()
        if player:
            return f"Abhi **{player['username']}** ki baari hai.\nSawal: {self.question}"
        return "Khel mein koi player nahi hai."

    def get_game_data_for_db(self):
        """MongoDB mein save karne ke liye data."""
        return {
            "_id": self.game_id,
            "group_id": self.group_id,
            "game_type": self.__class__.__name__.lower(),
            "question": self.question,
            "answer": self.answer,
            "players": self.players,
            "current_player_index": self.current_player_index,
            "status": self.status,
            "start_time": self.start_time,
            "last_activity_time": self.last_activity_time,
        }

class WordChainGame(BaseGame):
    def __init__(self, game_id, group_id, question, answer):
        super().__init__(game_id, group_id, question, answer)
        self.last_word = "" # Pichhla bola gaya shabd

    def is_answer_correct(self, user_answer):
        user_answer = user_answer.upper()
        # Initial word ko check karein
        if not self.last_word:
            # First word, just check if it matches the answer
            return user_answer == self.answer
        else:
            # Subsequent words must start with the last letter of the previous word
            return user_answer.startswith(self.last_word[-1]) and user_answer == self.answer # Yahan aapko ek list of valid words ki zaroorat padegi

    def update_last_word(self, word):
        self.last_word = word.upper()

    def get_initial_message(self):
        return f"Game ID: `{self.game_id}`\n\n**Wordchain Game** shuru ho gaya hai!\n" \
               f"Pehla shabd: **{self.question}**\n\n`/join` command se judiye!"

class GuessingGame(BaseGame):
    def __init__(self, game_id, group_id, question, answer):
        super().__init__(game_id, group_id, question, answer)
        self.guessed_letters = set() # Guessed letters
        self.attempts = 0
        self.max_attempts = 10 # Example

    def get_display_word(self):
        display = ""
        for char in self.answer:
            if char in self.guessed_letters or not char.isalpha():
                display += char
            else:
                display += "_"
        return display

    def is_answer_correct(self, user_answer):
        user_answer = user_answer.upper()
        self.attempts += 1
        if len(user_answer) == 1 and user_answer.isalpha():
            if user_answer in self.answer:
                self.guessed_letters.add(user_answer)
                return True # Correct letter, but not necessarily the full word
            return False # Incorrect letter
        else:
            # Full word guess
            return user_answer == self.answer

    def get_initial_message(self):
        display_word = self.get_display_word()
        return f"Game ID: `{self.game_id}`\n\n**Guessing Game** shuru ho gaya hai!\n" \
               f"Shabd: `{display_word}`\n\n`/join` command se judiye! " \
               f"Aap letters ya poora shabd guess kar sakte hain."

class WordCorrectionGame(BaseGame):
    def __init__(self, game_id, group_id, question, answer):
        super().__init__(game_id, group_id, question, answer)
        # self.question is the misspelled word, self.answer is the correct word

    def is_answer_correct(self, user_answer):
        return user_answer.upper() == self.answer

    def get_initial_message(self):
        return f"Game ID: `{self.game_id}`\n\n**Word Correction Game** shuru ho gaya hai!\n" \
               f"Galat shabd: `{self.question}`\n\n`/join` command se judiye! Sahi spelling batayein."

# Game factory to create game instances
def create_game(game_type, game_id, group_id, question, answer):
    game_type = game_type.lower()
    if game_type == "wordchain":
        return WordChainGame(game_id, group_id, question, answer)
    elif game_type == "guessing":
        return GuessingGame(game_id, group_id, question, answer)
    elif game_type == "wordcorrection":
        return WordCorrectionGame(game_id, group_id, question, answer)
    else:
        return None

# Example usage (testing purpose)
if __name__ == "__main__":
    game_id = "test_game_abc"
    group_id = 123
    
    # Wordchain Test
    wc_game = create_game("wordchain", game_id, group_id, "APPLE", "APPLE")
    print(wc_game.get_initial_message())
    wc_game.add_player(101, "Alice")
    wc_game.add_player(102, "Bob")
    print(f"Current player: {wc_game.get_current_player()['username']}")
    print(f"Is 'apple' correct (initial)? {wc_game.is_answer_correct('apple')}")
    wc_game.update_last_word('APPLE')
    print(f"Is 'EGG' correct (starts with E)? {wc_game.is_answer_correct('EGG')}") # This logic needs refinement in WordChainGame
    wc_game.next_turn()
    print(f"Next player: {wc_game.get_current_player()['username']}")
    print("\n---")

    # Guessing Game Test
    guess_game = create_game("guessing", "test_guess_xyz", group_id, "_____", "PYTHON")
    print(guess_game.get_initial_message())
    guess_game.add_player(201, "Charlie")
    print(f"Is 'P' correct? {guess_game.is_answer_correct('P')}")
    print(f"Display: {guess_game.get_display_word()}")
    print(f"Is 'PYTHON' correct? {guess_game.is_answer_correct('PYTHON')}")
