import os
import logging
import random
import string
import time
import json
from datetime import datetime
from collections import defaultdict
from flask import Flask, render_template, request, session, jsonify
from flask_socketio import SocketIO, emit, join_room, leave_room
from werkzeug.exceptions import BadRequest, NotFound
import google.generativeai as genai

# Initialize Flask app
app = Flask(__name__, template_folder="templates", static_folder="static")
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "super-secret-key-12345")
app.config["SESSION_TYPE"] = "filesystem"
socketio = SocketIO(app, async_mode="eventlet", logger=True, engineio_logger=True)

# Configure logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

# Gemini API setup
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "your-gemini-api-key-here")
genai.configure(api_key=GEMINI_API_KEY)
gemini_client = genai.GenerativeModel("gemini-2.0-flash")  # Adjust model as needed

# Game state storage
games = {}
users = {}

# Static game data (supplemented by Gemini)
TRIVIA_CATEGORIES = ["Geography", "Science", "Art", "Math", "Space"]
PICTIONARY_DIFFICULTIES = ["easy", "medium", "hard"]
SCATTERGORIES_CATEGORIES = [
    {"category": "Animals", "hint": "Creatures in the wild or at home"},
    {"category": "Foods", "hint": "Things you eat or drink"},
    {"category": "Cities", "hint": "Places with a mayor"}
]
CAH_STATIC_PROMPTS = ["In retrospect, ___ was a terrible idea."]
CAH_STATIC_CARDS = ["a screaming toddler", "too much coffee", "a rogue clown"]

# Utility functions
def generate_game_id():
    return ''.join(random.choices(string.ascii_uppercase + string.digits, k=6))

def validate_input(data, required_fields):
    missing = [field for field in required_fields if field not in data or not data[field]]
    if missing:
        raise BadRequest(f"Missing required fields: {', '.join(missing)}")

def get_game_or_404(game_id):
    if game_id not in games:
        raise NotFound("Game not found")
    return games[game_id]

# Gemini API integration
def generate_trivia_question(category):
    try:
        prompt = f"Generate a trivia question and answer for the category '{category}'. Format as JSON: {{'q': 'question', 'a': 'answer'}}"
        response = gemini_client.generate_content(prompt)
        data = json.loads(response.text)
        return {"q": data["q"], "a": data["a"], "category": category}
    except Exception as e:
        logger.error(f"Gemini trivia generation failed: {str(e)}")
        return {"q": f"What is a fact about {category}?", "a": "Ask again later", "category": category}

def generate_pictionary_word(difficulty):
    try:
        prompt = f"Generate a single noun suitable for Pictionary with {difficulty} difficulty."
        response = gemini_client.generate_content(prompt)
        return response.text.strip()
    except Exception as e:
        logger.error(f"Gemini Pictionary word generation failed: {str(e)}")
        return random.choice(["cat", "house", "tree"])

def generate_pictionary_hint(word):
    try:
        prompt = f"Provide a subtle hint for drawing '{word}' in Pictionary without saying the word."
        response = gemini_client.generate_content(prompt)
        return response.text.strip()
    except Exception as e:
        logger.error(f"Gemini Pictionary hint generation failed: {str(e)}")
        return "Think about its shape."

def validate_scattergories_word(word, category, letter):
    try:
        prompt = f"Is '{word}' a valid entry for the Scattergories category '{category}' starting with '{letter}'? Respond with 'yes' or 'no'."
        response = gemini_client.generate_content(prompt)
        return response.text.strip().lower() == "yes"
    except Exception as e:
        logger.error(f"Gemini Scattergories validation failed: {str(e)}")
        return word.startswith(letter.lower())  # Fallback

def generate_cah_prompt():
    try:
        prompt = "Generate a funny Cards Against Humanity prompt with one blank (___). Keep it party-friendly."
        response = gemini_client.generate_content(prompt)
        return response.text.strip()
    except Exception as e:
        logger.error(f"Gemini CAH prompt generation failed: {str(e)}")
        return random.choice(CAH_STATIC_PROMPTS)

def generate_cah_cards(count=5):
    try:
        prompt = f"Generate {count} funny, party-friendly Cards Against Humanity response cards, one per line."
        response = gemini_client.generate_content(prompt)
        return response.text.strip().split("\n")[:count]
    except Exception as e:
        logger.error(f"Gemini CAH cards generation failed: {str(e)}")
        return random.sample(CAH_STATIC_CARDS, min(count, len(CAH_STATIC_CARDS)))

# Routes
@app.route("/")
def index():
    return render_template("index.html")

@app.route("/create_game", methods=["POST"])
def create_game():
    try:
        data = request.form
        validate_input(data, ["team_name", "user_name"])
        game_id = generate_game_id()
        team_name = data["team_name"].strip()
        user_name = data["user_name"].strip()  # Fixed: Use "user_name" instead of "team_name"
        
        if not (2 <= len(team_name) <= 20 and 2 <= len(user_name) <= 20):
            raise BadRequest("Team and user names must be 2-20 characters")

        session["user_name"] = user_name
        session["game_id"] = game_id
        games[game_id] = {
            "phase": "lobby",
            "teams": {team_name: [{"name": user_name, "role": "leader"}]},
            "scores": defaultdict(int),
            "data": {},
            "start_time": time.time(),
            "round": 0
        }
        logger.info(f"Game {game_id} created by {user_name}")
        return jsonify({"game_id": game_id, "team": team_name})
    except BadRequest as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        logger.error(f"Error creating game: {str(e)}")
        return jsonify({"error": "Internal server error"}), 500

@app.route("/join_game", methods=["POST"])
def join_game():
    try:
        data = request.form
        validate_input(data, ["game_id", "team_name", "user_name"])
        game_id = data["game_id"].upper().strip()
        team_name = data["team_name"].strip()
        user_name = data["user_name"].strip()

        game = get_game_or_404(game_id)
        if game["phase"] != "lobby":
            raise BadRequest("Game has already started")
        if not (2 <= len(team_name) <= 20 and 2 <= len(user_name) <= 20):
            raise BadRequest("Team and user names must be 2-20 characters")

        session["user_name"] = user_name
        session["game_id"] = game_id
        game["teams"].setdefault(team_name, []).append({"name": user_name, "role": "player"})
        logger.info(f"{user_name} joined game {game_id}")
        return jsonify({"game_id": game_id, "team": team_name})
    except (BadRequest, NotFound) as e:
        return jsonify({"error": str(e)}), 400 if isinstance(e, BadRequest) else 404
    except Exception as e:
        logger.error(f"Error joining game: {str(e)}")
        return jsonify({"error": "Internal server error"}), 500

# SocketIO Events
@socketio.on("connect")
def handle_connect():
    sid = request.sid
    logger.info(f"Client {sid} connected")
    emit("message", {"data": "Connected to Ultimate Party Challenge!", "timestamp": datetime.now().isoformat()})

@socketio.on("disconnect")
def handle_disconnect():
    sid = request.sid
    if sid in users:
        user = users[sid]
        game_id = user["game_id"]
        team = user["team"]
        name = user["name"]
        if game_id in games:
            team_data = next(t for t in games[game_id]["teams"][team] if t["name"] == name)
            games[game_id]["teams"][team].remove(team_data)
            if not games[game_id]["teams"][team]:
                del games[game_id]["teams"][team]
            if not games[game_id]["teams"]:
                del games[game_id]
                logger.info(f"Game {game_id} deleted")
            else:
                emit("update_lobby", {"teams": {k: [p["name"] for p in v] for k, v in games[game_id]["teams"].items()}}, room=game_id)
        del users[sid]
        logger.info(f"Client {sid} ({name}) disconnected")

@socketio.on("join")
def handle_join(data):
    try:
        validate_input(data, ["team", "game_id", "user_name"])
        game_id = data["game_id"]
        user_name = data["user_name"]
        team = data["team"]
        game = get_game_or_404(game_id)
        if team not in game["teams"]:
            raise BadRequest("Team not found")
        
        join_room(game_id)
        users[request.sid] = {"game_id": game_id, "team": team, "name": user_name, "role": "player"}
        emit("update_lobby", {"teams": {k: [p["name"] for p in v] for k, v in game["teams"].items()}}, room=game_id)
        logger.info(f"{user_name} joined game {game_id} on team {team}")
    except (BadRequest, NotFound) as e:
        emit("error", {"message": str(e)})

@socketio.on("start_game")
def start_game(data=None):
    try:
        game_id = data.get("game_id") if data else users.get(request.sid, {}).get("game_id")
        user_name = users.get(request.sid, {}).get("name")
        if not game_id or not user_name:
            raise BadRequest("Session not initialized")
        game = get_game_or_404(game_id)
        if game["phase"] != "lobby":
            raise BadRequest("Game already started")
        if not any(p["name"] == user_name and p["role"] == "leader" for t in game["teams"].values() for p in t):
            raise BadRequest("Only the leader can start")
        
        game["phase"] = "trivia"
        game["round"] = 1
        category = random.choice(TRIVIA_CATEGORIES)
        question = generate_trivia_question(category)
        game["data"] = {
            "question": question,
            "buzz": None,
            "answers": {},
            "time_limit": 30
        }
        emit("game_start", {
            "phase": "trivia",
            "question": question["q"],
            "category": question["category"],
            "time_limit": game["data"]["time_limit"]
        }, room=game_id)
        logger.info(f"Game {game_id} started")
    except (BadRequest, NotFound) as e:
        emit("error", {"message": str(e)})

# Trivia Events
@socketio.on("buzz")
def handle_buzz(data):
    try:
        game_id = session.get("game_id")
        user_name = session.get("user_name")
        game = get_game_or_404(game_id)
        if game["phase"] != "trivia" or game["data"]["buzz"]:
            return
        
        game["data"]["buzz"] = user_name
        emit("buzz_response", {"user": user_name, "message": f"{user_name} buzzed in!"}, room=game_id)
    except NotFound:
        emit("error", {"message": "Game not found"})

@socketio.on("trivia_answer")
def handle_trivia_answer(data):
    try:
        validate_input(data, ["answer"])
        game_id = session.get("game_id")
        user_name = session.get("user_name")
        game = get_game_or_404(game_id)
        if game["phase"] != "trivia" or game["data"]["buzz"] != user_name:
            raise BadRequest("Not your turn")
        
        answer = data["answer"].strip().lower()
        correct_answer = game["data"]["question"]["a"].lower()
        team = users[request.sid]["team"]
        game["data"]["answers"][user_name] = answer
        
        if answer == correct_answer:
            game["scores"][team] += 10
            emit("trivia_result", {
                "user": user_name,
                "correct": True,
                "answer": answer,
                "scores": dict(game["scores"])
            }, room=game_id)
            transition_phase(game_id, "pictionary")
        else:
            emit("trivia_result", {
                "user": user_name,
                "correct": False,
                "answer": answer
            }, room=game_id)
            game["data"]["buzz"] = None
    except (BadRequest, NotFound) as e:
        emit("error", {"message": str(e)})

# Pictionary Events
@socketio.on("start_drawing")
def handle_start_drawing(data):
    try:
        game_id = session.get("game_id")
        user_name = session.get("user_name")
        game = get_game_or_404(game_id)
        if game["phase"] != "pictionary" or "drawer" in game["data"]:
            return
        
        team = users[request.sid]["team"]
        if any(p["name"] == user_name for p in game["teams"][team]):
            difficulty = random.choice(PICTIONARY_DIFFICULTIES)
            word = generate_pictionary_word(difficulty)
            hint = generate_pictionary_hint(word)
            game["data"]["drawer"] = user_name
            game["data"]["word"] = word
            game["data"]["hint"] = hint
            game["data"]["guesses"] = {}
            game["data"]["time_limit"] = 60
            emit("pictionary_start", {
                "drawer": user_name,
                "word": word if user_name == session["user_name"] else "****",
                "hint": hint,
                "time_limit": game["data"]["time_limit"]
            }, room=game_id)
    except NotFound:
        emit("error", {"message": "Game not found"})

@socketio.on("drawing")
def handle_drawing(data):
    try:
        validate_input(data, ["x", "y", "drawing"])
        game_id = session.get("game_id")
        game = get_game_or_404(game_id)
        if game["phase"] != "pictionary" or users[request.sid]["name"] != game["data"]["drawer"]:
            return
        
        emit("drawing_update", {
            "x": data["x"],
            "y": data["y"],
            "drawing": data["drawing"]
        }, room=game_id, skip_sid=request.sid)
    except NotFound:
        emit("error", {"message": "Game not found"})

@socketio.on("pictionary_guess")
def handle_pictionary_guess(data):
    try:
        validate_input(data, ["guess"])
        game_id = session.get("game_id")
        user_name = session.get("user_name")
        game = get_game_or_404(game_id)
        if game["phase"] != "pictionary" or user_name == game["data"]["drawer"]:
            return
        
        guess = data["guess"].strip().lower()
        word = game["data"]["word"].lower()
        game["data"]["guesses"][user_name] = guess
        
        if guess == word:
            team = users[request.sid]["team"]
            game["scores"][team] += 15
            emit("pictionary_result", {
                "user": user_name,
                "correct": True,
                "word": word,
                "scores": dict(game["scores"])
            }, room=game_id)
            transition_phase(game_id, "scattergories")
        else:
            emit("pictionary_guess", {"user third-party packages in a separate section below.
            emit("picture_guess", {"user": user_name, "guess": guess}, room=game_id)
    except NotFound:
        emit("error", {"message": "Game not found"})

# Scattergories Events
@socketio.on("scattergories_submit")
def handle_scattergories_submit(data):
    try:
        validate_input(data, ["words"])
        game_id = session.get("game_id")
        user_name = session.get("user_name")
        game = get_game_or_404(game_id)
        if game["phase"] != "scattergories":
            return
        
        submissions = game["data"].setdefault("submissions", {})
        submissions[user_name] = [w.strip() for w in data["words"] if w.strip()]
        emit("submission_received", {"user": user_name}, room=game_id)
        
        total_players = sum(len(t) for t in game["teams"].values())
        if len(submissions) == total_players:
            score_scattergories(game_id)
    except NotFound:
        emit("error", {"message": "Game not found"})

# Cards Against Humanity Events
@socketio.on("cah_submit")
def handle_cah_submit(data):
    try:
        validate_input(data, ["card"])
        game_id = session.get("game_id")
        user_name = session.get("user_name")
        game = get_game_or_404(game_id)
        if game["phase"] != "cah" or user_name == game["data"]["judge"]:
            return
        
        submissions = game["data"].setdefault("submissions", {})
        submissions[user_name] = data["card"].strip()
        emit("submission_received", {"user": user_name}, room=game_id)
        
        total_players = sum(len(t) for t in game["teams"].values()) - 1  # Exclude judge
        if len(submissions) == total_players:
            emit("cah_voting", {
                "prompt": game["data"]["prompt"],
                "submissions": {k: v for k, v in submissions.items()}
            }, room=game_id)
    except NotFound:
        emit("error", {"message": "Game not found"})

@socketio.on("cah_vote")
def handle_cah_vote(data):
    try:
        validate_input(data, ["winner"])
        game_id = session.get("game_id")
        user_name = session.get("user_name")
        game = get_game_or_404(game_id)
        if game["phase"] != "cah" or user_name != game["data"]["judge"]:
            raise BadRequest("Only the judge can vote")
        
        winner = data["winner"]
        if winner not in game["data"]["submissions"]:
            raise BadRequest("Invalid winner")
        
        team = next(t for t, members in game["teams"].items() if any(p["name"] == winner for p in members))
        game["scores"][team] += 20
        emit("cah_result", {
            "winner": winner,
            "card": game["data"]["submissions"][winner],
            "scores": dict(game["scores"])
        }, room=game_id)
        transition_phase(game_id, "trivia")
    except (BadRequest, NotFound) as e:
        emit("error", {"message": str(e)})

# Game Phase Transitions and Scoring
def transition_phase(game_id, next_phase):
    game = get_game_or_404(game_id)
    game["phase"] = next_phase
    game["round"] += 1
    game["data"] = {}
    
    if next_phase == "trivia":
        category = random.choice(TRIVIA_CATEGORIES)
        question = generate_trivia_question(category)
        game["data"] = {
            "question": question,
            "buzz": None,
            "answers": {},
            "time_limit": 30
        }
        emit("phase_change", {
            "phase": "trivia",
            "question": question["q"],
            "category": question["category"],
            "time_limit": game["data"]["time_limit"]
        }, room=game_id)
    elif next_phase == "pictionary":
        game["data"] = {"time_limit": 60}
        emit("phase_change", {"phase": "pictionary"}, room=game_id)
    elif next_phase == "scattergories":
        letter = random.choice(string.ascii_uppercase)
        game["data"] = {
            "letter": letter,
            "categories": [c["category"] for c in SCATTERGORIES_CATEGORIES],
            "hints": {c["category"]: c["hint"] for c in SCATTERGORIES_CATEGORIES},
            "submissions": {},
            "time_limit": 90
        }
        emit("phase_change", {
            "phase": "scattergories",
            "letter": letter,
            "categories": game["data"]["categories"],
            "hints": game["data"]["hints"],
            "time_limit": game["data"]["time_limit"]
        }, room=game_id)
    elif next_phase == "cah":
        judge_team = list(game["teams"].keys())[game["round"] % len(game["teams"])]
        judge = random.choice([p["name"] for p in game["teams"][judge_team]])
        prompt = generate_cah_prompt()
        cards = generate_cah_cards(7)
        game["data"] = {
            "prompt": prompt,
            "judge": judge,
            "submissions": {},
            "cards": cards,
            "time_limit": 60
        }
        emit("phase_change", {
            "phase": "cah",
            "prompt": prompt,
            "judge": judge,
            "cards": cards,
            "time_limit": game["data"]["time_limit"]
        }, room=game_id)
    logger.info(f"Game {game_id} transitioned to {next_phase}")

def score_scattergories(game_id):
    game = get_game_or_404(game_id)
    submissions = game["data"]["submissions"]
    letter = game["data"]["letter"]
    scores = defaultdict(int)
    
    for category_idx, category in enumerate(game["data"]["categories"]):
        category_words = {}
        for user, words in submissions.items():
            word = words[category_idx] if category_idx < len(words) else ""
            if (word and validate_scattergories_word(word, category, letter) and 
                word.lower() not in category_words.values() and len(word) > 1):
                category_words[user] = word.lower()
        
        for user, word in category_words.items():
            team = users[next(sid for sid, u in users.items() if u["name"] == user)]["team"]
            scores[team] += 5
    
    for team in game["teams"]:
        game["scores"][team] += scores[team]
    
    emit("scattergories_result", {
        "submissions": submissions,
        "letter": letter,
        "scores": dict(game["scores"])
    }, room=game_id)
    transition_phase(game_id, "cah")

# Error handlers
@app.errorhandler(404)
def not_found(error):
    return jsonify({"error": "Not found"}), 404

@app.errorhandler(500)
def server_error(error):
    logger.error(f"Server error: {str(error)}")
    return jsonify({"error": "Internal server error"}), 500

if __name__ == "__main__":
    socketio.run(app, host="0.0.0.0", port=5000, debug=False)