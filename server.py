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

# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------
def get_db():
    conn = psycopg2.connect(DATABASE_URL, cursor_factory=RealDictCursor)
    return conn

def init_db():
    conn = get_db()
    cur  = conn.cursor()

    # Key-value store : miroir du localStorage du frontend
    cur.execute("""
        CREATE TABLE IF NOT EXISTS kv_store (
            key   TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            updated_at TIMESTAMP DEFAULT NOW()
        )
    """)

    # Table fichiers PDF (stockes en binaire dans la base)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS pdf_files (
            id         SERIAL PRIMARY KEY,
            filename   TEXT UNIQUE NOT NULL,
            data       BYTEA NOT NULL,
            uploaded_at TIMESTAMP DEFAULT NOW()
        )
    """)
    conn.commit()

    # Table fichiers generaux (PDF, Excel, images - stockes en binaire)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS general_files (
            fid        TEXT PRIMARY KEY,
            filename   TEXT NOT NULL,
            mimetype   TEXT NOT NULL,
            data       BYTEA NOT NULL,
            size       INTEGER DEFAULT 0,
            uploaded_at TIMESTAMP DEFAULT NOW()
        )
    """)
    conn.commit()

    # Table pour les examens par utilisateur (dates propres a chaque compte)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS user_exams (
            user_email TEXT PRIMARY KEY,
            exams_data TEXT NOT NULL DEFAULT '[]',
            updated_at TIMESTAMP DEFAULT NOW()
        )
    """)
    conn.commit()

    # Table pour les annonces du site (changelog / infos)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS site_announcements (
            id         SERIAL PRIMARY KEY,
            title      TEXT NOT NULL,
            content    TEXT NOT NULL,
            author     TEXT DEFAULT '',
            created_at TIMESTAMP DEFAULT NOW(),
            updated_at TIMESTAMP DEFAULT NOW()
        )
    """)
    conn.commit()

    cur.close()
    conn.close()

# ---------------------------------------------------------------------------
# Sync API (remplace le localStorage par PostgreSQL)
# ---------------------------------------------------------------------------
@app.route("/api/sync/pull", methods=["GET"])
def sync_pull():
    """Renvoie TOUTES les cles/valeurs stockees en base."""
    conn = get_db()
    cur  = conn.cursor()
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
    """Recoit une ou plusieurs cles a sauvegarder."""
    data = request.get_json(silent=True) or {}
    conn = get_db()
    cur  = conn.cursor()
    for key, value in data.items():
        cur.execute(
            """INSERT INTO kv_store (key, value)
               VALUES (%s, %s)
               ON CONFLICT (key) DO UPDATE
               SET value = EXCLUDED.value, updated_at = NOW()""",
            (key, value)
        )
    conn.commit()
    cur.close()
    conn.close()
    return jsonify({"ok": True, "saved": len(data)})

@app.route("/api/sync/delete", methods=["POST"])
def sync_delete():
    """Supprime une cle."""
    data = request.get_json(silent=True) or {}
    key  = data.get("key")
    if not key:
        return jsonify({"error": "key required"}), 400
    conn = get_db()
    cur  = conn.cursor()
    cur.execute("DELETE FROM kv_store WHERE key = %s", (key,))
    conn.commit()
    cur.close()
    conn.close()
    return jsonify({"ok": True})

# ---------------------------------------------------------------------------
# PDF management (stocke en base PostgreSQL)
# ---------------------------------------------------------------------------
@app.route("/pdfs")
def list_pdfs():
    conn = get_db()
    cur  = conn.cursor()
    cur.execute("SELECT filename FROM pdf_files ORDER BY filename")
    files = [row["filename"] for row in cur.fetchall()]
    cur.close()
    conn.close()
    return jsonify(files)

@app.route("/pdfs/<filename>")
def download_pdf(filename):
    conn = get_db()
    cur  = conn.cursor()
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
    cur  = conn.cursor()
    try:
        cur.execute(
            """INSERT INTO pdf_files (filename, data)
               VALUES (%s, %s)
               ON CONFLICT (filename) DO UPDATE
               SET data = EXCLUDED.data, uploaded_at = NOW()""",
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
    cur  = conn.cursor()
    cur.execute("DELETE FROM pdf_files WHERE filename = %s", (filename,))
    deleted = cur.rowcount
    conn.commit()
    cur.close()
    conn.close()
    if deleted:
        return jsonify({"message": "Supprime"})
    return jsonify({"error": "Fichier non trouve"}), 404

# ---------------------------------------------------------------------------
# General Files API (synchronise IndexedDB <-> PostgreSQL)
# ---------------------------------------------------------------------------
@app.route("/api/files/upload", methods=["POST"])
def upload_general_file():
    """Upload un fichier (PDF, Excel, image...) avec son fid IndexedDB."""
    fid = request.form.get("fid")
    if not fid or "file" not in request.files:
        return jsonify({"error": "fid et file requis"}), 400
    f = request.files["file"]
    data = f.read()
    mimetype = f.content_type or "application/octet-stream"
    conn = get_db()
    cur  = conn.cursor()
    try:
        cur.execute(
            """INSERT INTO general_files (fid, filename, mimetype, data, size)
               VALUES (%s, %s, %s, %s, %s)
               ON CONFLICT (fid) DO UPDATE
               SET filename = EXCLUDED.filename, mimetype = EXCLUDED.mimetype,
                   data = EXCLUDED.data, size = EXCLUDED.size, uploaded_at = NOW()""",
            (fid, f.filename, mimetype, psycopg2.Binary(data), len(data))
        )
        conn.commit()
    finally:
        cur.close()
        conn.close()
    return jsonify({"ok": True, "fid": fid, "filename": f.filename, "size": len(data)})

@app.route("/api/files/<fid>", methods=["GET"])
def get_general_file(fid):
    """Telecharge un fichier par son fid."""
    conn = get_db()
    cur  = conn.cursor()
    cur.execute("SELECT filename, mimetype, data FROM general_files WHERE fid = %s", (fid,))
    row = cur.fetchone()
    cur.close()
    conn.close()
    if row is None:
        return jsonify({"error": "Fichier non trouve"}), 404
    return send_file(
        BytesIO(bytes(row["data"])),
        mimetype=row["mimetype"],
        as_attachment=False,
        download_name=row["filename"]
    )

@app.route("/api/files/<fid>/meta", methods=["GET"])
def get_general_file_meta(fid):
    """Renvoie les metadonnees d un fichier (sans le contenu binaire)."""
    conn = get_db()
    cur  = conn.cursor()
    cur.execute("SELECT fid, filename, mimetype, size FROM general_files WHERE fid = %s", (fid,))
    row = cur.fetchone()
    cur.close()
    conn.close()
    if row is None:
        return jsonify({"exists": False}), 404
    return jsonify({"exists": True, "fid": row["fid"], "filename": row["filename"],
                     "mimetype": row["mimetype"], "size": row["size"]})

@app.route("/api/files/<fid>", methods=["DELETE"])
def delete_general_file(fid):
    """Supprime un fichier."""
    conn = get_db()
    cur  = conn.cursor()
    cur.execute("DELETE FROM general_files WHERE fid = %s", (fid,))
    deleted = cur.rowcount
    conn.commit()
    cur.close()
    conn.close()
    if deleted:
        return jsonify({"ok": True})
    return jsonify({"error": "Fichier non trouve"}), 404

# ---------------------------------------------------------------------------
# Per-user Exam Dates API
# ---------------------------------------------------------------------------
@app.route("/api/user/exams", methods=["GET"])
def get_user_exams():
    email = request.args.get("email", "").strip().lower()
    if not email:
        return jsonify({"error": "email required"}), 400
    conn = get_db()
    cur  = conn.cursor()
    cur.execute("SELECT exams_data FROM user_exams WHERE user_email = %s", (email,))
    row = cur.fetchone()
    cur.close()
    conn.close()
    if row:
        return jsonify({"exams": json.loads(row["exams_data"])})
    return jsonify({"exams": []})

@app.route("/api/user/exams", methods=["POST"])
def save_user_exams():
    data  = request.get_json(silent=True) or {}
    email = data.get("email", "").strip().lower()
    exams = data.get("exams", [])
    if not email:
        return jsonify({"error": "email required"}), 400
    conn = get_db()
    cur  = conn.cursor()
    cur.execute(
        """INSERT INTO user_exams (user_email, exams_data)
           VALUES (%s, %s)
           ON CONFLICT (user_email) DO UPDATE
           SET exams_data = EXCLUDED.exams_data, updated_at = NOW()""",
        (email, json.dumps(exams))
    )
    conn.commit()
    cur.close()
    conn.close()
    return jsonify({"ok": True})

# ---------------------------------------------------------------------------
# Site Announcements API (changelog / infos)
# ---------------------------------------------------------------------------
@app.route("/api/announcements", methods=["GET"])
def list_announcements():
    conn = get_db()
    cur  = conn.cursor()
    cur.execute("""SELECT id, title, content, author, created_at, updated_at
                   FROM site_announcements ORDER BY created_at DESC""")
    rows = cur.fetchall()
    cur.close()
    conn.close()
    result = []
    for r in rows:
        result.append({
            "id": r["id"],
            "title": r["title"],
            "content": r["content"],
            "author": r["author"],
            "created_at": r["created_at"].isoformat() if r["created_at"] else None,
            "updated_at": r["updated_at"].isoformat() if r["updated_at"] else None
        })
    return jsonify(result)

@app.route("/api/announcements", methods=["POST"])
def create_announcement():
    data    = request.get_json(silent=True) or {}
    title   = data.get("title", "").strip()
    content = data.get("content", "").strip()
    author  = data.get("author", "")
    if not title or not content:
        return jsonify({"error": "title and content required"}), 400
    conn = get_db()
    cur  = conn.cursor()
    cur.execute(
        "INSERT INTO site_announcements (title, content, author) VALUES (%s, %s, %s) RETURNING id",
        (title, content, author)
    )
    new_id = cur.fetchone()["id"]
    conn.commit()
    cur.close()
    conn.close()
    return jsonify({"ok": True, "id": new_id})

@app.route("/api/announcements/<int:ann_id>", methods=["PUT"])
def update_announcement(ann_id):
    data    = request.get_json(silent=True) or {}
    title   = data.get("title", "").strip()
    content = data.get("content", "").strip()
    if not title or not content:
        return jsonify({"error": "title and content required"}), 400
    conn = get_db()
    cur  = conn.cursor()
    cur.execute(
        "UPDATE site_announcements SET title=%s, content=%s, updated_at=NOW() WHERE id=%s",
        (title, content, ann_id)
    )
    conn.commit()
    cur.close()
    conn.close()
    return jsonify({"ok": True})

@app.route("/api/announcements/<int:ann_id>", methods=["DELETE"])
def delete_announcement(ann_id):
    conn = get_db()
    cur  = conn.cursor()
    cur.execute("DELETE FROM site_announcements WHERE id = %s", (ann_id,))
    deleted = cur.rowcount
    conn.commit()
    cur.close()
    conn.close()
    if deleted:
        return jsonify({"ok": True})
    return jsonify({"error": "not found"}), 404

# ---------------------------------------------------------------------------
# Serve index.html with all patches injected
# ---------------------------------------------------------------------------

def build_branding_patch():
    """Renomme le site de Reussir l EDHEC a Reussir Etudes dans le DOM."""
    js = r"""
(function(){
    function renameSite(){
        document.title = document.title.replace(/R\u00e9ussir l'EDHEC/g, 'R\u00e9ussir \u00c9tudes');
        var walk = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT, null, false);
        while(walk.nextNode()){
            var n = walk.currentNode;
            if(n.nodeValue.indexOf("R\u00e9ussir l'EDHEC") !== -1){
                n.nodeValue = n.nodeValue.replace(/R\u00e9ussir l'EDHEC/g, 'R\u00e9ussir \u00c9tudes');
            }
            if(n.nodeValue.indexOf("R\u00e9ussir l\\'EDHEC") !== -1){
                n.nodeValue = n.nodeValue.replace(/R\u00e9ussir l\\'EDHEC/g, 'R\u00e9ussir \u00c9tudes');
            }
        }
        document.querySelectorAll('[placeholder],[title]').forEach(function(el){
            if(el.placeholder && el.placeholder.indexOf('EDHEC')!==-1) el.placeholder = el.placeholder.replace(/EDHEC/g,'\u00c9tudes');
            if(el.title && el.title.indexOf('EDHEC')!==-1) el.title = el.title.replace(/EDHEC/g,'\u00c9tudes');
        });
    }
    if(document.readyState==='loading'){
        document.addEventListener('DOMContentLoaded', function(){ setTimeout(renameSite, 100); });
    } else { setTimeout(renameSite, 100); }
    var _renameTimer = null;
    new MutationObserver(function(){
        clearTimeout(_renameTimer);
        _renameTimer = setTimeout(renameSite, 200);
    }).observe(document.body, {childList:true, subtree:true, characterData:true});
})();
"""
    return "<" + "script>" + js + "</" + "script>"


def build_annales_patch():
    """Ajoute les fonctionnalites admin pour les annales."""
    js = r"""
(function(){
    var style = document.createElement('style');
    style.textContent = '\
    .ann-item { position:relative; transition: transform 0.2s, box-shadow 0.2s; }\
    .ann-item.dragging { opacity:0.5; transform:scale(0.97); box-shadow:0 8px 32px rgba(0,0,0,0.3); z-index:100; }\
    .ann-item.drag-over-top { border-top:3px solid #E07A5F !important; }\
    .ann-item.drag-over-bottom { border-bottom:3px solid #E07A5F !important; }\
    .ann-admin-actions { display:inline-flex; gap:6px; margin-left:8px; align-items:center; }\
    .ann-admin-actions button { background:transparent; border:1px solid rgba(255,255,255,0.2); color:rgba(255,255,255,0.7); border-radius:6px; padding:3px 8px; font-size:0.75rem; cursor:pointer; transition:all 0.2s; }\
    .ann-admin-actions button:hover { background:rgba(255,255,255,0.1); color:#fff; }\
    .ann-admin-actions button.del:hover { background:rgba(224,90,90,0.2); color:#E07A5F; border-color:#E07A5F; }\
    .ann-drag-handle { cursor:grab; padding:4px 6px; color:rgba(255,255,255,0.4); font-size:1.1rem; user-select:none; }\
    .ann-drag-handle:active { cursor:grabbing; }\
    ';
    document.head.appendChild(style);

    function getAnnalesKey(slug){
        var possibleKeys = ['annales_' + slug, 'ct_' + slug + '_annales'];
        for(var i=0; i<possibleKeys.length; i++){
            var val = DB.get(possibleKeys[i]);
            if(val !== null && val !== undefined) return possibleKeys[i];
        }
        for(var j=0; j<localStorage.length; j++){
            var key = localStorage.key(j);
            if(key.indexOf('annale') !== -1 && key.indexOf(slug) !== -1){
                return key.replace('edhec_','');
            }
        }
        return 'annales_' + slug;
    }

    window.renameAnnale = function(slug, idx){
        var key = getAnnalesKey(slug);
        var list = DB.get(key) || [];
        if(idx < 0 || idx >= list.length) return;
        var oldName = list[idx].name || 'Sans nom';
        var newName = prompt('Renommer cette annale :', oldName);
        if(newName !== null && newName.trim() !== ''){
            list[idx].name = newName.trim();
            DB.set(key, list);
            if(typeof refreshAnnalesTab === 'function') refreshAnnalesTab(slug);
            if(typeof toast === 'function') toast('Annale renommee');
        }
    };

    window.deleteAnnaleEnhanced = function(slug, idx){
        var key = getAnnalesKey(slug);
        var list = DB.get(key) || [];
        if(idx < 0 || idx >= list.length) return;
        var name = list[idx].name || 'Sans nom';
        if(!confirm('Supprimer "' + name + '" ?')) return;
        if(list[idx].sujetId && typeof FDB !== 'undefined'){ try { FDB.del(list[idx].sujetId); } catch(e){} }
        if(list[idx].corrId && typeof FDB !== 'undefined'){ try { FDB.del(list[idx].corrId); } catch(e){} }
        list.splice(idx, 1);
        DB.set(key, list);
        if(typeof refreshAnnalesTab === 'function') refreshAnnalesTab(slug);
        if(typeof toast === 'function') toast('Annale supprimee');
    };

    window.changeAnnaleType = function(slug, idx){
        var key = getAnnalesKey(slug);
        var list = DB.get(key) || [];
        if(idx < 0 || idx >= list.length) return;
        var currentType = list[idx].type || 'finals';
        var types = ['finals', 'midterms', 'rattrapages', 'partiels', 'td', 'autres'];
        var msg = 'Choisir le statut :\n';
        for(var t=0; t<types.length; t++) msg += (t+1) + '. ' + types[t] + (types[t]===currentType?' (actuel)':'') + '\n';
        var choice = prompt(msg, currentType);
        if(choice === null) return;
        choice = choice.trim().toLowerCase();
        var num = parseInt(choice);
        if(num >= 1 && num <= types.length) choice = types[num-1];
        if(choice !== ''){
            list[idx].type = choice;
            DB.set(key, list);
            if(typeof refreshAnnalesTab === 'function') refreshAnnalesTab(slug);
            if(typeof toast === 'function') toast('Statut modifie : ' + choice);
        }
    };

    window.moveAnnale = function(slug, fromIdx, toIdx){
        var key = getAnnalesKey(slug);
        var list = DB.get(key) || [];
        if(fromIdx < 0 || fromIdx >= list.length || toIdx < 0 || toIdx >= list.length) return;
        var item = list.splice(fromIdx, 1)[0];
        list.splice(toIdx, 0, item);
        DB.set(key, list);
        if(typeof refreshAnnalesTab === 'function') refreshAnnalesTab(slug);
    };

    window.initAnnalesDragDrop = function(slug){
        var container = document.getElementById('annales-tab-' + slug);
        if(!container) return;
        var items = container.querySelectorAll('.ann-item');
        var dragSrcIdx = null;
        items.forEach(function(item, idx){
            var touchTimer = null; var isDragging = false; var touchStartY = 0;
            item.addEventListener('touchstart', function(e){
                touchStartY = e.touches[0].clientY;
                touchTimer = setTimeout(function(){
                    isDragging = true; dragSrcIdx = idx;
                    item.classList.add('dragging');
                    if(navigator.vibrate) navigator.vibrate(50);
                }, 400);
            }, {passive:true});
            item.addEventListener('touchmove', function(e){
                if(!isDragging){ if(Math.abs(e.touches[0].clientY - touchStartY) > 10) clearTimeout(touchTimer); return; }
                e.preventDefault();
                var touchY = e.touches[0].clientY;
                items.forEach(function(oi, oidx){
                    oi.classList.remove('drag-over-top', 'drag-over-bottom');
                    if(oidx === idx) return;
                    var rect = oi.getBoundingClientRect();
                    if(touchY > rect.top && touchY < rect.bottom){
                        if(touchY < rect.top + rect.height/2) oi.classList.add('drag-over-top');
                        else oi.classList.add('drag-over-bottom');
                    }
                });
            }, {passive:false});
            item.addEventListener('touchend', function(){
                clearTimeout(touchTimer);
                if(!isDragging) return;
                isDragging = false; item.classList.remove('dragging');
                var targetIdx = -1;
                items.forEach(function(oi, oidx){
                    if(oi.classList.contains('drag-over-top') || oi.classList.contains('drag-over-bottom')) targetIdx = oidx;
                    oi.classList.remove('drag-over-top', 'drag-over-bottom');
                });
                if(targetIdx >= 0 && targetIdx !== dragSrcIdx) moveAnnale(slug, dragSrcIdx, targetIdx);
            });
            item.setAttribute('draggable', 'true');
            item.addEventListener('dragstart', function(e){
                dragSrcIdx = idx; item.classList.add('dragging');
                e.dataTransfer.effectAllowed = 'move'; e.dataTransfer.setData('text/plain', idx);
            });
            item.addEventListener('dragend', function(){ item.classList.remove('dragging'); items.forEach(function(it){ it.classList.remove('drag-over-top','drag-over-bottom'); }); });
            item.addEventListener('dragover', function(e){
                e.preventDefault(); e.dataTransfer.dropEffect = 'move';
                var rect = item.getBoundingClientRect();
                items.forEach(function(it){ it.classList.remove('drag-over-top','drag-over-bottom'); });
                if(e.clientY < rect.top+rect.height/2) item.classList.add('drag-over-top');
                else item.classList.add('drag-over-bottom');
            });
            item.addEventListener('dragleave', function(){ item.classList.remove('drag-over-top','drag-over-bottom'); });
            item.addEventListener('drop', function(e){
                e.preventDefault();
                items.forEach(function(it){ it.classList.remove('drag-over-top','drag-over-bottom'); });
                var fromIdx = parseInt(e.dataTransfer.getData('text/plain'));
                if(fromIdx !== idx) moveAnnale(slug, fromIdx, idx);
            });
        });
    };

    function enhanceAnnalesUI(){
        if(typeof Auth === 'undefined' || !Auth.admin()) return;
        document.querySelectorAll('[id^="annales-tab-"]').forEach(function(container){
            if(container.querySelectorAll('.ann-admin-actions').length > 0) return;
            var slug = container.id.replace('annales-tab-','');
            var items = container.querySelectorAll('.card.ani');
            if(items.length === 0){
                var btns = container.querySelectorAll('[onclick*="downloadAnnaleFile"]');
                var cardSet = new Set();
                btns.forEach(function(b){ var c = b.closest('.card'); if(c) cardSet.add(c); });
                items = Array.from(cardSet);
            }
            var key = getAnnalesKey(slug);
            var list = DB.get(key) || [];
            Array.from(items).forEach(function(item, idx){
                if(item.querySelector('.ann-admin-actions')) return;
                if(idx >= list.length) return;
                item.classList.add('ann-item');
                var actions = document.createElement('span');
                actions.className = 'ann-admin-actions';
                actions.innerHTML = '<span class="ann-drag-handle" title="Maintenir et glisser">&#x2630;</span>' +
                    '<button onclick="renameAnnale(\'' + slug + '\',' + idx + ')">&#x270E; Renommer</button>' +
                    '<button onclick="changeAnnaleType(\'' + slug + '\',' + idx + ')">&#x1F3F7; Statut</button>' +
                    '<button class="del" onclick="deleteAnnaleEnhanced(\'' + slug + '\',' + idx + ')">&#x2716; Supprimer</button>';
                var titleEl = item.querySelector('strong, .ann-name, h4, h3') || item.firstElementChild;
                if(titleEl) titleEl.appendChild(actions);
                else item.appendChild(actions);
            });
            initAnnalesDragDrop(slug);
        });
    }
    window.enhanceAnnalesUI = enhanceAnnalesUI;
    var _enhanceTimer = null;
    new MutationObserver(function(){ clearTimeout(_enhanceTimer); _enhanceTimer = setTimeout(enhanceAnnalesUI, 300); }).observe(document.body, {childList:true, subtree:true});
    setTimeout(enhanceAnnalesUI, 500); setTimeout(enhanceAnnalesUI, 1500); setTimeout(enhanceAnnalesUI, 3000);
    window.addEventListener('hashchange', function(){ setTimeout(enhanceAnnalesUI, 400); });
})();
"""
    return "<" + "script>" + js + "</" + "script>"


def build_filesync_patch():
    """Patch FDB pour synchroniser les fichiers avec le serveur PostgreSQL."""
    js = r"""
(function(){
    if(window._fileSyncPatched) return;
    window._fileSyncPatched = true;
    var _origPut = FDB.put.bind(FDB);
    var _origGet = FDB.get.bind(FDB);
    var _origDel = FDB.del.bind(FDB);

    FDB.put = async function(fid, obj){
        var result = await _origPut(fid, obj);
        try {
            if(obj && obj.data){
                var dataUrl = obj.data;
                var parts = dataUrl.split(",");
                var mime = parts[0].match(/:(.*?);/);
                mime = mime ? mime[1] : "application/octet-stream";
                var b64 = parts[1];
                var binary = atob(b64);
                var bytes = new Uint8Array(binary.length);
                for(var i=0; i<binary.length; i++) bytes[i] = binary.charCodeAt(i);
                var blob = new Blob([bytes], {type: mime});
                var fd = new FormData();
                fd.append("fid", fid);
                fd.append("file", blob, obj.name || "fichier");
                fetch("/api/files/upload", {method:"POST", body: fd}).catch(function(e){});
            }
        } catch(e){ console.warn("[FileSync] Upload error:", e); }
        return result;
    };

    FDB.get = async function(fid){
        var local = await _origGet(fid);
        if(local) return local;
        try {
            var resp = await fetch("/api/files/" + encodeURIComponent(fid));
            if(!resp.ok) return null;
            var blob = await resp.blob();
            var filename = "fichier";
            var disp = resp.headers.get("content-disposition");
            if(disp){ var match = disp.match(/filename[^;=\n]*=(["'].*?["']|[^;\n]*)/); if(match) filename = match[1].replace(/["']/g, ""); }
            var dataUrl = await new Promise(function(resolve){ var reader = new FileReader(); reader.onload = function(){ resolve(reader.result); }; reader.readAsDataURL(blob); });
            var fileObj = {name:filename, type:blob.type, size:blob.size, data:dataUrl};
            try { await _origPut(fid, fileObj); } catch(e){}
            return fileObj;
        } catch(e){ return null; }
    };

    FDB.del = async function(fid){
        var result = await _origDel(fid);
        try { fetch("/api/files/" + encodeURIComponent(fid), {method:"DELETE"}).catch(function(e){}); } catch(e){}
        return result;
    };

    // Bulk sync - push local IndexedDB files to server for all users
    async function bulkSyncToServer(){
        if(sessionStorage.getItem("_fdb_bulk_synced")) return;
        try {
            var db = await new Promise(function(res, rej){ var req = indexedDB.open("edhec_files", 1); req.onsuccess = function(e){ res(e.target.result); }; req.onerror = function(e){ rej(e); }; });
            var tx = db.transaction("files", "readonly");
            var store = tx.objectStore("files");
            var allKeys = await new Promise(function(res, rej){ var req = store.getAllKeys(); req.onsuccess = function(){ res(req.result); }; req.onerror = function(e){ rej(e); }; });
            var synced = 0;
            for(var k = 0; k < allKeys.length; k++){
                var fid = allKeys[k];
                try {
                    var metaResp = await fetch("/api/files/" + encodeURIComponent(fid) + "/meta");
                    if(metaResp.ok) continue;
                    var obj = await new Promise(function(res, rej){ var tx2 = db.transaction("files", "readonly"); var r = tx2.objectStore("files").get(fid); r.onsuccess = function(){ res(r.result); }; r.onerror = function(e){ rej(e); }; });
                    if(!obj || !obj.data) continue;
                    var dataUrl = obj.data; var parts = dataUrl.split(",");
                    var mime = "application/octet-stream"; var mimeMatch = parts[0].match(/:(.*?);/); if(mimeMatch) mime = mimeMatch[1];
                    var b64 = parts[1]; var binary = atob(b64);
                    var bytes = new Uint8Array(binary.length);
                    for(var i=0; i<binary.length; i++) bytes[i] = binary.charCodeAt(i);
                    var blob = new Blob([bytes], {type: mime});
                    var fd = new FormData(); fd.append("fid", fid); fd.append("file", blob, obj.name || "fichier");
                    await fetch("/api/files/upload", {method:"POST", body: fd});
                    synced++;
                } catch(e){}
            }
            sessionStorage.setItem("_fdb_bulk_synced", "1");
            if(synced > 0 && typeof toast === "function") toast(synced + " fichier(s) synchronise(s)");
        } catch(e){}
    }
    setTimeout(bulkSyncToServer, 3000);
    window.addEventListener("hashchange", function(){ setTimeout(bulkSyncToServer, 2000); });
})();
"""
    return "<" + "script>" + js + "</" + "script>"


def build_exam_peruser_patch():
    """Patch pour rendre les dates d examens propres a chaque compte utilisateur."""
    js = ""
    js += "(function(){\n"
    js += "    if(window._examPerUserPatched) return;\n"
    js += "    window._examPerUserPatched = true;\n"
    js += "    function getUserEmail(){\n"
    js += "        try { var u = Auth.current(); return u && u.email ? u.email.trim().toLowerCase() : null; } catch(e){ return null; }\n"
    js += "    }\n"
    js += "    function getExamKey(){\n"
    js += "        var email = getUserEmail();\n"
    js += "        if(!email) return 'exams';\n"
    js += "        return 'exams_' + email.replace(/[^a-z0-9]/g, '_');\n"
    js += "    }\n"
    js += "    var _origDBget = DB.get.bind(DB);\n"
    js += "    var _origDBset = DB.set.bind(DB);\n"
    js += "    DB.get = function(key){\n"
    js += "        if(key === 'exams'){\n"
    js += "            var perUserKey = getExamKey();\n"
    js += "            var val = _origDBget(perUserKey);\n"
    js += "            if(val !== null && val !== undefined) return val;\n"
    js += "            return [];\n"
    js += "        }\n"
    js += "        return _origDBget(key);\n"
    js += "    };\n"
    js += "    DB.set = function(key, val){\n"
    js += "        if(key === 'exams'){\n"
    js += "            var perUserKey = getExamKey();\n"
    js += "            _origDBset(perUserKey, val);\n"
    js += "            var email = getUserEmail();\n"
    js += "            if(email){\n"
    js += "                fetch('/api/user/exams', {\n"
    js += "                    method: 'POST',\n"
    js += "                    headers: {'Content-Type': 'application/json'},\n"
    js += "                    body: JSON.stringify({email: email, exams: val})\n"
    js += "                }).catch(function(e){ console.warn('[ExamSync] Push failed:', e); });\n"
    js += "            }\n"
    js += "            return;\n"
    js += "        }\n"
    js += "        return _origDBset(key, val);\n"
    js += "    };\n"
    js += "    function pullUserExams(){\n"
    js += "        var email = getUserEmail();\n"
    js += "        if(!email) return;\n"
    js += "        fetch('/api/user/exams?email=' + encodeURIComponent(email))\n"
    js += "        .then(function(r){ return r.json(); })\n"
    js += "        .then(function(data){\n"
    js += "            if(data.exams && data.exams.length > 0){\n"
    js += "                var perUserKey = getExamKey();\n"
    js += "                var local = _origDBget(perUserKey);\n"
    js += "                    _origDBset(perUserKey, data.exams);\n"
    js += "                    if(typeof renderDashboard === 'function') try { renderDashboard(); } catch(e){}\n"
    js += "            }\n"
    js += "        }).catch(function(e){});\n"
    js += "    }\n"
    js += "    function migrateSharedExams(){\n"
    js += "        var email = getUserEmail();\n"
    js += "        if(!email) return;\n"
    js += "        var perUserKey = getExamKey();\n"
    js += "        var perUser = _origDBget(perUserKey);\n"
    js += "        if(perUser && perUser.length > 0) return;\n"
    js += "        var shared = _origDBget('exams');\n"
    js += "        if(shared && shared.length > 0){\n"
    js += "            _origDBset(perUserKey, shared);\n"
    js += "            fetch('/api/user/exams', {\n"
    js += "                method: 'POST',\n"
    js += "                headers: {'Content-Type': 'application/json'},\n"
    js += "                body: JSON.stringify({email: email, exams: shared})\n"
    js += "            }).catch(function(e){});\n"
    js += "        }\n"
    js += "    }\n"
    js += "    function cleanSharedExams(){\n"
    js += "        try { localStorage.removeItem('edhec_exams'); } catch(e){}\n"
    js += "    }\n"
    js += "    setTimeout(function(){ migrateSharedExams(); pullUserExams(); cleanSharedExams(); }, 2000);\n"
    js += "    window.addEventListener('hashchange', function(){ setTimeout(pullUserExams, 500); });\n"
    js += "    console.log('[ExamSync] Per-user exam dates enabled');\n"
    js += "})();\n"
    return "<" + "script>" + js + "</" + "script>"


def build_device_patch():
    """Patch pour permettre plusieurs appareils PC par utilisateur."""
    js = ""
    js += "(function(){\n"
    js += "    if(window._deviceMultiPcPatched) return;\n"
    js += "    window._deviceMultiPcPatched = true;\n"
    js += "    if(typeof DeviceTracker !== 'undefined' && DeviceTracker.approveDevice){\n"
    js += "        var _origApprove = DeviceTracker.approveDevice.bind(DeviceTracker);\n"
    js += "        DeviceTracker.approveDevice = function(userId, deviceId){\n"
    js += "            var devices = this.getUserDevices(userId);\n"
    js += "            var dev = devices.find(function(d){ return d.id === deviceId; });\n"
    js += "            if(!dev) return;\n"
    js += "            if(dev.type === 'pc'){\n"
    js += "                dev.approved = true;\n"
    js += "                dev.pending = false;\n"
    js += "                this.setUserDevices(userId, devices);\n"
    js += "                return;\n"
    js += "            }\n"
    js += "            _origApprove(userId, deviceId);\n"
    js += "        };\n"
    js += "        console.log('[DevicePatch] Multi-PC approval enabled');\n"
    js += "    }\n"
    js += "})();\n"
    return "<" + "script>" + js + "</" + "script>"


def build_announcements_patch():
    """Patch pour la section infos/changelog sur le Dashboard avec editeur riche."""
    js = ""
    js += "(function(){\n"
    js += "    if(window._announcementsPatched) return;\n"
    js += "    window._announcementsPatched = true;\n"
    js += "    var style = document.createElement('style');\n"
    js += "    style.textContent = '"
    js += "#site-announcements { margin-top:1.5rem; } "
    js += ".ann-section-title { color:#e6edf3; font-size:1.1rem; margin-bottom:1rem; display:flex; align-items:center; justify-content:space-between; } "
    js += ".ann-card { background:rgba(255,255,255,.04); border:1px solid rgba(255,255,255,.08); border-radius:12px; padding:1.2rem; margin-bottom:.8rem; transition:all .2s; } "
    js += ".ann-card:hover { background:rgba(255,255,255,.06); border-color:rgba(255,255,255,.12); } "
    js += ".ann-card-title { color:#e6edf3; font-size:.95rem; font-weight:600; margin-bottom:.5rem; } "
    js += ".ann-card-date { color:rgba(255,255,255,.3); font-size:.7rem; margin-bottom:.6rem; } "
    js += ".ann-card-content { color:rgba(255,255,255,.65); font-size:.85rem; line-height:1.6; } "
    js += ".ann-card-content p { margin-bottom:.4rem; } "
    js += ".ann-card-content strong { color:#e6edf3; } "
    js += ".ann-card-actions { display:flex; gap:.5rem; margin-top:.8rem; } "
    js += ".ann-card-actions button { background:rgba(255,255,255,.06); border:1px solid rgba(255,255,255,.12); color:rgba(255,255,255,.6); border-radius:6px; padding:4px 12px; font-size:.75rem; cursor:pointer; transition:all .2s; } "
    js += ".ann-card-actions button:hover { background:rgba(255,255,255,.1); color:#fff; } "
    js += ".ann-card-actions button.del:hover { background:rgba(224,90,90,.2); color:#E07A5F; } "
    js += ".ann-btn-add { background:linear-gradient(135deg,rgba(212,168,83,.2),rgba(212,168,83,.1)); border:1px solid rgba(212,168,83,.3); color:var(--gold); border-radius:8px; padding:6px 16px; font-size:.8rem; cursor:pointer; transition:all .2s; } "
    js += ".ann-btn-add:hover { background:linear-gradient(135deg,rgba(212,168,83,.3),rgba(212,168,83,.2)); } "
    js += ".ann-editor-overlay { position:fixed; top:0; left:0; width:100%25; height:100%25; background:rgba(0,0,0,.7); z-index:10000; display:flex; align-items:center; justify-content:center; backdrop-filter:blur(4px); } "
    js += ".ann-editor-modal { background:#1a1f2e; border:1px solid rgba(255,255,255,.15); border-radius:16px; width:90%25; max-width:600px; max-height:85vh; overflow:auto; padding:1.5rem; } "
    js += ".ann-editor-modal h3 { color:#e6edf3; font-size:1.1rem; margin-bottom:1rem; } "
    js += ".ann-editor-modal input[type=text] { width:100%25; background:rgba(255,255,255,.06); border:1px solid rgba(255,255,255,.15); border-radius:8px; padding:.6rem .8rem; color:#e6edf3; font-size:.9rem; margin-bottom:.8rem; outline:none; box-sizing:border-box; } "
    js += ".ann-editor-modal input[type=text]:focus { border-color:var(--gold); } "
    js += ".ann-toolbar { display:flex; flex-wrap:wrap; gap:4px; margin-bottom:0; padding:.4rem; background:rgba(255,255,255,.04); border-radius:8px 8px 0 0; border:1px solid rgba(255,255,255,.1); border-bottom:none; } "
    js += ".ann-toolbar button { background:rgba(255,255,255,.08); border:1px solid rgba(255,255,255,.1); color:rgba(255,255,255,.7); border-radius:4px; padding:4px 8px; font-size:.8rem; cursor:pointer; min-width:28px; } "
    js += ".ann-toolbar button:hover { background:rgba(255,255,255,.15); color:#fff; } "
    js += ".ann-toolbar select { background:rgba(255,255,255,.08); border:1px solid rgba(255,255,255,.1); color:rgba(255,255,255,.7); border-radius:4px; padding:3px 6px; font-size:.75rem; } "
    js += ".ann-editor-content { min-height:200px; max-height:350px; overflow-y:auto; background:rgba(255,255,255,.04); border:1px solid rgba(255,255,255,.1); border-radius:0 0 8px 8px; padding:.8rem; color:rgba(255,255,255,.8); font-size:.85rem; line-height:1.6; outline:none; } "
    js += ".ann-editor-content:focus { border-color:rgba(212,168,83,.4); } "
    js += ".ann-editor-btns { display:flex; gap:.6rem; margin-top:1rem; justify-content:flex-end; } "
    js += ".ann-editor-btns button { border-radius:8px; padding:8px 20px; font-size:.85rem; cursor:pointer; border:none; } "
    js += ".ann-editor-btns .ann-save { background:linear-gradient(135deg,rgba(212,168,83,.8),rgba(212,168,83,.6)); color:#1a1f2e; font-weight:600; } "
    js += ".ann-editor-btns .ann-save:hover { background:linear-gradient(135deg,rgba(212,168,83,1),rgba(212,168,83,.8)); } "
    js += ".ann-editor-btns .ann-cancel { background:rgba(255,255,255,.08); color:rgba(255,255,255,.6); border:1px solid rgba(255,255,255,.12); } "
    js += ".ann-editor-btns .ann-cancel:hover { background:rgba(255,255,255,.12); color:#fff; } "
    js += "';\n"
    js += "    document.head.appendChild(style);\n"
    js += "    var announcementsCache = [];\n"
    js += "    function loadAnnouncements(cb){\n"
    js += "        fetch('/api/announcements').then(function(r){ return r.json(); }).then(function(data){ announcementsCache = data || []; if(cb) cb(); }).catch(function(e){ if(cb) cb(); });\n"
    js += "    }\n"
    js += "    function openEditor(existing){\n"
    js += "        var ov = document.createElement('div'); ov.className = 'ann-editor-overlay';\n"
    js += "        ov.onclick = function(e){ if(e.target === ov) ov.remove(); };\n"
    js += "        var md = document.createElement('div'); md.className = 'ann-editor-modal';\n"
    js += "        var titleVal = existing ? existing.title.replace(/\"/g,'&quot;') : '';\n"
    js += "        var contentVal = existing ? existing.content : '';\n"
    js += "        var h = '<h3>' + (existing ? '\\u270E Modifier' : '\\u2795 Nouvelle annonce') + '</h3>';\n"
    js += "        h += '<input type=\"text\" id=\"ann-title-input\" placeholder=\"Titre\" value=\"' + titleVal + '\">';\n"
    js += "        h += '<div class=\"ann-toolbar\">';\n"
    js += "        h += '<button onclick=\"document.execCommand(\\'bold\\')\" title=\"Gras\"><b>G</b></button>';\n"
    js += "        h += '<button onclick=\"document.execCommand(\\'italic\\')\" title=\"Italique\"><i>I</i></button>';\n"
    js += "        h += '<button onclick=\"document.execCommand(\\'underline\\')\" title=\"Souligne\"><u>S</u></button>';\n"
    js += "        h += '<button onclick=\"document.execCommand(\\'strikeThrough\\')\" title=\"Barre\"><s>B</s></button>';\n"
    js += "        h += '<select onchange=\"document.execCommand(\\'foreColor\\',false,this.value);this.value=\\'\\'\">';\n"
    js += "        h += '<option value=\"\">Couleur</option>';\n"
    js += "        h += '<option value=\"#e6edf3\">\\u25CF Blanc</option>';\n"
    js += "        h += '<option value=\"#E07A5F\">\\u25CF Rouge</option>';\n"
    js += "        h += '<option value=\"#D4A853\">\\u25CF Or</option>';\n"
    js += "        h += '<option value=\"#2A908F\">\\u25CF Vert</option>';\n"
    js += "        h += '<option value=\"#5FA8D3\">\\u25CF Bleu</option>';\n"
    js += "        h += '<option value=\"#C084FC\">\\u25CF Violet</option>';\n"
    js += "        h += '</select>';\n"
    js += "        h += '<select onchange=\"document.execCommand(\\'fontSize\\',false,this.value);this.value=\\'\\'\">';\n"
    js += "        h += '<option value=\"\">Taille</option>';\n"
    js += "        h += '<option value=\"2\">Petit</option><option value=\"3\">Normal</option><option value=\"4\">Grand</option><option value=\"5\">Tres grand</option>';\n"
    js += "        h += '</select>';\n"
    js += "        h += '<button onclick=\"document.execCommand(\\'insertUnorderedList\\')\">&bull; Liste</button>';\n"
    js += "        h += '<button onclick=\"document.execCommand(\\'insertOrderedList\\')\">1. Liste</button>';\n"
    js += "        h += '</div>';\n"
    js += "        h += '<div class=\"ann-editor-content\" contenteditable=\"true\" id=\"ann-content-editor\">' + contentVal + '</div>';\n"
    js += "        h += '<div class=\"ann-editor-btns\">';\n"
    js += "        h += '<button class=\"ann-cancel\" id=\"ann-cancel-btn\">Annuler</button>';\n"
    js += "        h += '<button class=\"ann-save\" id=\"ann-save-btn\">' + (existing ? 'Modifier' : 'Publier') + '</button>';\n"
    js += "        h += '</div>';\n"
    js += "        md.innerHTML = h;\n"
    js += "        ov.appendChild(md); document.body.appendChild(ov);\n"
    js += "        document.getElementById('ann-cancel-btn').onclick = function(){ ov.remove(); };\n"
    js += "        document.getElementById('ann-save-btn').onclick = function(){\n"
    js += "            var t = document.getElementById('ann-title-input').value.trim();\n"
    js += "            var c = document.getElementById('ann-content-editor').innerHTML.trim();\n"
    js += "            if(!t || !c){ if(typeof toast==='function') toast('Remplissez titre et contenu','err'); return; }\n"
    js += "            var author = ''; try { author = Auth.current().name || ''; } catch(e){}\n"
    js += "            if(existing){\n"
    js += "                fetch('/api/announcements/' + existing.id, { method:'PUT', headers:{'Content-Type':'application/json'}, body:JSON.stringify({title:t,content:c}) })\n"
    js += "                .then(function(r){ return r.json(); }).then(function(){ ov.remove(); if(typeof toast==='function') toast('Annonce modifiee','ok'); loadAnnouncements(injectSection); });\n"
    js += "            } else {\n"
    js += "                fetch('/api/announcements', { method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({title:t,content:c,author:author}) })\n"
    js += "                .then(function(r){ return r.json(); }).then(function(){ ov.remove(); if(typeof toast==='function') toast('Annonce publiee','ok'); loadAnnouncements(injectSection); });\n"
    js += "            }\n"
    js += "        };\n"
    js += "    }\n"
    js += "    window.openAnnouncementEditor = function(id){\n"
    js += "        if(id){ var a = announcementsCache.find(function(x){ return x.id === id; }); if(a) openEditor(a); }\n"
    js += "        else openEditor(null);\n"
    js += "    };\n"
    js += "    window.deleteAnnouncement = function(id){\n"
    js += "        if(!confirm('Supprimer cette annonce ?')) return;\n"
    js += "        fetch('/api/announcements/' + id, {method:'DELETE'}).then(function(r){ return r.json(); })\n"
    js += "        .then(function(){ if(typeof toast==='function') toast('Annonce supprimee','ok'); loadAnnouncements(injectSection); });\n"
    js += "    };\n"
    js += "    function injectSection(){\n"
    js += "        var ph = document.querySelector('.ph.ani');\n"
    js += "        if(!ph) return;\n"
    js += "        var dc = ph.parentElement; if(!dc) return;\n"
    js += "        var old = document.getElementById('site-announcements'); if(old) old.remove();\n"
    js += "        var isAdmin = typeof Auth !== 'undefined' && Auth.admin();\n"
    js += "        if(announcementsCache.length === 0 && !isAdmin) return;\n"
    js += "        var h = '<div id=\"site-announcements\">';\n"
    js += "        h += '<div class=\"ann-section-title\"><span>\\uD83D\\uDCE2 Infos & Mises \\u00e0 jour</span>';\n"
    js += "        if(isAdmin) h += '<button class=\"ann-btn-add\" onclick=\"openAnnouncementEditor()\">\\u2795 Ajouter</button>';\n"
    js += "        h += '</div>';\n"
    js += "        for(var i=0; i<announcementsCache.length; i++){\n"
    js += "            var a = announcementsCache[i];\n"
    js += "            var ds = ''; try { var dd = new Date(a.created_at); ds = dd.toLocaleDateString('fr-FR',{day:'numeric',month:'long',year:'numeric'}); if(a.author) ds += ' \\u2022 ' + a.author; } catch(e){}\n"
    js += "            h += '<div class=\"ann-card\">';\n"
    js += "            h += '<div class=\"ann-card-title\">' + a.title + '</div>';\n"
    js += "            h += '<div class=\"ann-card-date\">' + ds + '</div>';\n"
    js += "            h += '<div class=\"ann-card-content\">' + a.content + '</div>';\n"
    js += "            if(isAdmin){\n"
    js += "                h += '<div class=\"ann-card-actions\">';\n"
    js += "                h += '<button onclick=\"openAnnouncementEditor(' + a.id + ')\">\\u270E Modifier</button>';\n"
    js += "                h += '<button class=\"del\" onclick=\"deleteAnnouncement(' + a.id + ')\">\\u2716 Supprimer</button>';\n"
    js += "                h += '</div>';\n"
    js += "            }\n"
    js += "            h += '</div>';\n"
    js += "        }\n"
    js += "        h += '</div>';\n"
    js += "        var tmp = document.createElement('div'); tmp.innerHTML = h;\n"
    js += "        dc.appendChild(tmp.firstElementChild);\n"
    js += "    }\n"
    js += "    function checkAndInject(){\n"
    js += "        var hash = location.hash || '';\n"
    js += "        if(hash === '' || hash === '#' || hash === '#/' || hash.indexOf('dashboard') !== -1 || hash.indexOf('home') !== -1){\n"
    js += "            loadAnnouncements(injectSection);\n"
    js += "        }\n"
    js += "    }\n"
    js += "    var _annTimer = null;\n"
    js += "    new MutationObserver(function(){ clearTimeout(_annTimer); _annTimer = setTimeout(checkAndInject, 500); }).observe(document.body, {childList:true, subtree:true});\n"
    js += "    setTimeout(checkAndInject, 2500);\n"
    js += "    window.addEventListener('hashchange', function(){ setTimeout(checkAndInject, 500); });\n"
    js += "})();\n"
    return "<" + "script>" + js + "</" + "script>"


def build_infos_richtext_patch():
    return """<script>
(function(){
  // Override getInfosTabHTML to add rich text toolbar + contenteditable
  var _origGetInfosTabHTML = window.getInfosTabHTML;
  window.getInfosTabHTML = function(slug){
    var html = _origGetInfosTabHTML(slug);
    // Replace textarea with toolbar + contenteditable div
    html = html.replace(/<textarea[^>]*id="infos-edit-[^"]*"[^>]*>[^<]*<\/textarea>/,
      '<div style="margin-bottom:.5rem;display:flex;flex-wrap:wrap;gap:4px">' +
      '<button type="button" class="btn btn-g" style="padding:2px 8px;font-size:.75rem;min-width:auto" onclick="document.execCommand(\\'bold\\')"><b>G</b></button>' +
      '<button type="button" class="btn btn-g" style="padding:2px 8px;font-size:.75rem;min-width:auto" onclick="document.execCommand(\\'italic\\')"><i>I</i></button>' +
      '<button type="button" class="btn btn-g" style="padding:2px 8px;font-size:.75rem;min-width:auto" onclick="document.execCommand(\\'underline\\')"><u>S</u></button>' +
      '<button type="button" class="btn btn-g" style="padding:2px 8px;font-size:.75rem;min-width:auto" onclick="document.execCommand(\\'strikeThrough\\')"><s>B</s></button>' +
      '<select onchange="document.execCommand(\\'foreColor\\',false,this.value);this.value=\\'\\'" style="padding:2px 4px;font-size:.72rem;background:rgba(255,255,255,.08);color:#e6edf3;border:1px solid rgba(255,255,255,.15);border-radius:6px;cursor:pointer"><option value="">Couleur</option><option value="#ffffff" style="color:#fff">Blanc</option><option value="#ff4444" style="color:#ff4444">Rouge</option><option value="#FFD700" style="color:#FFD700">Or</option><option value="#44ff44" style="color:#44ff44">Vert</option><option value="#4488ff" style="color:#4488ff">Bleu</option><option value="#aa66ff" style="color:#aa66ff">Violet</option></select>' +
      '<select onchange="document.execCommand(\\'hiliteColor\\',false,this.value);this.value=\\'\\'" style="padding:2px 4px;font-size:.72rem;background:rgba(255,255,255,.08);color:#e6edf3;border:1px solid rgba(255,255,255,.15);border-radius:6px;cursor:pointer"><option value="">Surligneur</option><option value="#FFD700">Jaune</option><option value="#44ff44">Vert</option><option value="#ff4444">Rouge</option><option value="#4488ff">Bleu</option><option value="#aa66ff">Violet</option></select>' +
      '<select onchange="document.execCommand(\\'fontSize\\',false,this.value);this.value=\\'\\'" style="padding:2px 4px;font-size:.72rem;background:rgba(255,255,255,.08);color:#e6edf3;border:1px solid rgba(255,255,255,.15);border-radius:6px;cursor:pointer"><option value="">Taille</option><option value="1">Petit</option><option value="3">Normal</option><option value="5">Grand</option><option value="7">Tr\u00e8s grand</option></select>' +
      '</div>' +
      '<div contenteditable="true" id="infos-edit-'+slug+'" style="width:100%;min-height:120px;padding:.75rem;background:rgba(255,255,255,.06);border:1px solid rgba(255,255,255,.1);border-radius:8px;color:#e6edf3;font-family:inherit;font-size:.85rem;outline:none;line-height:1.6;box-sizing:border-box;overflow-y:auto"></div>'
    );
    return html;
  };
  // Override saveInfos to use innerHTML instead of value
  var _origSaveInfos = window.saveInfos;
  window.saveInfos = function(slug){
    if(!Auth.admin()) return;
    var el = document.getElementById('infos-edit-'+slug);
    if(!el || !el.innerHTML.trim()) return;
    var msgs = _migrateInfos(slug);
    msgs.push({id:Date.now(), text:el.innerHTML.trim(), date:Date.now()});
    DB.set('infos_'+slug, msgs);
    DB.set('infos_ts_'+slug, Date.now());
    el.innerHTML = '';
    toast('Information publi\u00e9e','ok');
    refreshInfosTab(slug);
  };
})();
</script>"""

def build_patch_script():
    """Construit le JavaScript ServerSync a injecter."""
    js = r"""
(function(){
    var PREFIX = 'edhec_';
    var _pulling = false;
    window.ServerSync = {
        enabled: true,
        init: function(){
            _pulling = true;
            return fetch('/api/sync/pull')
            .then(function(r){ return r.json(); })
            .then(function(data){
                var keys = Object.keys(data);
                for(var i=0; i<keys.length; i++){
                    localStorage.setItem(keys[i], data[keys[i]]);
                }
                _pulling = false;
                if(keys.length > 0) console.log('[ServerSync] Pulled ' + keys.length + ' keys');
                ServerSync.enabled = true;
                return keys.length;
            })
            .catch(function(e){
                _pulling = false;
                console.warn('[ServerSync] Pull failed', e);
                ServerSync.enabled = false;
                return 0;
            });
        },
        pushRaw: function(fullKey, rawValue){
            if(!ServerSync.enabled || _pulling) return Promise.resolve();
            var payload = {};
            payload[fullKey] = rawValue;
            return fetch('/api/sync/push', {
                method: 'POST',
                headers: {'Content-Type':'application/json'},
                body: JSON.stringify(payload)
            }).catch(function(e){});
        },
        push: function(k, v){
            if(!ServerSync.enabled || _pulling) return Promise.resolve();
            var payload = {};
            payload[PREFIX + k] = JSON.stringify(v);
            return fetch('/api/sync/push', {
                method: 'POST',
                headers: {'Content-Type':'application/json'},
                body: JSON.stringify(payload)
            }).catch(function(e){});
        },
        pullAll: function(){ return this.init(); },
        pushFile: function(){ return Promise.resolve(); },
        pushAll: function(){
            var payload = {}; var count = 0;
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
            .then(function(d){ console.log('[ServerSync] Pushed ' + count + ' keys'); })
            .catch(function(e){});
        }
    };
    var _origSetItem = Storage.prototype.setItem;
    Storage.prototype.setItem = function(key, value){
        _origSetItem.call(this, key, value);
        if(this === localStorage && key.startsWith(PREFIX) && !_pulling && ServerSync.enabled){
            ServerSync.pushRaw(key, value);
        }
    };
    ServerSync.init().then(function(pulledCount){
        if(pulledCount > 0 && !sessionStorage.getItem('_ss_synced')){
            sessionStorage.setItem('_ss_synced', '1');
            location.reload();
            return;
        }
        if(pulledCount === 0){
            console.log('[ServerSync] Server empty, pushing local data...');
            ServerSync.pushAll();
        }
    });
})();
"""
    return "<" + "script>" + js + "</" + "script>"



def build_nav_fix_patch():
    js = """
(function(){
    // Fix 1: Override navigate to fix double-slash AND call route()
    window.navigate = function(p) {
        while(p.indexOf('//')===0) p = p.substring(1);
        location.hash = p;
        if(typeof route === 'function') route();
    };

    // Fix 2: Ensure hashchange always calls route()
    window.addEventListener('hashchange', function(){
        if(typeof route === 'function') route();
    });
    if(typeof ServerSync !== 'undefined' && ServerSync && ServerSync.init){
        var _origInit = ServerSync.init;
        ServerSync.init = function(){
            try { return _origInit.apply(this, arguments); }
            catch(e){ console.warn('[ServerSync] init error:', e); return Promise.resolve(0); }
        };
    }
    console.log('[NavFix] Navigation, hashchange, and ServerSync patches applied');
})();
"""
    return "<" + "script>" + js + "</" + "script>"

@app.route("/")
def index():
    html_path = os.path.join(app.static_folder, "index.html")
    with open(html_path, "r", encoding="utf-8") as f:
        html = f.read()

    # Renommer le site dans le HTML statique
    html = html.replace("R\u00e9ussir l'EDHEC", "R\u00e9ussir \u00c9tudes")
    html = html.replace("R\u00e9ussir l\\'EDHEC", "R\u00e9ussir \u00c9tudes")

    # Construire tous les patches
    filesync     = build_filesync_patch()
    exam_patch   = build_exam_peruser_patch()
    device_patch = build_device_patch()
    ann_patch    = build_announcements_patch()
    patch        = build_patch_script()
    branding     = build_branding_patch()
    annales      = build_annales_patch()
    infos_rt     = build_infos_richtext_patch()
    nav_fix      = build_nav_fix_patch()

    # Injecter dans l ordre : filesync, exam, device, announcements, serversync, branding, annales
    html = html.replace(
        "</body>",
        filesync + "\n" +
        exam_patch + "\n" +
        device_patch + "\n" +
        ann_patch + "\n" +
        patch + "\n" +
        branding + "\n" +
        annales + "\n" +
                infos_rt + "\n" +
                    nav_fix + "\n</body>",
        1
    )
    return Response(html, mimetype="text/html")

# ---------------------------------------------------------------------------
# Init & Run
# ---------------------------------------------------------------------------
init_db()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=True)
