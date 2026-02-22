import os
import json
import hashlib
import secrets
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

DATABASE_URL = os.environ.get("DATABASE_URL")

# --- DB helpers ---

def get_db():
    conn = psycopg2.connect(DATABASE_URL, cursor_factory=RealDictCursor)
    return conn


def init_db():
    conn = get_db()
    cur = conn.cursor()

    cur.execute("""
        CREATE TABLE IF NOT EXISTS kv_store (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            updated_at TIMESTAMP DEFAULT NOW()
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS pdf_files (
            id SERIAL PRIMARY KEY,
            filename TEXT UNIQUE NOT NULL,
            data BYTEA NOT NULL,
            uploaded_at TIMESTAMP DEFAULT NOW()
        )
    """)

    conn.commit()
    cur.close()
    conn.close()


# --- Sync API (localStorage <-> PostgreSQL) ---

@app.route("/api/sync/pull", methods=["GET"])
def sync_pull():
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT key, value FROM kv_store ORDER BY key")
    rows = cur.fetchall()
    cur.close()
    conn.close()
    result = {}
    for row in rows:
        result[row["key"]] = row["value"]
    return jsonify(result)


@app.route("/api/sync/push", methods=["POST"])
def sync_push():
    data = request.get_json(silent=True) or {}
    conn = get_db()
    cur = conn.cursor()
    for key, value in data.items():
        cur.execute(
            """INSERT INTO kv_store (key, value)
               VALUES (%s, %s)
               ON CONFLICT (key)
               DO UPDATE SET value = EXCLUDED.value,
                             updated_at = NOW()""",
            (key, value)
        )
    conn.commit()
    cur.close()
    conn.close()
    return jsonify({"ok": True, "saved": len(data)})


@app.route("/api/sync/delete", methods=["POST"])
def sync_delete():
    data = request.get_json(silent=True) or {}
    key = data.get("key")
    if not key:
        return jsonify({"error": "key required"}), 400
    conn = get_db()
    cur = conn.cursor()
    cur.execute("DELETE FROM kv_store WHERE key = %s", (key,))
    conn.commit()
    cur.close()
    conn.close()
    return jsonify({"ok": True})


# --- PDF management ---

@app.route("/pdfs")
def list_pdfs():
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT filename FROM pdf_files ORDER BY filename")
    files = [row["filename"] for row in cur.fetchall()]
    cur.close()
    conn.close()
    return jsonify(files)


@app.route("/pdfs/<filename>")
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


# --- Serve index.html with ServerSync patch ---

def build_patch_script():
    js = r"""
(function(){
    var PREFIX = 'edhec_';
    window.ServerSync = {
        enabled: true,
        init: function(){
            return fetch('/api/sync/pull')
                .then(function(r){ return r.json(); })
                .then(function(data){
                    var keys = Object.keys(data);
                    for(var i=0; i<keys.length; i++){
                        localStorage.setItem(keys[i], data[keys[i]]);
                    }
                    if(keys.length > 0){
                        console.log('[ServerSync] Pulled ' + keys.length + ' keys from server');
                    }
                    ServerSync.enabled = true;
                })
                .catch(function(e){
                    console.warn('[ServerSync] Pull failed, working offline', e);
                    ServerSync.enabled = false;
                });
        },
        push: function(k, v){
            if(!ServerSync.enabled) return Promise.resolve();
            var payload = {};
            payload[PREFIX + k] = JSON.stringify(v);
            return fetch('/api/sync/push', {
                method: 'POST',
                headers: {'Content-Type':'application/json'},
                body: JSON.stringify(payload)
            }).catch(function(e){
                console.warn('[ServerSync] Push failed for key:', k, e);
            });
        },
        pullAll: function(){ return this.init(); },
        pushFile: function(){ return Promise.resolve(); },
        pushAll: function(){
            var payload = {};
            var count = 0;
            for(var i=0; i<localStorage.length; i++){
                var key = localStorage.key(i);
                if(key.startsWith(PREFIX)){
                    payload[key] = localStorage.getItem(key);
                    count++;
                }
            }
            if(count === 0) return Promise.resolve();
            return fetch('/api/sync/push', {
                method: 'POST',
                headers: {'Content-Type':'application/json'},
                body: JSON.stringify(payload)
            }).then(function(r){ return r.json(); })
            .then(function(d){
                console.log('[ServerSync] Pushed ' + count + ' keys to server');
            })
            .catch(function(e){
                console.warn('[ServerSync] PushAll failed', e);
            });
        }
    };
    window.addEventListener('load', function(){
        setTimeout(function(){
            ServerSync.init().then(function(){
                fetch('/api/sync/pull')
                    .then(function(r){ return r.json(); })
                    .then(function(data){
                        if(Object.keys(data).length === 0){
                            console.log('[ServerSync] Server empty, pushing local data...');
                            ServerSync.pushAll();
                        }
                    });
            });
        }, 500);
    });
})();
"""
    return "<" + "script>" + js + "</" + "script>"


@app.route("/")
def index():
    html_path = os.path.join(app.static_folder, "index.html")
    with open(html_path, "r", encoding="utf-8") as f:
        html = f.read()
    patch = build_patch_script()
    html = html.replace("</head>", patch + "\n</head>", 1)
    return Response(html, mimetype="text/html")


# --- Init & Run ---

init_db()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=True)

