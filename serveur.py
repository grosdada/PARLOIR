#!/usr/bin/env python3
"""Le Parloir — messagerie locale partagée entre toi et tes IA (Claude, Codex, Grok…).

Aucune dépendance : Python 3.8+ suffit.
    python3 serveur.py            -> http://127.0.0.1:7777

Réglages : fichier config.json (à côté de ce script, ou dans %APPDATA%\\Parloir pour Parloir.exe),
ou variables d'environnement PARLOIR_PORT, PARLOIR_MACHINE, PARLOIR_MEMOIRE, PARLOIR_MAX_IA.

Mémoire : chaque conversation est enregistrée dans <memoire>/<machine>/<id>.jsonl.
Si <memoire> est un dossier synchronisé (Google Drive, Dropbox, OneDrive, iCloud…),
tous tes PC partagent la même mémoire. Chaque PC n'écrit que dans son propre
sous-dossier : aucun conflit de synchronisation possible.
"""
import json
import os
import re
import socket
import threading
import time
import unicodedata
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import chemins
import systeme

CFG = chemins.lire_config()
PORT = chemins.port()
HOTE = os.environ.get("PARLOIR_HOTE") or CFG.get("hote") or "127.0.0.1"
MACHINE = re.sub(r"[^\w.-]+", "-", os.environ.get("PARLOIR_MACHINE") or CFG.get("machine") or socket.gethostname()).strip("-") or "PC"
MEMOIRE = Path(os.path.expanduser(os.environ.get("PARLOIR_MEMOIRE") or CFG.get("memoire") or str(chemins.DONNEES / "memoire")))
# Nombre max de messages d'IA d'affilée sans intervention humaine (anti ping-pong).
MAX_IA_DAFFILEE = int(os.environ.get("PARLOIR_MAX_IA") or CFG.get("max_ia") or 6)
HUMAIN = "Toi"
NOM_SUR = re.compile(r"^[\w.-]+$")

verrou = threading.Condition()
etat = {"conv": None, "fichier": None}   # conversation en cours sur ce PC
messages = []          # messages de la conversation en cours
compteur = [0]         # identifiants croissants, jamais remis à zéro
presence = {}          # nom -> {"vu": ts, "etat": "present"|"parti", "modele": str|None}


# ---------- mémoire ----------

def nouvel_id():
    return time.strftime("%Y%m%d-%H%M%S")


def lire_jsonl(chemin):
    out = []
    try:
        for ligne in chemin.read_text(encoding="utf-8").splitlines():
            if ligne.strip():
                try:
                    out.append(json.loads(ligne))
                except json.JSONDecodeError:
                    pass   # ligne tronquée pendant une synchro : on l'ignore
    except OSError:
        pass
    return out


# ---------- sujet et dossier d'une conversation ----------
# Rangés dans un petit fichier à côté du .jsonl : le format des conversations ne change pas,
# et celles d'avant gardent leur titre déduit du premier message tant qu'on ne les nomme pas.

def chemin_meta(chemin):
    return chemin.with_suffix(".meta.json")


def lire_meta(chemin):
    try:
        m = json.loads(chemin_meta(chemin).read_text(encoding="utf-8"))
        return {"titre": str(m.get("titre") or "").strip(), "dossier": str(m.get("dossier") or "").strip()}
    except (OSError, json.JSONDecodeError, AttributeError, TypeError):
        return {"titre": "", "dossier": ""}


def ecrire_meta(chemin, titre=None, dossier=None):
    """Écrit le sujet et/ou le dossier. None = on ne touche pas, chaîne vide = on efface."""
    m = lire_meta(chemin)
    if titre is not None:
        m["titre"] = " ".join(str(titre).split())[:80]
    if dossier is not None:
        m["dossier"] = " ".join(str(dossier).split())[:60]
    cible = chemin_meta(chemin)
    cible.parent.mkdir(parents=True, exist_ok=True)
    cible.write_text(json.dumps(m, ensure_ascii=False, indent=2), encoding="utf-8")
    return m


def titre_de(msgs):
    reel = [m for m in msgs if not m.get("systeme")]
    if reel and reel[0].get("importe") and reel[0].get("titre"):
        return "↪ " + reel[0]["titre"].lstrip("↪ ")   # suite d'une conversation reprise
    for m in msgs:
        if m.get("auteur") == HUMAIN and not m.get("importe"):
            t = " ".join(m["texte"].split())
            return t[:80] + ("…" if len(t) > 80 else "")
    return "(sans titre)"


_cache = {}   # chemin -> (empreinte, résumé)


def empreinte(chemin):
    """Conversation + fiche de rangement : renommer sans toucher aux messages invalide le cache."""
    st = chemin.stat()
    try:
        stm = chemin_meta(chemin).stat()
    except OSError:
        return (st.st_mtime, st.st_size, 0, 0)
    return (st.st_mtime, st.st_size, stm.st_mtime, stm.st_size)


def resume(chemin):
    e = empreinte(chemin)
    cle = str(chemin)
    if cle in _cache and _cache[cle][0] == e:
        return _cache[cle][1]
    msgs = [m for m in lire_jsonl(chemin) if not m.get("systeme")]
    meta = lire_meta(chemin)
    r = {
        "machine": chemin.parent.name,
        "id": chemin.stem,
        "titre": meta["titre"] or titre_de(msgs),
        "dossier": meta["dossier"],
        "nomme": bool(meta["titre"]),
        "debut": msgs[0]["ts"] if msgs else e[0],
        "fin": msgs[-1]["ts"] if msgs else e[0],
        "nb": len([m for m in msgs if not m.get("importe")]),
        "participants": sorted({m["auteur"] for m in msgs if not m.get("importe")}),
    }
    _cache[cle] = (e, r)
    return r


def dossiers():
    return sorted({c["dossier"] for c in toutes_les_conversations() if c["dossier"]}, key=str.lower)


def toutes_les_conversations():
    res = []
    if MEMOIRE.exists():
        for dossier in MEMOIRE.iterdir():
            if dossier.is_dir() and NOM_SUR.match(dossier.name):
                for f in dossier.glob("*.jsonl"):
                    if NOM_SUR.match(f.stem):
                        try:
                            r = resume(f)
                        except OSError:
                            continue
                        r["en_cours"] = (dossier.name == MACHINE and f.stem == etat["conv"])
                        res.append(r)
    res.sort(key=lambda r: r["fin"], reverse=True)
    return res


def chemin_conv(machine, cid):
    if not (NOM_SUR.match(machine or "") and NOM_SUR.match(cid or "")):
        return None
    c = MEMOIRE / machine / f"{cid}.jsonl"
    return c if c.exists() else None


def sans_accents(s):
    return "".join(c for c in unicodedata.normalize("NFD", s.lower()) if unicodedata.category(c) != "Mn")


def chercher(q, limite=20):
    mots = sans_accents(q).split()
    if not mots:
        return []
    res = []
    for r in toutes_les_conversations():
        c = chemin_conv(r["machine"], r["id"])
        if not c:
            continue
        for m in lire_jsonl(c):
            if m.get("systeme") or m.get("importe"):
                continue
            t = sans_accents(m["texte"])
            if all(w in t for w in mots):
                i = t.find(mots[0])
                extrait = m["texte"][max(0, i - 60): i + 140].replace("\n", " ")
                res.append({**{k: r[k] for k in ("machine", "id", "titre")},
                            "auteur": m["auteur"], "ts": m["ts"], "extrait": extrait})
                if len(res) >= limite:
                    return res
    return res


# ---------- conversation en cours ----------

def ouvrir(cid):
    etat["conv"] = cid
    etat["fichier"] = MEMOIRE / MACHINE / f"{cid}.jsonl"


def charger():
    (MEMOIRE / MACHINE).mkdir(parents=True, exist_ok=True)
    fichiers = sorted((MEMOIRE / MACHINE).glob("*.jsonl"))
    if fichiers:
        ouvrir(fichiers[-1].stem)
        messages.extend(lire_jsonl(fichiers[-1]))
        compteur[0] = max([m.get("id", 0) for m in messages] + [0])
    else:
        ouvrir(nouvel_id())


def ajouter(auteur, texte, systeme=False, **extra):
    with verrou:
        compteur[0] += 1
        msg = {"id": compteur[0], "auteur": auteur, "texte": texte, "ts": time.time(), "systeme": systeme, **extra}
        messages.append(msg)
        if not systeme:   # les annonces (entrées/sorties) ne vont pas en mémoire
            etat["fichier"].parent.mkdir(parents=True, exist_ok=True)
            with etat["fichier"].open("a", encoding="utf-8") as f:
                f.write(json.dumps(msg, ensure_ascii=False) + "\n")
        verrou.notify_all()
        return msg


def nouvelle_conversation(reprise=None, titre=None, dossier=None):
    """Démarre une conversation vierge, ou la suite d'une conversation de la mémoire."""
    with verrou:
        if not any(not m.get("systeme") for m in messages):
            # On ne garde pas une conversation vide, ni sa fiche de rangement.
            etat["fichier"].unlink(missing_ok=True)
            chemin_meta(etat["fichier"]).unlink(missing_ok=True)
        messages.clear()
        cid = nouvel_id()
        if (MEMOIRE / MACHINE / f"{cid}.jsonl").exists():
            cid += f"-{compteur[0]}"
        ouvrir(cid)
        if titre or dossier:
            ecrire_meta(etat["fichier"], titre, dossier)
    if reprise:
        machine, ancien, anciens, ancien_titre = reprise
        ajouter("Parloir", f"Suite de « {ancien_titre} » ({machine}, {time.strftime('%d/%m/%Y', time.localtime(anciens[0]['ts']))}).",
                systeme=True, nouvelle=True)
        for m in anciens:
            if not m.get("systeme"):
                ajouter(m["auteur"], m["texte"], importe=True, origine=f"{machine}/{ancien}",
                        ts_origine=m["ts"], titre=ancien_titre)
    else:
        # Le sujet part dans l'annonce : les IA savent de quoi on va parler avant le premier message.
        ajouter("Parloir", f"Nouvelle conversation : {titre}" if titre else "Nouvelle conversation.",
                systeme=True, nouvelle=True)


def changer_memoire(nouvelle, machine=None):
    """Bascule la mémoire vers un autre dossier (ex. Google Drive), sans perdre l'historique de ce PC."""
    global MEMOIRE, MACHINE
    import shutil
    with verrou:
        ancienne = MEMOIRE / MACHINE
        MEMOIRE = Path(nouvelle)
        if machine:
            MACHINE = machine
        cible = MEMOIRE / MACHINE
        cible.mkdir(parents=True, exist_ok=True)
        if ancienne.is_dir() and ancienne.resolve() != cible.resolve():
            for motif in ("*.jsonl", "*.meta.json"):
                for f in ancienne.glob(motif):
                    if not (cible / f.name).exists():
                        shutil.copy2(f, cible / f.name)
        ouvrir(etat["conv"])


def ia_daffilee():
    n = 0
    for m in reversed(messages):
        if m.get("systeme") or m.get("importe"):
            continue
        if m["auteur"] == HUMAIN:
            break
        n += 1
    return n


def presents():
    maintenant = time.time()
    return {nom: ("present" if p["etat"] == "present" and maintenant - p["vu"] < 180 else "absent")
            for nom, p in presence.items()}


def modeles():
    """Modèle déclaré par chaque participant (auto-déclaré : le parloir ne peut pas le vérifier)."""
    return {nom: p["modele"] for nom, p in presence.items() if p.get("modele")}


def noter_modele(nom, valeur):
    """Enregistre le modèle annoncé par un participant, et l'oublie si on lui passe du vide."""
    if valeur is None:
        return
    p = presence.setdefault(nom, {"vu": time.time(), "etat": "present"})
    p["modele"] = " ".join(str(valeur).split())[:60] or None


def infos():
    meta = lire_meta(etat["fichier"])
    return {"machine": MACHINE, "conv": etat["conv"], "memoire": str(MEMOIRE),
            "titre": meta["titre"] or titre_de(messages),
            "dossier": meta["dossier"], "nomme": bool(meta["titre"])}


# ---------- HTTP ----------

PAGES = {"/": "index.html", "/index.html": "index.html", "/accueil": "accueil.html"}
HOTES_OK = {f"127.0.0.1:{PORT}", f"localhost:{PORT}"}


class Gestionnaire(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _hote_ok(self):
        # Contre le « DNS rebinding » : on ne répond qu'aux adresses locales.
        return self.headers.get("Host", "") in HOTES_OK

    def _page(self, nom):
        corps = (chemins.RESSOURCES / nom).read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(corps)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(corps)

    def _json(self, code, obj):
        corps = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(corps)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(corps)

    def _corps(self):
        n = int(self.headers.get("Content-Length", "0") or 0)
        if not n:
            return {}
        try:
            return json.loads(self.rfile.read(n).decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            return {}

    def do_GET(self):
        if not self._hote_ok():
            return self._json(403, {"erreur": "accès refusé"})
        url = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(url.query).items()}
        if url.path in PAGES:
            self._page(PAGES[url.path])
        elif url.path == "/api/systeme":
            self._json(200, systeme.etat())
        elif url.path == "/api/messages":
            depuis = int(q.get("depuis", 0))
            attendre = min(float(q.get("attendre", 0)), 55.0)
            sauf = q.get("sauf", "")   # on ne se réveille pas pour ses propres messages
            nom = q.get("nom", "")
            if nom in presence:
                presence[nom]["vu"] = time.time()
            fin = time.time() + attendre

            def nouveaux():
                return [m for m in messages if m["id"] > depuis]

            def interessants():
                return [m for m in nouveaux()
                        if m["auteur"] != sauf and (not m.get("systeme") or m.get("nouvelle"))]

            with verrou:
                while attendre > 0 and not interessants():
                    reste = fin - time.time()
                    if reste <= 0:
                        break
                    verrou.wait(reste)
                res = nouveaux()
            self._json(200, {"messages": res, "presence": presents(), "modeles": modeles(),
                             "dernier": compteur[0], **infos()})
        elif url.path == "/api/presence":
            self._json(200, {"presence": presents(), "modeles": modeles(), **infos()})
        elif url.path == "/api/memoire":
            self._json(200, {"conversations": toutes_les_conversations(), "dossiers": dossiers(), **infos()})
        elif url.path == "/api/dossiers":
            self._json(200, {"dossiers": dossiers(), **infos()})
        elif url.path == "/api/maj":
            # Vérification en cache côté systeme (10 min) : la page de discussion peut demander souvent.
            m = systeme.verifier_maj()
            self._json(200, {"version": chemins.VERSION, "derniere": m.get("derniere"),
                             "disponible": bool(m.get("disponible")), "fige": chemins.FIGE})
        elif url.path == "/api/memoire/conversation":
            c = chemin_conv(q.get("machine"), q.get("id"))
            if not c:
                return self._json(404, {"erreur": "conversation introuvable"})
            msgs = [m for m in lire_jsonl(c) if not m.get("systeme")]
            self._json(200, {**resume(c), "messages": msgs})
        elif url.path == "/api/memoire/chercher":
            self._json(200, {"resultats": chercher(q.get("q", ""), int(q.get("limite", 20)))})
        else:
            self._json(404, {"erreur": "introuvable"})

    def do_POST(self):
        # Un site web ne peut pas envoyer cet en-tête au parloir sans autorisation (CORS) :
        # ça empêche une page malveillante d'écrire ou de lancer une installation à ta place.
        if not self._hote_ok() or self.headers.get("X-Parloir") != "1":
            return self._json(403, {"erreur": "accès refusé"})
        url = urlparse(self.path)
        d = self._corps()
        if url.path.startswith("/api/systeme/"):
            try:
                return self._json(200, systeme.action(url.path.rsplit("/", 1)[1], d))
            except Exception as e:
                return self._json(400, {"erreur": str(e)})
        if url.path == "/api/messages":
            auteur = str(d.get("auteur") or HUMAIN).strip()[:40]
            texte = str(d.get("texte") or "").strip()
            if not texte:
                return self._json(400, {"erreur": "message vide"})
            if auteur != HUMAIN:
                presence.setdefault(auteur, {"etat": "present"})["vu"] = time.time()
                presence[auteur]["etat"] = "present"
                noter_modele(auteur, d.get("modele"))
                if ia_daffilee() >= MAX_IA_DAFFILEE:
                    return self._json(429, {"erreur": (
                        f"{MAX_IA_DAFFILEE} messages d'IA d'affilée sans intervention humaine : "
                        "attends que l'humain reprenne la parole (utilise parloir_attendre).")})
            self._json(200, {"message": ajouter(auteur, texte)})
        elif url.path == "/api/entrer":
            nom = str(d.get("nom") or "").strip()[:40]
            if not nom:
                return self._json(400, {"erreur": "nom manquant"})
            deja = presence.get(nom, {}).get("etat") == "present"
            ancien = presence.get(nom, {}).get("modele")
            presence[nom] = {"vu": time.time(), "etat": "present", "modele": ancien}
            # Re-entrer en annonçant un autre modèle suffit à mettre l'affichage à jour.
            noter_modele(nom, d.get("modele"))
            if not deja:
                ajouter(nom, f"{nom} est entré dans le parloir.", systeme=True)
            self._json(200, {"presence": presents(), "modeles": modeles(), **infos()})
        elif url.path == "/api/quitter":
            nom = str(d.get("nom") or "").strip()[:40]
            if nom in presence:
                presence[nom]["etat"] = "parti"
                ajouter(nom, f"{nom} a quitté le parloir.", systeme=True)
            self._json(200, {"presence": presents()})
        elif url.path in ("/api/nouvelle", "/api/effacer"):
            nouvelle_conversation(titre=d.get("titre"), dossier=d.get("dossier"))
            self._json(200, infos())
        elif url.path == "/api/renommer":
            # Sans machine/id : on renomme la conversation en cours.
            c = etat["fichier"] if not d.get("id") else chemin_conv(d.get("machine"), d.get("id"))
            if not c:
                return self._json(404, {"erreur": "conversation introuvable"})
            ecrire_meta(c, d.get("titre"), d.get("dossier"))
            # « titre »/« dossier » décrivent toujours la conversation en cours (en-tête de la page) ;
            # « conversation » décrit celle qu'on vient de renommer, en cours ou archivée.
            self._json(200, {"conversation": resume(c) if c.exists() else None,
                             "dossiers": dossiers(), **infos()})
        elif url.path == "/api/reprendre":
            c = chemin_conv(d.get("machine"), d.get("id"))
            if not c:
                return self._json(404, {"erreur": "conversation introuvable"})
            anciens = [m for m in lire_jsonl(c) if not m.get("systeme")]
            if not anciens:
                return self._json(400, {"erreur": "conversation vide"})
            r = resume(c)
            ancien_titre = r["titre"]
            # La suite reste rangée au même endroit, sous le même sujet, marqué « ↪ ».
            nouvelle_conversation(reprise=(d["machine"], d["id"], anciens, ancien_titre),
                                  titre=d.get("titre") or "↪ " + ancien_titre.lstrip("↪ "),
                                  dossier=d["dossier"] if d.get("dossier") is not None else r["dossier"])
            self._json(200, infos())
        else:
            self._json(404, {"erreur": "introuvable"})


def main():
    charger()
    serveur = ThreadingHTTPServer((HOTE, PORT), Gestionnaire)
    serveur.daemon_threads = True
    systeme.arreter_serveur = serveur.shutdown
    print(f"Le Parloir est ouvert : http://{HOTE}:{PORT}\n"
          f"  PC : {MACHINE}\n  Mémoire : {MEMOIRE}", flush=True)
    try:
        serveur.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
