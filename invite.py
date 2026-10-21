#!/usr/bin/env python3
"""Inviter n'importe quel LLM « par API » dans Le Parloir (Grok, GPT, Mistral, DeepSeek, Ollama…).

Toute API compatible OpenAI (/chat/completions) fonctionne. Exemples :

    export XAI_API_KEY=...            ;  python3 invite.py grok
    export OPENAI_API_KEY=...         ;  python3 invite.py openai
    export MISTRAL_API_KEY=...        ;  python3 invite.py mistral
    python3 invite.py ollama --modele llama3.1          # local, sans clé

    # n'importe quel autre fournisseur :
    python3 invite.py --nom Kimi --url https://api.moonshot.ai/v1 --modele kimi-k2 --cle-env MOONSHOT_API_KEY

Ctrl+C pour le faire sortir du parloir. Aucune dépendance : Python 3.8+ suffit.
"""
import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request

import chemins
# Raccourcis : nom affiché, URL de base, modèle par défaut, variable d'environnement de la clé.
# Les noms de modèles évoluent vite : remplace-les avec --modele si besoin.
PROFILS = {
    "grok":     ("Grok",     "https://api.x.ai/v1",                                       "grok-4",              "XAI_API_KEY"),
    "openai":   ("GPT",      "https://api.openai.com/v1",                                 "gpt-5",               "OPENAI_API_KEY"),
    "mistral":  ("Mistral",  "https://api.mistral.ai/v1",                                 "mistral-large-latest", "MISTRAL_API_KEY"),
    "deepseek": ("DeepSeek", "https://api.deepseek.com/v1",                               "deepseek-chat",       "DEEPSEEK_API_KEY"),
    "gemini":   ("Gemini",   "https://generativelanguage.googleapis.com/v1beta/openai",   "gemini-2.5-pro",      "GEMINI_API_KEY"),
    "ollama":   ("Ollama",   "http://localhost:11434/v1",                                 "llama3.1",            ""),
}

ap = argparse.ArgumentParser(description="Inviter un LLM (API compatible OpenAI) dans Le Parloir.")
ap.add_argument("profil", nargs="?", choices=sorted(PROFILS), help="raccourci de fournisseur")
ap.add_argument("--nom", help="nom affiché dans le parloir")
ap.add_argument("--url", help="URL de base de l'API (…/v1)")
ap.add_argument("--modele", help="nom du modèle")
ap.add_argument("--cle-env", help="variable d'environnement contenant la clé API")
ap.add_argument("--personnalite", default="", help="consigne de caractère en plus (facultatif)")
ap.add_argument("--parloir", default=os.environ.get("PARLOIR_URL", f"http://127.0.0.1:{chemins.port()}"))
a = ap.parse_args()

p = PROFILS.get(a.profil, (None, None, None, None))
NOM = a.nom or p[0] or "Invité"
URL = (a.url or p[1] or "").rstrip("/")
MODELE = a.modele or p[2]
CLE_ENV = a.cle_env if a.cle_env is not None else (p[3] or "")
CLE = os.environ.get(CLE_ENV, "") if CLE_ENV else ""
PARLOIR = a.parloir.rstrip("/")
SILENCE = "[silence]"

if not URL or not MODELE:
    sys.exit("Indique un profil (ex. « grok ») ou bien --url et --modele.")
if CLE_ENV and not CLE:
    sys.exit(f"La variable {CLE_ENV} est vide : exporte ta clé API d'abord.")

SYSTEME = f"""Tu es « {NOM} », invité dans LE PARLOIR : une messagerie de groupe entre un humain (affiché « Toi ») et plusieurs IA (Claude, Codex et d'autres).
Les messages des autres participants te parviennent sous la forme « Nom : texte ».
Règles :
- Réponds toujours à « Toi », sauf s'il s'adresse explicitement à quelqu'un d'autre seulement (ex. « @Claude … »). « @{NOM} » = c'est pour toi.
- Tu peux répondre aux autres IA, les compléter ou les contredire, mais seulement si tu apportes quelque chose de neuf.
- Si tu n'as rien d'utile à ajouter, réponds exactement {SILENCE} (rien d'autre) : ce ne sera pas publié.
- Ne préfixe pas ta réponse par ton nom. Sois concis, en français, Markdown accepté.
{a.personnalite}""".strip()


_direct = urllib.request.build_opener(urllib.request.ProxyHandler({}))   # parloir local : sans proxy


def http(methode, url, corps=None, entetes=None, delai=30):
    data = json.dumps(corps).encode("utf-8") if corps is not None else None
    h = {"Content-Type": "application/json", **(entetes or {})}
    local = url.startswith(PARLOIR)
    if local:
        h["X-Parloir"] = "1"
    req = urllib.request.Request(url, data=data, method=methode, headers=h)
    ouvrir = _direct.open if local else urllib.request.urlopen
    try:
        with ouvrir(req, timeout=delai) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        corps = e.read().decode("utf-8", "replace")
        try:
            return json.loads(corps)
        except json.JSONDecodeError:
            return {"erreur": f"HTTP {e.code} : {corps[:300]}"}


def demander_au_modele(historique):
    msgs = [{"role": "system", "content": SYSTEME}]
    for m in historique[-40:]:
        if m.get("systeme"):
            continue
        if m["auteur"] == NOM:
            msgs.append({"role": "assistant", "content": m["texte"]})
        else:
            msgs.append({"role": "user", "content": f"{m['auteur']} : {m['texte']}"})
    entetes = {"Authorization": f"Bearer {CLE}"} if CLE else {}
    r = http("POST", f"{URL}/chat/completions", {"model": MODELE, "messages": msgs}, entetes, delai=180)
    try:
        return r["choices"][0]["message"]["content"].strip()
    except (KeyError, IndexError, TypeError, AttributeError):
        raise RuntimeError(r.get("erreur") or r.get("error") or r)


def concerne_moi(nouveaux):
    """Faut-il solliciter le modèle ? Non si l'humain parle uniquement à d'autres (@Autre sans @moi)."""
    for m in nouveaux:
        if m.get("importe"):
            continue
        t = m["texte"]
        if f"@{NOM}".lower() in t.lower():
            return True
        if m["auteur"] == "Toi" and not t.lstrip().startswith("@"):
            return True
        if m["auteur"] != "Toi":
            return True   # message d'une autre IA : le modèle décide (ou [silence])
    return False


def main():
    try:
        http("GET", f"{PARLOIR}/api/presence", delai=3)
    except Exception:
        sys.exit(f"Le parloir n'est pas joignable sur {PARLOIR}. Lance d'abord : python3 serveur.py")

    http("POST", f"{PARLOIR}/api/entrer", {"nom": NOM})
    h = http("GET", f"{PARLOIR}/api/messages?depuis=0")
    historique, dernier = h["messages"], h["dernier"]
    print(f"{NOM} ({MODELE}) est dans le parloir : {PARLOIR}  — Ctrl+C pour sortir.", flush=True)

    try:
        while True:
            h = http("GET", f"{PARLOIR}/api/messages?depuis={dernier}&attendre=45&sauf={NOM}&nom={NOM}", delai=60)
            recus = h["messages"]
            idx = [i for i, m in enumerate(recus) if m.get("nouvelle")]
            if idx:   # nouvelle conversation (ou reprise depuis la mémoire) : on repart de là
                recus, historique = recus[idx[-1]:], []
            nouveaux = [m for m in recus if m["auteur"] != NOM and not m.get("systeme") and not m.get("importe")]
            historique += recus
            dernier = h["dernier"]
            if not nouveaux:
                continue
            if any(m["auteur"] == "Toi" and m["texte"].strip().lower().startswith("/quitter") for m in nouveaux):
                break
            if not concerne_moi(nouveaux):
                continue
            time.sleep(2)   # laisse les autres finir d'écrire, pour répondre à l'ensemble
            h = http("GET", f"{PARLOIR}/api/messages?depuis={dernier}")
            historique += h["messages"]
            dernier = h["dernier"]
            try:
                reponse = demander_au_modele(historique)
            except Exception as e:
                print(f"Erreur du modèle : {e}", file=sys.stderr, flush=True)
                continue
            if not reponse or reponse.strip() == SILENCE:
                continue
            r = http("POST", f"{PARLOIR}/api/messages", {"auteur": NOM, "texte": reponse})
            if "erreur" in r:
                print(f"Parloir : {r['erreur']}", file=sys.stderr, flush=True)
    except KeyboardInterrupt:
        pass
    finally:
        http("POST", f"{PARLOIR}/api/quitter", {"nom": NOM})
        print(f"{NOM} a quitté le parloir.")


if __name__ == "__main__":
    main()
