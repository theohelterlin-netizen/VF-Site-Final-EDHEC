import os
import sqlite3
import hashlib
import secrets
from functools import wraps
from flask import (
    Flask, request, jsonify, send_from_directory,
    session, redirect
)

app = Flask(__name__, static_folder="static")
app.secret_key = os.environ.get("SECRET_KEY", secrets.token_hex(32))

DATABASE = os.path.join(os.path.dirname(__file__), "data", "users.db")
PDF_FOLDER = os.path.join(os.path.dirname(__file__), "data", "files")

def get_db():
    conn = sqlite3.connect(DATABASE)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    os.makedirs(os.path.dirname(DATABASE), exist_ok=True)
    conn = get_db()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL
        )
    """)
    conn.commit()
    default_user = os.environ.get("DEFAULT_USER", "admin")
    default_pass = os.environ.get("DEFAULT_PASS", "edhec2026")
    pw_hash = hashlib.sha256(default_pass.encode()).hexdigest()
    try:
        conn.execute("INSERT INTO users (username, password_hash) VALUES (?, ?)", (default_user, pw_hash))
        conn.commit()
    except sqlite3.IntegrityError:
        pass
    finally:
        conn.close()

def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if "user" not in session:
            return jsonify({"error": "Non autorise"}), 401
        return f(*args, **kwargs)
    return decorated

@app.route("/")
def index():
    return send_from_directory("static", "index.html")

@app.route("/login", methods=["POST"])
def login():
    data = request.get_json(silent=True) or {}
    username = data.get("username", "").strip()
    password = data.get("password", "")
    if not username or not password:
        return jsonify({"error": "Champs requis"}), 400
    pw_hash = hashlib.sha256(password.encode()).hexdigest()
    conn = get_db()
    user = conn.execute("SELECT * FROM users WHERE username = ? AND password_hash = ?", (username, pw_hash)).fetchone()
    conn.close()
    if user:
        session["user"] = username
        return jsonify({"message": "Connecte", "username": username})
    return jsonify({"error": "Identifiants incorrects"}), 401

@app.route("/logout", methods=["POST"])
def logout():
    session.pop("user", None)
    return jsonify({"message": "Deconnecte"})

@app.route("/me")
def me():
    if "user" in session:
        return jsonify({"username": session["user"]})
    return jsonify({"username": None})

@app.route("/pdfs")
@login_required
def list_pdfs():
    os.makedirs(PDF_FOLDER, exist_ok=True)
    files = sorted(f for f in os.listdir(PDF_FOLDER) if f.lower().endswith(".pdf"))
    return jsonify(files)

@app.route("/pdfs/<filename>")
@login_required
def download_pdf(filename):
    return send_from_directory(PDF_FOLDER, filename)

init_db()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=True)
