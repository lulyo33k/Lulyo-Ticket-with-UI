import os
import json
import uuid
import asyncio
import threading
from functools import wraps

from flask import Flask, render_template, request, redirect, url_for, flash, Response
from dotenv import load_dotenv

from bot_runner import TicketBot

load_dotenv()

BOTS_FILE = "bots.json"
CONFIG_DIR = "configs"
os.makedirs(CONFIG_DIR, exist_ok=True)

WEB_USERNAME = os.getenv("WEB_USERNAME", "admin")
WEB_PASSWORD = os.getenv("WEB_PASSWORD", "admin")

app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET_KEY", "change-me-please")

# bot_id -> {"loop":..., "client":..., "status": "running"/"stopped"/"error", "error": str|None}
RUNNING = {}
LOCK = threading.Lock()


# ---------------------------------------------------------------------------
# Authentification basique (le dashboard contient des tokens -> à protéger)
# ---------------------------------------------------------------------------

def check_auth(username, password):
    return username == WEB_USERNAME and password == WEB_PASSWORD


def authenticate():
    return Response(
        "Authentification requise.", 401,
        {"WWW-Authenticate": 'Basic realm="Login Required"'}
    )


def requires_auth(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        auth = request.authorization
        if not auth or not check_auth(auth.username, auth.password):
            return authenticate()
        return f(*args, **kwargs)
    return decorated


# ---------------------------------------------------------------------------
# Stockage des bots (bots.json - pas de base de données)
# ---------------------------------------------------------------------------

def load_bots():
    if not os.path.exists(BOTS_FILE) or os.path.getsize(BOTS_FILE) == 0:
        save_bots([])
        return []
    try:
        with open(BOTS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except json.JSONDecodeError:
        save_bots([])
        return []


def save_bots(bots):
    with open(BOTS_FILE, "w", encoding="utf-8") as f:
        json.dump(bots, f, indent=4, ensure_ascii=False)


def get_bot(bot_id):
    return next((b for b in load_bots() if b["id"] == bot_id), None)


def update_bot(bot_id, **fields):
    bots = load_bots()
    for b in bots:
        if b["id"] == bot_id:
            b.update(fields)
    save_bots(bots)


# ---------------------------------------------------------------------------
# Cycle de vie des bots (1 thread + 1 boucle asyncio par bot)
# ---------------------------------------------------------------------------

def _run_bot_thread(bot_id, token, config_path):
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    client = TicketBot(config_path)

    with LOCK:
        RUNNING[bot_id] = {"loop": loop, "client": client, "status": "running", "error": None}

    error_message = None
    try:
        loop.run_until_complete(client.start(token))
    except Exception as e:
        error_message = str(e)
        print(f"[{bot_id}] Erreur : {e}")
    finally:
        try:
            if not client.is_closed():
                loop.run_until_complete(client.close())
        except Exception:
            pass
        loop.close()
        with LOCK:
            if bot_id in RUNNING:
                RUNNING[bot_id]["status"] = "error" if error_message else "stopped"
                RUNNING[bot_id]["error"] = error_message


def start_bot_instance(bot_id):
    with LOCK:
        if bot_id in RUNNING and RUNNING[bot_id]["status"] == "running":
            return False, "Ce bot est déjà démarré."

    bot_info = get_bot(bot_id)
    if bot_info is None:
        return False, "Bot introuvable."

    thread = threading.Thread(
        target=_run_bot_thread,
        args=(bot_id, bot_info["token"], bot_info["config_file"]),
        daemon=True,
    )
    thread.start()
    return True, "Bot en cours de démarrage."


def stop_bot_instance(bot_id):
    with LOCK:
        info = RUNNING.get(bot_id)

    if info is None or info.get("status") != "running":
        return False, "Ce bot n'est pas démarré."

    try:
        future = asyncio.run_coroutine_threadsafe(info["client"].close(), info["loop"])
        future.result(timeout=15)
    except Exception as e:
        return False, f"Erreur lors de l'arrêt : {e}"

    return True, "Bot arrêté."


def get_status(bot_id):
    with LOCK:
        info = RUNNING.get(bot_id)
    if info is None:
        return "stopped", None
    return info.get("status", "stopped"), info.get("error")


# ---------------------------------------------------------------------------
# Routes Flask
# ---------------------------------------------------------------------------

@app.route("/")
@requires_auth
def index():
    bots = load_bots()
    for b in bots:
        status, error = get_status(b["id"])
        b["status"] = status
        b["error"] = error
    return render_template("index.html", bots=bots)


@app.route("/add", methods=["GET", "POST"])
@requires_auth
def add_bot():
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        token = request.form.get("token", "").strip()

        if not name or not token:
            flash("Le nom et le token sont obligatoires.", "error")
            return redirect(url_for("add_bot"))

        bot_id = uuid.uuid4().hex[:10]
        config_path = os.path.join(CONFIG_DIR, f"{bot_id}.json")

        with open(config_path, "w", encoding="utf-8") as f:
            json.dump({"panels": {}, "tickets": {}}, f, indent=4)

        bots = load_bots()
        bots.append({"id": bot_id, "name": name, "token": token, "config_file": config_path})
        save_bots(bots)

        flash(f"Bot « {name} » ajouté avec succès.", "success")
        return redirect(url_for("index"))

    return render_template("form.html", bot=None, action=url_for("add_bot"))


@app.route("/edit/<bot_id>", methods=["GET", "POST"])
@requires_auth
def edit_bot(bot_id):
    bot_info = get_bot(bot_id)
    if bot_info is None:
        flash("Bot introuvable.", "error")
        return redirect(url_for("index"))

    if request.method == "POST":
        name = request.form.get("name", "").strip()
        token = request.form.get("token", "").strip()

        if not name or not token:
            flash("Le nom et le token sont obligatoires.", "error")
            return redirect(url_for("edit_bot", bot_id=bot_id))

        status, _ = get_status(bot_id)
        if status == "running":
            flash("Arrêtez le bot avant de modifier son token.", "error")
            return redirect(url_for("edit_bot", bot_id=bot_id))

        update_bot(bot_id, name=name, token=token)
        flash("Bot mis à jour.", "success")
        return redirect(url_for("index"))

    return render_template("form.html", bot=bot_info, action=url_for("edit_bot", bot_id=bot_id))


@app.route("/start/<bot_id>", methods=["POST"])
@requires_auth
def start_bot_route(bot_id):
    ok, message = start_bot_instance(bot_id)
    flash(message, "success" if ok else "error")
    return redirect(url_for("index"))


@app.route("/stop/<bot_id>", methods=["POST"])
@requires_auth
def stop_bot_route(bot_id):
    ok, message = stop_bot_instance(bot_id)
    flash(message, "success" if ok else "error")
    return redirect(url_for("index"))


@app.route("/delete/<bot_id>", methods=["POST"])
@requires_auth
def delete_bot_route(bot_id):
    status, _ = get_status(bot_id)
    if status == "running":
        stop_bot_instance(bot_id)

    bot_info = get_bot(bot_id)
    if bot_info:
        try:
            os.remove(bot_info["config_file"])
        except OSError:
            pass

    bots = [b for b in load_bots() if b["id"] != bot_id]
    save_bots(bots)

    with LOCK:
        RUNNING.pop(bot_id, None)

    flash("Bot supprimé.", "success")
    return redirect(url_for("index"))


if __name__ == "__main__":
    port = int(os.getenv("WEB_PORT", "5000"))
    app.run(host="0.0.0.0", port=port, threaded=True)