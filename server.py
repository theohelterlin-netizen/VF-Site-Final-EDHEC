import os
import hashlib
import secrets
import base64
from functools import wraps
from io import BytesIO

import psycopg2
from psycopg2.extras import RealDictCursor
from flask import (
    Flask, request, jsonify, send_from_directory,
    session, send_file, Response
)

app = Flask(__name__, static_folder="static")
app.secret_key = os.environ.get("SECRET_KEY", secrets.token_hex(32))

DATABASE_URL = os.environ.get("DATABASE_URL")  # fourni par Render PostgreSQL

# --- helpers DB ---

def get_db():
    conn = psycopg2.connect(DATABASE_URL, cursor_factory=RealDictCursor)
    return conn


def init_db():
    conn = get_db()
    cur = conn.cursor()

    # Table utilisateurs
    cur.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id SERIAL PRIMARY KEY,
            username TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL
        )
    """)

    # Table fichiers PDF (stockes en binaire dans la base)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS pdf_files (
            id SERIAL PRIMARY KEY,
            filename TEXT UNIQUE NOT NULL,
            data BYTEA NOT NULL,
            uploaded_at TIMESTAMP DEFAULT NOW()
        )
    """)

    # Table progression utilisateur
    cur.execute("""
        CREATE TABLE IF NOT EXISTS user_progress (
            id SERIAL PRIMARY KEY,
            username TEXT NOT NULL,
            item_key TEXT NOT NULL,
            item_value TEXT,
            updated_at TIMESTAMP DEFAULT NOW(),
            UNIQUE(username, item_key)
        )
    """)

    conn.commit()

    # Creer l'utilisateur par defaut s'il n'existe pas
    default_user = os.environ.get("DEFAULT_USER", "admin")
    default_pass = os.environ.get("DEFAULT_PASS", "edhec2026")
    pw_hash = hashlib.sha256(default_pass.encode()).hexdigest()
    try:
        cur.execute(
            "INSERT INTO users (username, password_hash) VALUES (%s, %s)",
            (default_user, pw_hash)
        )
        conn.commit()
    except psycopg2.errors.UniqueViolation:
        conn.rollback()
    finally:
        cur.close()
        conn.close()


# --- decorateur auth ---

def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if "user" not in session:
            return jsonify({"error": "Non autorise"}), 401
        return f(*args, **kwargs)
    return decorated


# --- routes pages ---

PATCH_SCRIPT = """<script>
window.ServerSync={
    enabled:false,
    init:function(){return Promise.resolve()},
    pullAll:function(){return Promise.resolve()},
    push:function(){return Promise.resolve()},
    pushFile:function(){return Promise.resolve()}
};
</script>"""

@app.route("/")
def index():
    html_path = os.path.join(app.static_folder, "index.html")
    with open(html_path, "r", encoding="utf-8") as f:
        html = f.read()
    html = html.replace("</head>", PATCH_SCRIPT + "\n</head>", 1)
    return Response(html, mimetype="text/html")


# --- auth ---

@app.route("/login", methods=["POST"])
def login():
    data = request.get_json(silent=True) or {}
    username = data.get("username", "").strip()
    password = data.get("password", "")

    if not username or not password:
        return jsonify({"error": "Champs requis"}), 400

    pw_hash = hashlib.sha256(password.encode()).hexdigest()
    conn = get_db()
    cur = conn.cursor()
    cur.execute(
        "SELECT * FROM users WHERE username = %s AND password_hash = %s",
        (username, pw_hash)
    )
    user = cur.fetchone()
    cur.close()
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


# --- PDF management (stocke en base PostgreSQL) ---

@app.route("/pdfs")
@login_required
def list_pdfs():
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT filename FROM pdf_files ORDER BY filename")
    files = [row["filename"] for row in cur.fetchall()]
    cur.close()
    conn.close()
    return jsonify(files)


@app.route("/pdfs/<filename>")
@login_required
def download_pdf(filename):
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT data FROM pdf_files WHERE filename = %s", (filename,))
    row = cur.fetchone()
    cur.close()
    conn.close()

    if row is None:
        return jsonify({"error": "Fichier non trouve"}), 404

    return send_file(
        BytesIO(bytes(row["data"])),
        mimetype="application/pdf",
        as_attachment=False,
        download_name=filename
    )


@app.route("/pdfs/upload", methods=["POST"])
@login_required
def upload_pdf():
    if "file" not in request.files:
        return jsonify({"error": "Aucun fichier"}), 400

    f = request.files["file"]
    if not f.filename.lower().endswith(".pdf"):
        return jsonify({"error": "Seuls les fichiers PDF sont acceptes"}), 400

    data = f.read()
    conn = get_db()
    cur = conn.cursor()
    try:
        cur.execute(
            """INSERT INTO pdf_files (filename, data)
               VALUES (%s, %s)
               ON CONFLICT (filename)
               DO UPDATE SET data = EXCLUDED.data, uploaded_at = NOW()""",
            (f.filename, psycopg2.Binary(data))
        )
        conn.commit()
    finally:
        cur.close()
        conn.close()

    return jsonify({"message": "PDF uploade", "filename": f.filename})


@app.route("/pdfs/<filename>", methods=["DELETE"])
@login_required
def delete_pdf(filename):
    conn = get_db()
    cur = conn.cursor()
    cur.execute("DELETE FROM pdf_files WHERE filename = %s", (filename,))
    deleted = cur.rowcount
    conn.commit()
    cur.close()
    conn.close()

    if deleted:
        return jsonify({"message": "Supprime"})
    return jsonify({"error": "Fichier non trouve"}), 404


# --- progression utilisateur ---

@app.route("/progress", methods=["GET"])
@login_required
def get_progress():
    username = session["user"]
    conn = get_db()
    cur = conn.cursor()
    cur.execute(
        "SELECT item_key, item_value FROM user_progress WHERE username = %s",
        (username,)
    )
    progress = {row["item_key"]: row["item_value"] for row in cur.fetchall()}
    cur.close()
    conn.close()
    return jsonify(progress)


@app.route("/progress", methods=["POST"])
@login_required
def save_progress():
    username = session["user"]
    data = request.get_json(silent=True) or {}

    conn = get_db()
    cur = conn.cursor()
    for key, value in data.items():
        cur.execute(
            """INSERT INTO user_progress (username, item_key, item_value)
               VALUES (%s, %s, %s)
               ON CONFLICT (username, item_key)
               DO UPDATE SET item_value = EXCLUDED.item_value,
                             updated_at = NOW()""",
            (username, key, str(value))
        )
    conn.commit()
    cur.close()
    conn.close()
    return jsonify({"message": "Progression sauvegardee"})


# --- init & run ---

init_db()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=True)
