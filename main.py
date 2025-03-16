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

# Initialize Flask app
app = Flask(__name__, template_folder="templates", static_folder="static")
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "super-secret-key-12345")  # Use env var in production
app.config["SESSION_TYPE"] = "filesystem"  # For session persistence
socketio = SocketIO(app, async_mode="eventlet", logger=True, engineio_logger=True)

# Configure logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

# Game state storage (in-memory; replace with Redis/MongoDB in production)
games = {}  # {game_id: {"phase": str, "teams": {}, "scores": {}, "data": {}, "start_time": float}}
users = {}  # {sid: {"game_id": str, "team": str, "name": str, "role": str}}

# Game data (expand these in production)
TRIVIA_QUESTIONS = [
    {"q": "What is the capital of France?", "a": "Paris", "category": "Geography"},
    {"q": "What gas do plants primarily use for photosynthesis?", "a": "Carbon Dioxide", "category": "Science"},
    {"q": "Who painted the Mona Lisa?", "a": "Leonardo da Vinci", "category": "Art"},
    {"q": "What is 2 + 2?", "a": "4", "category": "Math"},
    {"q": "Which planet is known as the Red Planet?", "a": "Mars", "category": "Space"}
]
PICTIONARY_WORDS = [
    {"word": "cat", "difficulty": "easy"}, {"word": "house", "difficulty": "easy"},
    {"word": "dragon", "difficulty": "hard"}, {"word": "spaceship", "difficulty": "medium"},
    {"word": "pizza", "difficulty": "easy"}
]
SCATTERGORIES_CATEGORIES = [
    {"category": "Animals", "hint": "Creatures in the wild or at home"},
    {"category": "Foods", "hint": "Things you eat or drink"},
    {"category": "Cities", "hint": "Places with a mayor"},
    {"category": "Sports", "hint": "Activities requiring physical skill"},
    {"category": "Movies", "hint": "Films you watch on screen"}
]
CAH_PROMPTS = [
    "In retrospect, ___ was a terrible idea.",
    "The secret to ___ is ___.",
    "Why did I wake up with ___?",
    "___: the ultimate party foul."
]
CAH_CARDS = [
    "a screaming toddler", "too much coffee", "a rogue clown", "spilled wine",
    "an alien invasion", "a broken chair", "uncontrollable dancing", "a lost sock",
    "a bad haircut", "unexpected karaoke"
]

# Utility functions
def generate_game_id():
    """Generate a unique 6-character game ID."""
    while True:
        game_id = ''.join(random.choices(string.ascii_uppercase + string.digits, k=6))
        if game_id not in games:
            return game_id

def validate_input(data, required_fields):
    """Validate incoming data for required fields."""
    missing = [field for field in required_fields if field not in data or not data[field]]
    if missing:
        raise BadRequest(f"Missing required fields: {', '.join(missing)}")

def get_game_or_404(game_id):
    """Retrieve game state or raise 404."""
    if game_id not in games:
        raise NotFound("Game not found")
    return games[game_id]

# Routes
@app.route("/")
def index():
    """Render the main game interface."""
    return render_template("index.html")

@app.route("/create_game", methods=["POST"])
def create_game():
    """Create a new game instance."""
    try:
        data = request.form
        validate_input(data, ["team_name", "user_name"])
        game_id = generate_game_id()
        team_name = data["team_name"].strip()
        user_name = data["user_name"].strip()
        
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
        logger.info(f"Game {game_id} created by {user_name} with team {team_name}")
        return jsonify({"game_id": game_id, "team": team_name})
    except BadRequest as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        logger.error(f"Error creating game: {str(e)}")
        return jsonify({"error": "Internal server error"}), 500

@app.route("/join_game", methods=["POST"])
def join_game():
    """Join an existing game."""
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
        logger.info(f"{user_name} joined game {game_id} on team {team_name}")
        return jsonify({"game_id": game_id, "team": team_name})
    except (BadRequest, NotFound) as e:
        return jsonify({"error": str(e)}), 400 if isinstance(e, BadRequest) else 404
    except Exception as e:
        logger.error(f"Error joining game: {str(e)}")
        return jsonify({"error": "Internal server error"}), 500

# SocketIO Events
@socketio.on("connect")
def handle_connect():
    """Handle client connection."""
    sid = request.sid
    logger.info(f"Client {sid} connected")
    emit("message", {"data": "Connected to Ultimate Party Challenge!", "timestamp": datetime.now().isoformat()})

@socketio.on("disconnect")
def handle_disconnect():
    """Handle client disconnection."""
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
                logger.info(f"Game {game_id} deleted (no players left)")
            else:
                emit("update_lobby", {"teams": {k: [p["name"] for p in v] for k, v in games[game_id]["teams"].items()}}, room=game_id)
        del users[sid]
        logger.info(f"Client {sid} ({name}) disconnected from game {game_id}")

@socketio.on("join")
def handle_join(data):
    """Join a game room."""
    try:
        validate_input(data, ["team"])
        game_id = session.get("game_id")
        user_name = session.get("user_name")
        team = data["team"]
        if not game_id or not user_name:
            raise BadRequest("Session not initialized")
        game = get_game_or_404(game_id)
        if team not in game["teams"]:
            raise BadRequest("Team not found")
        
        join_room(game_id)
        users[request.sid] = {"game_id": game_id, "team": team, "name": user_name, "role": "player"}
        emit("update_lobby", {"teams": {k: [p["name"] for p in v] for k, v in game["teams"].items()}}, room=game_id)
        logger.info(f"{user_name} joined room {game_id}")
    except (BadRequest, NotFound) as e:
        emit("error", {"message": str(e)})

@socketio.on("start_game")
def start_game(data):
    """Start the game from lobby."""
    try:
        game_id = session.get("game_id")
        user_name = session.get("user_name")
        game = get_game_or_404(game_id)
        if game["phase"] != "lobby":
            raise BadRequest("Game already started")
        if not any(p["name"] == user_name and p["role"] == "leader" for t in game["teams"].values() for p in t):
            raise BadRequest("Only the leader can start the game")
        
        game["phase"] = "trivia"
        game["round"] = 1
        game["data"] = {
            "question": random.choice(TRIVIA_QUESTIONS),
            "buzz": None,
            "answers": {},
            "time_limit": 30  # seconds
        }
        emit("game_start", {
            "phase": "trivia",
            "question": game["data"]["question"]["q"],
            "category": game["data"]["question"]["category"],
            "time_limit": game["data"]["time_limit"]
        }, room=game_id)
        logger.info(f"Game {game_id} started by {user_name}")
    except (BadRequest, NotFound) as e:
        emit("error", {"message": str(e)})

# Trivia Events
@socketio.on("buzz")
def handle_buzz(data):
    """Handle trivia buzz-in."""
    try:
        game_id = session.get("game_id")
        user_name = session.get("user_name")
        game = get_game_or_404(game_id)
        if game["phase"] != "trivia" or game["data"]["buzz"]:
            return
        
        game["data"]["buzz"] = user_name
        emit("buzz_response", {"user": user_name, "message": f"{user_name} buzzed in!"}, room=game_id)
        logger.info(f"{user_name} buzzed in game {game_id}")
    except NotFound:
        emit("error", {"message": "Game not found"})

@socketio.on("trivia_answer")
def handle_trivia_answer(data):
    """Handle trivia answer submission."""
    try:
        validate_input(data, ["answer"])
        game_id = session.get("game_id")
        user_name = session.get("user_name")
        game = get_game_or_404(game_id)
        if game["phase"] != "trivia" or game["data"]["buzz"] != user_name:
            raise BadRequest("Not your turn to answer")
        
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
            logger.info(f"{user_name} answered correctly in game {game_id}")
            transition_phase(game_id, "pictionary")
        else:
            emit("trivia_result", {
                "user": user_name,
                "correct": False,
                "answer": answer
            }, room=game_id)
            game["data"]["buzz"] = None  # Allow another buzz
            logger.info(f"{user_name} answered incorrectly in game {game_id}")
    except (BadRequest, NotFound) as e:
        emit("error", {"message": str(e)})

# Pictionary Events
@socketio.on("start_drawing")
def handle_start_drawing(data):
    """Assign a drawer and start Pictionary."""
    try:
        game_id = session.get("game_id")
        user_name = session.get("user_name")
        game = get_game_or_404(game_id)
        if game["phase"] != "pictionary":
            return
        if "drawer" in game["data"]:
            raise BadRequest("Drawing already in progress")
        
        team = users[request.sid]["team"]
        if any(p["name"] == user_name for p in game["teams"][team]):
            game["data"]["drawer"] = user_name
            game["data"]["word"] = random.choice(PICTIONARY_WORDS)["word"]
            game["data"]["guesses"] = {}
            game["data"]["time_limit"] = 60
            emit("pictionary_start", {
                "drawer": user_name,
                "word": game["data"]["word"] if user_name == session["user_name"] else "****",
                "time_limit": game["data"]["time_limit"]
            }, room=game_id)
            logger.info(f"{user_name} started drawing in game {game_id}")
    except (BadRequest, NotFound) as e:
        emit("error", {"message": str(e)})

@socketio.on("drawing")
def handle_drawing(data):
    """Broadcast drawing coordinates."""
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
    """Handle Pictionary guesses."""
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
            logger.info(f"{user_name} guessed correctly in game {game_id}")
            transition_phase(game_id, "scattergories")
        else:
            emit("pictionary_guess", {"user": user_name, "guess": guess}, room=game_id)
    except NotFound:
        emit("error", {"message": "Game not found"})

# Scattergories Events
@socketio.on("scattergories_submit")
def handle_scattergories_submit(data):
    """Handle Scattergories word submissions."""
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
            logger.info(f"All submissions received for Scattergories in game {game_id}")
    except NotFound:
        emit("error", {"message": "Game not found"})

# Cards Against Humanity Events
@socketio.on("cah_submit")
def handle_cah_submit(data):
    """Handle CAH card submissions."""
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
            logger.info(f"All CAH submissions received in game {game_id}")
    except NotFound:
        emit("error", {"message": "Game not found"})

@socketio.on("cah_vote")
def handle_cah_vote(data):
    """Handle CAH voting."""
    try:
        validate_input(data, ["winner"])
        game_id = session.get("game_id")
        user_name = session.get("user_name")
        game = get_game_or_404(game_id)
        if game["phase"] != "cah" or user_name != game["data"]["judge"]:
            raise BadRequest("Only the judge can vote")
        
        winner = data["winner"]
        if winner not in game["data"]["submissions"]:
            raise BadRequest("Invalid winner selected")
        
        team = next(t for t, members in game["teams"].items() if any(p["name"] == winner for p in members))
        game["scores"][team] += 20
        emit("cah_result", {
            "winner": winner,
            "card": game["data"]["submissions"][winner],
            "scores": dict(game["scores"])
        }, room=game_id)
        logger.info(f"{winner} won CAH round in game {game_id}")
        transition_phase(game_id, "trivia")  # Loop back
    except (BadRequest, NotFound) as e:
        emit("error", {"message": str(e)})

# Game Phase Transitions and Scoring
def transition_phase(game_id, next_phase):
    """Transition to the next game phase."""
    game = get_game_or_404(game_id)
    game["phase"] = next_phase
    game["round"] += 1
    game["data"] = {}
    
    if next_phase == "trivia":
        game["data"] = {
            "question": random.choice(TRIVIA_QUESTIONS),
            "buzz": None,
            "answers": {},
            "time_limit": 30
        }
        emit("phase_change", {
            "phase": "trivia",
            "question": game["data"]["question"]["q"],
            "category": game["data"]["question"]["category"],
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
        game["data"] = {
            "prompt": random.choice(CAH_PROMPTS),
            "judge": judge,
            "submissions": {},
            "time_limit": 60
        }
        emit("phase_change", {
            "phase": "cah",
            "prompt": game["data"]["prompt"],
            "judge": judge,
            "cards": random.sample(CAH_CARDS, min(7, len(CAH_CARDS))),
            "time_limit": game["data"]["time_limit"]
        }, room=game_id)
    logger.info(f"Game {game_id} transitioned to {next_phase}")

def score_scattergories(game_id):
    """Score Scattergories submissions."""
    game = get_game_or_404(game_id)
    submissions = game["data"]["submissions"]
    letter = game["data"]["letter"]
    scores = defaultdict(int)
    
    for category_idx, category in enumerate(game["data"]["categories"]):
        category_words = {}
        for user, words in submissions.items():
            word = words[category_idx] if category_idx < len(words) else ""
            if (word and word[0].upper() == letter and 
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