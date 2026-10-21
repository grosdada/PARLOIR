#!/usr/bin/env python3
"""Pont MCP (stdio) vers Le Parloir — à brancher dans n'importe quel agent qui parle MCP
(Claude Code, Codex, Gemini CLI, Cursor…). Seul le nom change :

    python3 mcp_parloir.py --nom Claude
    python3 mcp_parloir.py --nom Codex
    python3 mcp_parloir.py --nom Gemini

Si le serveur du parloir n'est pas lancé, ce pont le démarre tout seul.
Aucune dépendance : Python 3.8+ suffit.
"""
import argparse
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ICI = Path(__file__).resolve().parent
try:
    _CFG = json.loads((ICI / "config.json").read_text(encoding="utf-8"))
except (OSError, json.JSONDecodeError):
    _CFG = {}
PORT = int(os.environ.get("PARLOIR_PORT") or _CFG.get("port") or 7777)
BASE = os.environ.get("PARLOIR_URL", f"http://127.0.0.1:{PORT}")

ap = argparse.ArgumentParser()
ap.add_argument("--nom", default=os.environ.get("PARLOIR_NOM", "Invité"),
                help="Nom affiché dans le parloir (Claude, Codex, Gemini…)")
NOM = ap.parse_args().nom

dernier_vu = 0

CONSIGNES = f"""Tu es « {NOM} » dans LE PARLOIR, une messagerie partagée entre l'humain (affiché « Toi ») et plusieurs IA (Claude, Codex, et d'autres invités possibles).
Quand l'utilisateur te dit « connecte-toi au parloir » (ou équivalent) :
1. Appelle parloir_entrer.
2. Puis boucle : parloir_attendre -> si de nouveaux messages arrivent, réponds avec parloir_ecrire si c'est pertinent -> parloir_attendre de nouveau. Ne t'arrête pas de toi-même : reste dans la boucle même quand l'attente revient vide.
Règles de conversation :
- Réponds toujours aux messages de « Toi » (l'humain), sauf s'il s'adresse explicitement à une autre IA seulement (ex. « @Codex … »). Si on t'écrit « @{NOM} », c'est pour toi.
- Tu peux répondre aux autres IA, la compléter ou la contredire, mais seulement si tu apportes quelque chose de neuf. Pas de politesses en boucle, pas de « je suis d'accord » vide.
- Si une autre IA a déjà bien répondu, tu peux te taire (rappelle simplement parloir_attendre).
- Messages concis, en français, Markdown accepté.
- Tu restes un agent : tu peux utiliser tes autres outils (lire des fichiers, coder…) si l'humain te le demande dans le parloir, puis rendre compte dans le parloir.
- Si l'humain écrit « /quitter » ou te demande de partir, appelle parloir_quitter et termine ton tour.
Mémoire : toutes les conversations passées (de tous les PC de l'humain) sont consultables avec parloir_memoire.
Utilise-la quand l'humain fait référence à une discussion antérieure (« comme on disait l'autre jour », « sur l'autre PC »…).
"""

OUTILS = [
    {
        "name": "parloir_entrer",
        "description": ("Entrer dans Le Parloir (à utiliser quand l'utilisateur dit « connecte-toi au parloir »). "
                        "Renvoie les consignes et les derniers messages. Ensuite, boucle sur parloir_attendre."),
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "parloir_attendre",
        "description": ("Attendre de nouveaux messages des autres participants (jusqu'à ~50 s). "
                        "Si rien n'arrive, rappelle-le simplement : c'est normal, reste dans la boucle."),
        "inputSchema": {"type": "object", "properties": {
            "secondes": {"type": "number", "description": "Durée max d'attente (1-50, défaut 45)."}}},
    },
    {
        "name": "parloir_ecrire",
        "description": "Publier un message dans Le Parloir, visible par l'humain et les autres IA.",
        "inputSchema": {"type": "object", "properties": {
            "texte": {"type": "string", "description": "Le message (Markdown accepté)."}},
            "required": ["texte"]},
    },
    {
        "name": "parloir_lire",
        "description": "Relire les N derniers messages du parloir (historique).",
        "inputSchema": {"type": "object", "properties": {
            "nombre": {"type": "integer", "description": "Nombre de messages (défaut 30)."}}},
    },
    {
        "name": "parloir_memoire",
        "description": ("Consulter la mémoire du parloir : les conversations passées, y compris celles tenues sur les autres PC. "
                        "Sans argument : liste les conversations récentes. Avec « chercher » : recherche plein texte. "
                        "Avec « machine » + « id » : relit une conversation entière."),
        "inputSchema": {"type": "object", "properties": {
            "chercher": {"type": "string", "description": "Mots à chercher (insensible aux accents et à la casse)."},
            "machine": {"type": "string", "description": "PC d'origine de la conversation à relire."},
            "id": {"type": "string", "description": "Identifiant de la conversation à relire."}}},
    },
    {
        "name": "parloir_quitter",
        "description": "Quitter Le Parloir (quand l'humain le demande ou écrit /quitter).",
        "inputSchema": {"type": "object", "properties": {}},
    },
]


# ---------- accès HTTP au serveur ----------

# Le parloir est local : on contourne tout proxy système.
_ouvreur = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def http(methode, chemin, corps=None, delai=10):
    data = json.dumps(corps).encode("utf-8") if corps is not None else None
    req = urllib.request.Request(BASE + chemin, data=data, method=methode,
                                 headers={"Content-Type": "application/json"})
    try:
        with _ouvreur.open(req, timeout=delai) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            return json.loads(e.read().decode("utf-8"))
        except Exception:
            return {"erreur": f"HTTP {e.code}"}


def serveur_pret():
    try:
        http("GET", "/api/presence", delai=2)
        return True
    except Exception:
        return False


def assurer_serveur():
    if serveur_pret():
        return
    subprocess.Popen([sys.executable, str(ICI / "serveur.py")],
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     stdin=subprocess.DEVNULL, start_new_session=True)
    for _ in range(40):
        time.sleep(0.25)
        if serveur_pret():
            return
    raise RuntimeError(f"Impossible de joindre le parloir sur {BASE}")


def formater(msgs):
    lignes = []
    for m in msgs:
        ts = m.get("ts_origine") or m["ts"]
        heure = time.strftime("%d/%m %H:%M" if m.get("importe") else "%H:%M", time.localtime(ts))
        if m.get("systeme"):
            lignes.append(f"[{heure}] · {m['texte']}")
        else:
            moi = " (toi-même)" if m["auteur"] == NOM else ""
            lignes.append(f"[{heure}] {m['auteur']}{moi} :\n{m['texte']}")
    return "\n\n".join(lignes)


def presence_txt(p):
    return ", ".join(f"{n} ({e})" for n, e in p.items()) or "personne"


def date(ts):
    return time.strftime("%d/%m/%Y %H:%M", time.localtime(ts))


# ---------- outils ----------

def outil(nom, args):
    global dernier_vu
    assurer_serveur()

    if nom == "parloir_entrer":
        r = http("POST", "/api/entrer", {"nom": NOM})
        h = http("GET", "/api/messages?depuis=0")
        msgs = h["messages"]
        dernier_vu = h["dernier"]
        return (CONSIGNES + f"\nInterface pour l'humain : {BASE}\n"
                f"PC : {h.get('machine')} · Présents : {presence_txt(r['presence'])}\n\n"
                f"--- Conversation en cours ---\n{formater(msgs[-30:]) or '(aucun message pour l’instant)'}\n\n"
                "Maintenant, appelle parloir_attendre.")

    if nom == "parloir_attendre":
        s = max(1, min(float(args.get("secondes") or 45), 50))
        h = http("GET", f"/api/messages?depuis={dernier_vu}&attendre={s}&sauf={NOM}&nom={NOM}", delai=s + 10)
        dernier_vu = h["dernier"]
        msgs = h["messages"]
        # Nouvelle conversation (ou reprise) : on repart de là.
        idx = [i for i, m in enumerate(msgs) if m.get("nouvelle")]
        entete = ""
        if idx:
            msgs = msgs[idx[-1]:]
            reprise = [m for m in msgs if m.get("importe")]
            entete = f"=== {msgs[0]['texte']} ===\n"
            if reprise:
                entete += ("Voici l'échange repris de la mémoire (contexte, n'y réponds pas) :\n\n"
                           f"{formater(reprise)}\n\n=== Fin du contexte ===\n")
            else:
                entete += "Oublie la conversation précédente : on repart de zéro.\n"
        nouveaux = [m for m in msgs if m["auteur"] != NOM and not m.get("systeme") and not m.get("importe")]
        if not nouveaux:
            if entete:
                return entete + "\nAttends le prochain message : rappelle parloir_attendre."
            return "Rien de nouveau. Rappelle parloir_attendre pour continuer à écouter."
        quitte = any(m["auteur"] == "Toi" and m["texte"].strip().lower().startswith("/quitter") for m in nouveaux)
        suite = ("\n\nL'humain a demandé /quitter : appelle parloir_quitter puis termine." if quitte else
                 "\n\nRéponds avec parloir_ecrire si c'est pertinent, puis rappelle parloir_attendre.")
        return f"{entete}Nouveaux messages :\n\n{formater(nouveaux)}{suite}"

    if nom == "parloir_ecrire":
        texte = str(args.get("texte") or "").strip()
        if not texte:
            raise ValueError("texte vide")
        r = http("POST", "/api/messages", {"auteur": NOM, "texte": texte})
        if "erreur" in r:
            raise ValueError(r["erreur"])
        return "Message publié. Rappelle parloir_attendre."

    if nom == "parloir_lire":
        n = int(args.get("nombre") or 30)
        h = http("GET", "/api/messages?depuis=0")
        return formater(h["messages"][-n:]) or "(aucun message)"

    if nom == "parloir_memoire":
        from urllib.parse import quote
        if args.get("machine") and args.get("id"):
            c = http("GET", f"/api/memoire/conversation?machine={quote(args['machine'])}&id={quote(args['id'])}")
            if "erreur" in c:
                raise ValueError(c["erreur"])
            return (f"« {c['titre']} » — PC {c['machine']}, {date(c['debut'])}\n\n"
                    + formater(c["messages"]))
        if args.get("chercher"):
            r = http("GET", f"/api/memoire/chercher?q={quote(args['chercher'])}")["resultats"]
            if not r:
                return "Rien trouvé dans la mémoire."
            return "Résultats (relis une conversation avec machine + id) :\n\n" + "\n".join(
                f"- [{x['machine']} / {x['id']}] « {x['titre']} » — {x['auteur']}, {date(x['ts'])} : …{x['extrait']}…"
                for x in r)
        convs = http("GET", "/api/memoire")["conversations"][:30]
        if not convs:
            return "La mémoire est vide."
        return "Conversations récentes (tous PC) :\n\n" + "\n".join(
            f"- [{c['machine']} / {c['id']}] « {c['titre']} » — {date(c['fin'])}, {c['nb']} messages, "
            f"{', '.join(c['participants'])}{' (en cours)' if c.get('en_cours') else ''}"
            for c in convs)

    if nom == "parloir_quitter":
        http("POST", "/api/quitter", {"nom": NOM})
        return "Tu as quitté le parloir. Tu peux terminer ton tour."

    raise ValueError(f"outil inconnu : {nom}")


# ---------- protocole MCP (JSON-RPC sur stdio) ----------

def envoyer(obj):
    sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def traiter(req):
    methode, rid = req.get("method"), req.get("id")
    if rid is None:            # notification : pas de réponse
        return
    if methode == "initialize":
        version = (req.get("params") or {}).get("protocolVersion") or "2025-06-18"
        return envoyer({"jsonrpc": "2.0", "id": rid, "result": {
            "protocolVersion": version,
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "parloir", "version": "1.0.0"},
            "instructions": CONSIGNES,
        }})
    if methode == "ping":
        return envoyer({"jsonrpc": "2.0", "id": rid, "result": {}})
    if methode == "tools/list":
        return envoyer({"jsonrpc": "2.0", "id": rid, "result": {"tools": OUTILS}})
    if methode == "tools/call":
        p = req.get("params") or {}
        try:
            texte, erreur = outil(p.get("name"), p.get("arguments") or {}), False
        except Exception as e:
            texte, erreur = f"Erreur : {e}", True
        return envoyer({"jsonrpc": "2.0", "id": rid, "result": {
            "content": [{"type": "text", "text": texte}], "isError": erreur}})
    envoyer({"jsonrpc": "2.0", "id": rid, "error": {"code": -32601, "message": f"méthode inconnue : {methode}"}})


def main():
    # UTF-8 partout (Windows utilise sinon l'encodage de la console).
    sys.stdin.reconfigure(encoding="utf-8")
    sys.stdout.reconfigure(encoding="utf-8", newline="\n")
    for ligne in sys.stdin:
        ligne = ligne.strip()
        if not ligne:
            continue
        try:
            traiter(json.loads(ligne))
        except json.JSONDecodeError:
            envoyer({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "JSON invalide"}})


if __name__ == "__main__":
    main()
