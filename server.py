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
    cur = conn.cursor()

    # Key-value store : miroir du localStorage du frontend
    cur.execute("""
        CREATE TABLE IF NOT EXISTS kv_store (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            updated_at TIMESTAMP DEFAULT NOW()
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

    conn.commit()

    # Table fichiers generaux (PDF, Excel, images - stockes en binaire)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS general_files (
            fid TEXT PRIMARY KEY,
            filename TEXT NOT NULL,
            mimetype TEXT NOT NULL,
            data BYTEA NOT NULL,
            size INTEGER DEFAULT 0,
            uploaded_at TIMESTAMP DEFAULT NOW()
        )
    """)
    conn.commit()
    cur.close()
    conn.close()


# ---------------------------------------------------------------------------
# Sync API  (remplace le localStorage par PostgreSQL)
# ---------------------------------------------------------------------------

@app.route("/api/sync/pull", methods=["GET"])
def sync_pull():
    """Renvoie TOUTES les cles/valeurs stockees en base."""
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
    """Recoit une ou plusieurs cles a sauvegarder."""
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
    """Supprime une cle."""
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


# ---------------------------------------------------------------------------
# PDF management (stocke en base PostgreSQL)
# ---------------------------------------------------------------------------

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
    cur = conn.cursor()
    try:
        cur.execute(
            """INSERT INTO general_files (fid, filename, mimetype, data, size)
               VALUES (%s, %s, %s, %s, %s)
               ON CONFLICT (fid) DO UPDATE
               SET filename = EXCLUDED.filename,
                   mimetype = EXCLUDED.mimetype,
                   data = EXCLUDED.data,
                   size = EXCLUDED.size,
                   uploaded_at = NOW()""",
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
    cur = conn.cursor()
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
    cur = conn.cursor()
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
    cur = conn.cursor()
    cur.execute("DELETE FROM general_files WHERE fid = %s", (fid,))
    deleted = cur.rowcount
    conn.commit()
    cur.close()
    conn.close()
    if deleted:
        return jsonify({"ok": True})
    return jsonify({"error": "Fichier non trouve"}), 404


# ---------------------------------------------------------------------------
# Serve index.html with ServerSync patch injected
# ---------------------------------------------------------------------------

# Le PATCH_SCRIPT est genere dynamiquement pour eviter les problemes
# avec les tags <script> dans les triple-quotes Python.

def build_branding_patch():
    """Renomme le site de Reussir l EDHEC a Reussir Etudes dans le DOM."""
    js = r"""
(function(){
    // --- Renommage du site ---
    function renameSite(){
        // Titre de la page
        document.title = document.title.replace(/RÃÂ©ussir l'EDHEC/g, 'RÃÂ©ussir ÃÂtudes');
        // Tous les elements texte visibles
        var walk = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT, null, false);
        while(walk.nextNode()){
            var n = walk.currentNode;
            if(n.nodeValue.indexOf("RÃÂ©ussir l'EDHEC") !== -1){
                n.nodeValue = n.nodeValue.replace(/RÃÂ©ussir l'EDHEC/g, 'RÃÂ©ussir ÃÂtudes');
            }
            if(n.nodeValue.indexOf("RÃÂ©ussir l\\'EDHEC") !== -1){
                n.nodeValue = n.nodeValue.replace(/RÃÂ©ussir l\\'EDHEC/g, 'RÃÂ©ussir ÃÂtudes');
            }
        }
        // Aussi dans les placeholders et titles
        document.querySelectorAll('[placeholder],[title]').forEach(function(el){
            if(el.placeholder && el.placeholder.indexOf('EDHEC')!==-1)
                el.placeholder = el.placeholder.replace(/EDHEC/g,'ÃÂtudes');
            if(el.title && el.title.indexOf('EDHEC')!==-1)
                el.title = el.title.replace(/EDHEC/g,'ÃÂtudes');
        });
    }
    // Executer au chargement et apres chaque navigation SPA
    if(document.readyState==='loading'){
        document.addEventListener('DOMContentLoaded', function(){ setTimeout(renameSite, 100); });
    } else {
        setTimeout(renameSite, 100);
    }
    // Observer les changements DOM pour re-renommer apres navigation SPA
    var _renameTimer = null;
    new MutationObserver(function(){
        clearTimeout(_renameTimer);
        _renameTimer = setTimeout(renameSite, 200);
    }).observe(document.body, {childList:true, subtree:true, characterData:true});
})();
"""
    return "<" + "script>" + js + "</" + "script>"


def build_annales_patch():
    """Ajoute les fonctionnalites admin pour les annales : renommer, supprimer, reordonner par drag and drop."""
    js = r"""
(function(){
    // --- CSS pour le drag & drop des annales ---
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

    // --- Stockage des fonctions originales ---
    var _origGetAnnalesTabHTML = window.getAnnalesTabHTML;
    var _origDeleteAnnale = window.deleteAnnale;
    var _origRefreshAnnalesTab = window.refreshAnnalesTab;

    // --- Helper : obtenir la cle annales pour un slug ---
    function getAnnalesKey(slug){
        // Les annales sont stockees sous differentes cles selon le slug
        // ex: edhec_annales_mafi, edhec_ct_microeco_annales
        var possibleKeys = [
            'annales_' + slug,
            'ct_' + slug + '_annales'
        ];
        for(var i=0; i<possibleKeys.length; i++){
            var val = DB.get(possibleKeys[i]);
            if(val !== null && val !== undefined) return possibleKeys[i];
        }
        // Chercher dans toutes les cles localStorage
        for(var j=0; j<localStorage.length; j++){
            var key = localStorage.key(j);
            if(key.indexOf('annale') !== -1 && key.indexOf(slug) !== -1){
                return key.replace('edhec_','');
            }
        }
        return 'annales_' + slug;
    }

    // --- Renommer une annale ---
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

    // --- Supprimer une annale (override ameliore) ---
    window.deleteAnnaleEnhanced = function(slug, idx){
        var key = getAnnalesKey(slug);
        var list = DB.get(key) || [];
        if(idx < 0 || idx >= list.length) return;
        var name = list[idx].name || 'Sans nom';
        if(!confirm('Supprimer "' + name + '" ?')) return;
        // Supprimer les fichiers associes dans FDB
        if(list[idx].sujetId && typeof FDB !== 'undefined'){
            try { FDB.del(list[idx].sujetId); } catch(e){}
        }
        if(list[idx].corrId && typeof FDB !== 'undefined'){
            try { FDB.del(list[idx].corrId); } catch(e){}
        }
        list.splice(idx, 1);
        DB.set(key, list);
        if(typeof refreshAnnalesTab === 'function') refreshAnnalesTab(slug);
        if(typeof toast === 'function') toast('Annale supprimee');
    };

    // --- Changer le type/statut d une annale (finals, midterms, autres...) ---
    window.changeAnnaleType = function(slug, idx){
        var key = getAnnalesKey(slug);
        var list = DB.get(key) || [];
        if(idx < 0 || idx >= list.length) return;
        var currentType = list[idx].type || 'finals';
        var types = ['finals', 'midterms', 'rattrapages', 'partiels', 'td', 'autres'];
        var msg = 'Choisir le statut pour cette annale :\\n';
        for(var t=0; t<types.length; t++) msg += (t+1) + '. ' + types[t] + (types[t]===currentType?' (actuel)':'') + '\\n';
        msg += '\\nEntrez le numero ou le nom du statut :';
        var choice = prompt(msg, currentType);
        if(choice === null) return;
        choice = choice.trim().toLowerCase();
        // Accept number input
        var num = parseInt(choice);
        if(num >= 1 && num <= types.length) choice = types[num-1];
        // Accept custom type too
        if(choice !== ''){
            list[idx].type = choice;
            DB.set(key, list);
            if(typeof refreshAnnalesTab === 'function') refreshAnnalesTab(slug);
            if(typeof toast === 'function') toast('Statut modifie : ' + choice);
        }
    };

    // --- Reordonner les annales (deplacer un element) ---
    window.moveAnnale = function(slug, fromIdx, toIdx){
        var key = getAnnalesKey(slug);
        var list = DB.get(key) || [];
        if(fromIdx < 0 || fromIdx >= list.length || toIdx < 0 || toIdx >= list.length) return;
        var item = list.splice(fromIdx, 1)[0];
        list.splice(toIdx, 0, item);
        DB.set(key, list);
        if(typeof refreshAnnalesTab === 'function') refreshAnnalesTab(slug);
    };

    // --- Initialiser le drag & drop sur les elements annales ---
    window.initAnnalesDragDrop = function(slug){
        var container = document.getElementById('annales-tab-' + slug);
        if(!container) return;
        var items = container.querySelectorAll('.ann-item');
        var dragSrcIdx = null;

        items.forEach(function(item, idx){
            // Touch events pour mobile (maintenir + glisser)
            var touchTimer = null;
            var isDragging = false;
            var touchStartY = 0;
            var dragClone = null;

            item.addEventListener('touchstart', function(e){
                touchStartY = e.touches[0].clientY;
                touchTimer = setTimeout(function(){
                    isDragging = true;
                    dragSrcIdx = idx;
                    item.classList.add('dragging');
                    // Vibration feedback si disponible
                    if(navigator.vibrate) navigator.vibrate(50);
                }, 400);
            }, {passive:true});

            item.addEventListener('touchmove', function(e){
                if(!isDragging){
                    // Si on bouge trop avant le timer, annuler
                    if(Math.abs(e.touches[0].clientY - touchStartY) > 10){
                        clearTimeout(touchTimer);
                    }
                    return;
                }
                e.preventDefault();
                var touchY = e.touches[0].clientY;
                // Trouver l element sous le doigt
                items.forEach(function(otherItem, otherIdx){
                    otherItem.classList.remove('drag-over-top', 'drag-over-bottom');
                    if(otherIdx === idx) return;
                    var rect = otherItem.getBoundingClientRect();
                    var midY = rect.top + rect.height / 2;
                    if(touchY > rect.top && touchY < rect.bottom){
                        if(touchY < midY){
                            otherItem.classList.add('drag-over-top');
                        } else {
                            otherItem.classList.add('drag-over-bottom');
                        }
                    }
                });
            }, {passive:false});

            item.addEventListener('touchend', function(e){
                clearTimeout(touchTimer);
                if(!isDragging){ return; }
                isDragging = false;
                item.classList.remove('dragging');
                // Trouver la cible
                var targetIdx = -1;
                items.forEach(function(otherItem, otherIdx){
                    if(otherItem.classList.contains('drag-over-top')){
                        targetIdx = otherIdx;
                    } else if(otherItem.classList.contains('drag-over-bottom')){
                        targetIdx = otherIdx;
                    }
                    otherItem.classList.remove('drag-over-top', 'drag-over-bottom');
                });
                if(targetIdx >= 0 && targetIdx !== dragSrcIdx){
                    moveAnnale(slug, dragSrcIdx, targetIdx);
                }
            });

            // Desktop drag & drop
            item.setAttribute('draggable', 'true');
            item.addEventListener('dragstart', function(e){
                dragSrcIdx = idx;
                item.classList.add('dragging');
                e.dataTransfer.effectAllowed = 'move';
                e.dataTransfer.setData('text/plain', idx);
            });
            item.addEventListener('dragend', function(){
                item.classList.remove('dragging');
                items.forEach(function(it){ it.classList.remove('drag-over-top','drag-over-bottom'); });
            });
            item.addEventListener('dragover', function(e){
                e.preventDefault();
                e.dataTransfer.dropEffect = 'move';
                var rect = item.getBoundingClientRect();
                var midY = rect.top + rect.height/2;
                items.forEach(function(it){ it.classList.remove('drag-over-top','drag-over-bottom'); });
                if(e.clientY < midY){
                    item.classList.add('drag-over-top');
                } else {
                    item.classList.add('drag-over-bottom');
                }
            });
            item.addEventListener('dragleave', function(){
                item.classList.remove('drag-over-top','drag-over-bottom');
            });
            item.addEventListener('drop', function(e){
                e.preventDefault();
                items.forEach(function(it){ it.classList.remove('drag-over-top','drag-over-bottom'); });
                var fromIdx = parseInt(e.dataTransfer.getData('text/plain'));
                var toIdx = idx;
                if(fromIdx !== toIdx){
                    moveAnnale(slug, fromIdx, toIdx);
                }
            });
        });
    };

    // --- Observer le DOM pour injecter les boutons admin dans les annales ---
    function enhanceAnnalesUI(){
        if(typeof Auth === 'undefined' || !Auth.admin()) return;
        // Trouver tous les conteneurs d annales
        document.querySelectorAll('[id^="annales-tab-"]').forEach(function(container){
            if(container.querySelectorAll('.ann-admin-actions').length > 0) return;
            var slug = container.id.replace('annales-tab-','');
            // Trouver les items d annales : ce sont des div.card.ani
            var items = container.querySelectorAll('.card.ani');
            if(items.length === 0){
                // Fallback: chercher les elements avec bouton download
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

                // Ajouter la classe ann-item si pas deja la
                item.classList.add('ann-item');

                // Creer les boutons admin
                var actions = document.createElement('span');
                actions.className = 'ann-admin-actions';
                actions.innerHTML = '<span class="ann-drag-handle" title="Maintenir et glisser pour reordonner">&#x2630;</span>' +
                    '<button onclick="renameAnnale(\'' + slug + '\',' + idx + ')" title="Renommer">&#x270E; Renommer</button>' +
                    '<button onclick="changeAnnaleType(\'' + slug + '\',' + idx + ')" title="Modifier le statut">&#x1F3F7; Statut</button>' +
                    '<button class="del" onclick="deleteAnnaleEnhanced(\'' + slug + '\',' + idx + ')" title="Supprimer">&#x2716; Supprimer</button>';

                // Inserer les actions dans le premier element de type titre/nom
                var titleEl = item.querySelector('strong, .ann-name, h4, h3') || item.firstElementChild;
                if(titleEl){
                    titleEl.appendChild(actions);
                } else {
                    item.appendChild(actions);
                }
            });

            // Activer le drag & drop
            initAnnalesDragDrop(slug);
            container.dataset.enhanced = '1';
        });
    }

    // Exposer globalement pour re-appel
    window.enhanceAnnalesUI = enhanceAnnalesUI;

    // Observer le DOM pour les changements (navigation SPA)
    var _enhanceTimer = null;
    new MutationObserver(function(){
        clearTimeout(_enhanceTimer);
        _enhanceTimer = setTimeout(enhanceAnnalesUI, 300);
    }).observe(document.body, {childList:true, subtree:true});
    // Au chargement initial avec retries progressifs
    setTimeout(enhanceAnnalesUI, 500);
    setTimeout(enhanceAnnalesUI, 1500);
    setTimeout(enhanceAnnalesUI, 3000);
    // Re-essayer aussi apres hashchange (navigation SPA)
    window.addEventListener('hashchange', function(){ setTimeout(enhanceAnnalesUI, 400); });

    console.log('[AnnalesPatch] Admin annales features loaded');
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

      // Override FDB.put : envoie aussi le fichier au serveur
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
            fetch("/api/files/upload", {method:"POST", body: fd})
              .then(function(r){ return r.json(); })
              .then(function(d){ console.log("[FileSync] Uploaded:", fid); })
              .catch(function(e){ console.warn("[FileSync] Upload failed:", fid, e); });
          }
        } catch(e){ console.warn("[FileSync] Upload error:", e); }
        return result;
      };

      // Override FDB.get : fallback serveur si pas en local
      FDB.get = async function(fid){
        var local = await _origGet(fid);
        if(local) return local;
        try {
          console.log("[FileSync] Trying server for:", fid);
          var resp = await fetch("/api/files/" + encodeURIComponent(fid));
          if(!resp.ok) return null;
          var blob = await resp.blob();
          var filename = "fichier";
          var disp = resp.headers.get("content-disposition");
          if(disp){
            var match = disp.match(/filename[^;=\n]*=(["'].*?["']|[^;\n]*)/);
            if(match) filename = match[1].replace(/["']/g, "");
          }
          var dataUrl = await new Promise(function(resolve){
            var reader = new FileReader();
            reader.onload = function(){ resolve(reader.result); };
            reader.readAsDataURL(blob);
          });
          var fileObj = {name:filename, type:blob.type, size:blob.size, data:dataUrl};
          try { await _origPut(fid, fileObj); } catch(e){}
          console.log("[FileSync] Retrieved from server:", fid, filename);
          return fileObj;
        } catch(e){
          console.warn("[FileSync] Server fetch failed:", fid, e);
          return null;
        }
      };

      // Override FDB.del : supprime aussi cote serveur
      FDB.del = async function(fid){
        var result = await _origDel(fid);
        try {
          fetch("/api/files/" + encodeURIComponent(fid), {method:"DELETE"}).catch(function(e){});
        } catch(e){}
        return result;
      };

      console.log("[FileSync] FDB patched for server sync");

      // --- Bulk sync : pousse tous les fichiers IndexedDB vers le serveur ---
      // Se lance automatiquement au chargement pour les admins
      async function bulkSyncToServer(){
        // Attendre que Auth soit disponible
        if(typeof Auth === "undefined" || !Auth.admin || !Auth.admin()) return;
        if(sessionStorage.getItem("_fdb_bulk_synced")) return;
        console.log("[FileSync] Admin detected, starting bulk sync of local files...");
        try {
          var db = await new Promise(function(res, rej){
            var req = indexedDB.open("edhec_files", 1);
            req.onsuccess = function(e){ res(e.target.result); };
            req.onerror = function(e){ rej(e); };
          });
          var tx = db.transaction("files", "readonly");
          var store = tx.objectStore("files");
          var allKeys = await new Promise(function(res, rej){
            var req = store.getAllKeys();
            req.onsuccess = function(){ res(req.result); };
            req.onerror = function(e){ rej(e); };
          });
          console.log("[FileSync] Found " + allKeys.length + " local files to check");
          var synced = 0;
          for(var k = 0; k < allKeys.length; k++){
            var fid = allKeys[k];
            try {
              // Verifier si le fichier existe deja sur le serveur
              var metaResp = await fetch("/api/files/" + encodeURIComponent(fid) + "/meta");
              if(metaResp.ok) continue; // deja sur le serveur
              // Recuperer le fichier depuis IndexedDB
              var obj = await new Promise(function(res, rej){
                var tx2 = db.transaction("files", "readonly");
                var r = tx2.objectStore("files").get(fid);
                r.onsuccess = function(){ res(r.result); };
                r.onerror = function(e){ rej(e); };
              });
              if(!obj || !obj.data) continue;
              // Upload vers le serveur
              var dataUrl = obj.data;
              var parts = dataUrl.split(",");
              var mime = "application/octet-stream";
              var mimeMatch = parts[0].match(/:(.*?);/);
              if(mimeMatch) mime = mimeMatch[1];
              var b64 = parts[1];
              var binary = atob(b64);
              var bytes = new Uint8Array(binary.length);
              for(var i=0; i<binary.length; i++) bytes[i] = binary.charCodeAt(i);
              var blob = new Blob([bytes], {type: mime});
              var fd = new FormData();
              fd.append("fid", fid);
              fd.append("file", blob, obj.name || "fichier");
              await fetch("/api/files/upload", {method:"POST", body: fd});
              synced++;
              console.log("[FileSync] Synced:", fid, obj.name || "");
            } catch(e){
              console.warn("[FileSync] Bulk sync error for:", fid, e);
            }
          }
          sessionStorage.setItem("_fdb_bulk_synced", "1");
          if(synced > 0){
            console.log("[FileSync] Bulk sync complete: " + synced + " files uploaded to server");
            if(typeof toast === "function") toast(synced + " fichier(s) synchronise(s) avec le serveur");
          } else {
            console.log("[FileSync] All files already on server");
          }
        } catch(e){
          console.warn("[FileSync] Bulk sync failed:", e);
        }
      }

      // Lancer le bulk sync apres un court delai (attendre que Auth soit pret)
      setTimeout(bulkSyncToServer, 3000);
      // Re-essayer apres navigation SPA
      window.addEventListener("hashchange", function(){ setTimeout(bulkSyncToServer, 2000); });

    })();
    """
    return "<" + "script>" + js + "</" + "script>"


def build_patch_script():
    """Construit le JavaScript ServerSync a injecter."""
    js = r"""
(function(){
    // --- ServerSync reel : synchronise localStorage <-> PostgreSQL ---
    var PREFIX = 'edhec_';
    var _pulling = false;   // flag pour ne pas re-push pendant un pull

    window.ServerSync = {
        enabled: true,

        // Au demarrage : charge tout depuis le serveur -> localStorage
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
                    if(keys.length > 0){
                        console.log('[ServerSync] Pulled ' + keys.length + ' keys from server');
                    }
                    ServerSync.enabled = true;
                    return keys.length;
                })
                .catch(function(e){
                    _pulling = false;
                    console.warn('[ServerSync] Pull failed, working offline', e);
                    ServerSync.enabled = false;
                    return 0;
                });
        },

        // Pousse UNE cle brute (avec prefixe complet) vers le serveur
        pushRaw: function(fullKey, rawValue){
            if(!ServerSync.enabled || _pulling) return Promise.resolve();
            var payload = {};
            payload[fullKey] = rawValue;
            return fetch('/api/sync/push', {
                method: 'POST',
                headers: {'Content-Type':'application/json'},
                body: JSON.stringify(payload)
            }).catch(function(e){
                console.warn('[ServerSync] Push failed for key:', fullKey, e);
            });
        },

        // Pousse UNE cle vers le serveur (appele par DB.set)
        push: function(k, v){
            if(!ServerSync.enabled || _pulling) return Promise.resolve();
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

        // Pousse TOUT le localStorage vers le serveur (bootstrap initial)
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

    // --- Patch localStorage.setItem pour capturer TOUTES les ecritures edhec_ ---
    var _origSetItem = Storage.prototype.setItem;
    Storage.prototype.setItem = function(key, value){
        _origSetItem.call(this, key, value);
        if(this === localStorage && key.startsWith(PREFIX) && !_pulling && ServerSync.enabled){
            ServerSync.pushRaw(key, value);
        }
    };

    // --- Auto-init avec reload unique pour forcer le re-rendu ---
    ServerSync.init().then(function(pulledCount){
        if(pulledCount > 0 && !sessionStorage.getItem('_ss_synced')){
            // Premier chargement : on a tire des donnees serveur.
            // On force un reload pour que le frontend re-rende avec les bonnes donnees.
            sessionStorage.setItem('_ss_synced', '1');
            location.reload();
            return;
        }
        // Si le serveur etait vide, pousser les donnees locales
        if(pulledCount === 0){
            console.log('[ServerSync] Server empty, pushing local data...');
            ServerSync.pushAll();
        }
    });

})();
"""
    return "<" + "script>" + js + "</" + "script>"


@app.route("/")
def index():
    html_path = os.path.join(app.static_folder, "index.html")
    with open(html_path, "r", encoding="utf-8") as f:
        html = f.read()
    # Renommer le site dans le HTML statique
    html = html.replace("RÃÂ©ussir l'EDHEC", "RÃÂ©ussir ÃÂtudes")
    html = html.replace("RÃÂ©ussir l\\'EDHEC", "RÃÂ©ussir ÃÂtudes")
    # Injecter les scripts (ServerSync + Branding + Annales admin)
    patch = build_patch_script()
    branding = build_branding_patch()
    annales = build_annales_patch()
    filesync = build_filesync_patch()
    html = html.replace("</body>", filesync + "\n" + patch + "\n" + branding + "\n" + annales + "\n</body>", 1)
    return Response(html, mimetype="text/html")


# ---------------------------------------------------------------------------
# Init & Run
# ---------------------------------------------------------------------------

init_db()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=True)
