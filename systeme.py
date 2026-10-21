"""Actions « système » de l'app : installer, brancher les IA, lancer les invités, mémoire, mises à jour.

Tout est piloté depuis la page d'accueil (accueil.html) via /api/systeme/…
"""
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import chemins

DEPOT = "grosdada/parloir"
PHRASE = "Connecte-toi au parloir"
SANS_FENETRE = 0x08000000 if chemins.WINDOWS else 0      # CREATE_NO_WINDOW
NOUVELLE_CONSOLE = 0x00000010 if chemins.WINDOWS else 0  # CREATE_NEW_CONSOLE
MAISON = Path.home()

arreter_serveur = None   # renseigné par serveur.main()

# Fournisseurs d'IA « par API » : nom affiché, variable de clé, modèle par défaut, lien pour obtenir une clé.
FOURNISSEURS = {
    "grok":     {"nom": "Grok",     "cle": "XAI_API_KEY",      "modele": "grok-4",               "lien": "https://console.x.ai"},
    "deepseek": {"nom": "DeepSeek", "cle": "DEEPSEEK_API_KEY", "modele": "deepseek-chat",        "lien": "https://platform.deepseek.com/api_keys"},
    "openai":   {"nom": "GPT",      "cle": "OPENAI_API_KEY",   "modele": "gpt-5",                "lien": "https://platform.openai.com/api-keys"},
    "mistral":  {"nom": "Mistral",  "cle": "MISTRAL_API_KEY",  "modele": "mistral-large-latest", "lien": "https://console.mistral.ai/api-keys"},
    "gemini":   {"nom": "Gemini",   "cle": "GEMINI_API_KEY",   "modele": "gemini-2.5-pro",       "lien": "https://aistudio.google.com/apikey"},
    "ollama":   {"nom": "Ollama",   "cle": "",                 "modele": "llama3.1",             "lien": "https://ollama.com/download"},
}

_verrou = threading.Lock()
_install = {"en_cours": False, "etapes": []}
_maj = {"en_cours": False, "derniere": None, "url": None, "notes": "", "erreur": None, "progression": 0, "verifie": 0}
_invites = {}   # fournisseur -> subprocess.Popen


# ---------- outils ----------

def executer(cmd, delai=600, **kw):
    """Lance une commande sans ouvrir de fenêtre ; renvoie (code, sortie)."""
    try:
        r = subprocess.run([str(c) for c in cmd], capture_output=True, text=True, timeout=delai,
                           creationflags=SANS_FENETRE, encoding="utf-8", errors="replace", **kw)
        return r.returncode, (r.stdout or "") + (r.stderr or "")
    except FileNotFoundError:
        return 127, f"introuvable : {cmd[0]}"
    except subprocess.TimeoutExpired:
        return 124, "trop long, abandon"


def premier(*candidats):
    for c in candidats:
        if c and Path(c).exists():
            return str(c)
    return None


def env(nom, defaut=""):
    return os.environ.get(nom, defaut)


def trouver(nom):
    """Cherche un programme, y compris là où les installateurs le mettent (le PATH n'est pas rafraîchi)."""
    w = shutil.which(nom)
    if w:
        return w
    if not chemins.WINDOWS:
        return premier(MAISON / ".local" / "bin" / nom)
    pf = env("ProgramFiles", r"C:\Program Files")
    return {
        "claude": premier(MAISON / ".local" / "bin" / "claude.exe"),
        "codex": premier(Path(env("APPDATA")) / "npm" / "codex.cmd"),
        "npm": premier(Path(pf) / "nodejs" / "npm.cmd"),
        "git": premier(Path(pf) / "Git" / "cmd" / "git.exe"),
        "winget": premier(Path(env("LOCALAPPDATA")) / "Microsoft" / "WindowsApps" / "winget.exe"),
    }.get(nom)


def commande_mcp(nom):
    """La commande qui branche une IA au parloir (exe installé, ou script Python)."""
    if chemins.FIGE:
        return [str(chemins.INSTALL / "parloir-mcp.exe"), "--nom", nom]
    return [sys.executable, str(chemins.ICI / "mcp_parloir.py"), "--nom", nom]


def version_tuple(v):
    return tuple(int(x) for x in re.findall(r"\d+", v or "0")[:3]) or (0,)


# ---------- état ----------

def etat():
    import serveur
    cfg = chemins.lire_config()
    with _verrou:
        invites = {k: (p.poll() is None) for k, p in _invites.items()}
    return {
        "version": chemins.VERSION,
        "fige": chemins.FIGE,
        "windows": chemins.WINDOWS,
        "installe": bool(cfg.get("installe")) or (not chemins.FIGE and not os.environ.get("PARLOIR_DEMO_INSTALL")),
        "install": _install,
        "claude": bool(trouver("claude")),
        "codex": bool(trouver("codex")),
        "machine": serveur.MACHINE,
        "memoire": str(serveur.MEMOIRE),
        "suggestions": suggestions_memoire(),
        "fournisseurs": {k: {**v, "a_cle": bool(cfg.get("cles", {}).get(k)) or not v["cle"],
                             "modele_choisi": cfg.get("modeles", {}).get(k) or v["modele"],
                             "present": invites.get(k, False)}
                         for k, v in FOURNISSEURS.items()},
        "maj": _maj,
        "commande_mcp": commande_mcp("NOM"),
        "dossier_donnees": str(chemins.DONNEES),
    }


# ---------- installation ----------

def _etape(nom):
    e = {"nom": nom, "etat": "encours", "detail": ""}
    _install["etapes"].append(e)
    return e


def _fin(e, etat_, detail=""):
    e["etat"], e["detail"] = etat_, detail


def copier_application():
    """Copie Parloir.exe et parloir-mcp.exe dans %LOCALAPPDATA%\\Programs\\Parloir."""
    chemins.INSTALL.mkdir(parents=True, exist_ok=True)
    moi = Path(sys.executable).resolve()
    cible = chemins.INSTALL / "Parloir.exe"
    if moi != cible.resolve():
        shutil.copy2(moi, cible)
    source_mcp = chemins.RESSOURCES / "parloir-mcp.exe"
    cible_mcp = chemins.INSTALL / "parloir-mcp.exe"
    if source_mcp.exists() and (not cible_mcp.exists() or empreinte(source_mcp) != empreinte(cible_mcp)):
        shutil.copy2(source_mcp, cible_mcp)


def empreinte(p):
    return hashlib.sha256(Path(p).read_bytes()).hexdigest()


def brancher_claude():
    exe = trouver("claude")
    if not exe:
        raise RuntimeError("Claude Code introuvable")
    executer([exe, "mcp", "remove", "parloir", "--scope", "user"], delai=60)
    code, sortie = executer([exe, "mcp", "add", "--scope", "user", "parloir", "--", *commande_mcp("Claude")], delai=60)
    if code != 0:
        raise RuntimeError(sortie.strip()[-300:])
    # Autoriser les outils du parloir : pas de demande de permission à chaque message.
    reglages = MAISON / ".claude" / "settings.json"
    try:
        d = json.loads(reglages.read_text(encoding="utf-8")) if reglages.exists() else {}
    except json.JSONDecodeError:
        return "branché (réglages de permissions illisibles, laissés tels quels)"
    permis = d.setdefault("permissions", {}).setdefault("allow", [])
    if "mcp__parloir" not in permis:
        permis.append("mcp__parloir")
        reglages.parent.mkdir(parents=True, exist_ok=True)
        if reglages.exists():
            shutil.copy(reglages, reglages.with_name("settings.json.avant-parloir"))
        reglages.write_text(json.dumps(d, indent=2, ensure_ascii=False), encoding="utf-8")
    return "branché"


def toml_chaine(s):
    return "'" + s + "'" if "'" not in s else json.dumps(s)


def brancher_codex():
    conf = MAISON / ".codex" / "config.toml"
    texte = conf.read_text(encoding="utf-8") if conf.exists() else ""
    if conf.exists():
        shutil.copy(conf, conf.with_name("config.toml.avant-parloir"))
    # On remplace l'ancien bloc s'il existe (le chemin a pu changer).
    # (le bloc s'arrête à la prochaine ligne qui commence par « [ », c.-à-d. la section suivante)
    texte = re.sub(r"^\[mcp_servers\.parloir\][^\n]*\n(?:(?!\[)[^\n]*\n?)*", "", texte, flags=re.M).rstrip() + "\n"
    cmd = commande_mcp("Codex")
    bloc = ("\n[mcp_servers.parloir]\n"
            f"command = {toml_chaine(cmd[0])}\n"
            f"args = [{', '.join(toml_chaine(a) for a in cmd[1:])}]\n"
            "tool_timeout_sec = 120\n")
    conf.parent.mkdir(parents=True, exist_ok=True)
    conf.write_text(texte.lstrip("\n") + bloc, encoding="utf-8")
    return "branché"


def winget(paquet):
    w = trouver("winget")
    if not w:
        raise RuntimeError("winget absent : installe ce programme à la main")
    code, sortie = executer([w, "install", "--id", paquet, "-e", "--silent",
                             "--accept-package-agreements", "--accept-source-agreements"], delai=1200)
    if code != 0 and "already installed" not in sortie.lower() and "déjà installé" not in sortie.lower():
        raise RuntimeError(sortie.strip()[-300:])


def raccourcis():
    cible = chemins.INSTALL / "Parloir.exe"
    ps = f"""
$s = New-Object -ComObject WScript.Shell
foreach ($d in @([Environment]::GetFolderPath('Desktop'), [Environment]::GetFolderPath('Programs'))) {{
  $l = $s.CreateShortcut((Join-Path $d 'Le Parloir.lnk'))
  $l.TargetPath = '{cible}'
  $l.WorkingDirectory = '{chemins.INSTALL}'
  $l.Description = 'Le Parloir'
  $l.Save()
}}"""
    code, sortie = executer(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", ps], delai=60)
    if code != 0:
        raise RuntimeError(sortie.strip()[-300:])


def _installer(avec_claude, avec_codex):
    try:
        win = chemins.WINDOWS
        if chemins.FIGE:
            e = _etape("Installer l'application")
            try:
                copier_application()
                _fin(e, "ok")
            except Exception as x:
                _fin(e, "erreur", str(x))

        if avec_claude:
            if not trouver("claude"):
                if win and not trouver("git"):
                    e = _etape("Installer Git (nécessaire à Claude Code)")
                    try:
                        winget("Git.Git")
                        _fin(e, "ok")
                    except Exception as x:
                        _fin(e, "erreur", f"{x} — télécharge Git sur git-scm.com")
                e = _etape("Installer Claude Code")
                if win:
                    code, sortie = executer(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command",
                                             "irm https://claude.ai/install.ps1 | iex"], delai=1200)
                else:
                    code, sortie = executer(["bash", "-c", "curl -fsSL https://claude.ai/install.sh | bash"], delai=1200)
                _fin(e, "ok" if trouver("claude") else "erreur", "" if trouver("claude") else sortie.strip()[-300:])
            e = _etape("Brancher Claude")
            try:
                _fin(e, "ok", brancher_claude())
            except Exception as x:
                _fin(e, "erreur", str(x))

        if avec_codex:
            if not trouver("codex"):
                if not trouver("npm"):
                    e = _etape("Installer Node.js (nécessaire à Codex)")
                    try:
                        if not win:
                            raise RuntimeError("installe Node.js depuis nodejs.org")
                        winget("OpenJS.NodeJS.LTS")
                        _fin(e, "ok")
                    except Exception as x:
                        _fin(e, "erreur", str(x))
                e = _etape("Installer Codex")
                npm = trouver("npm")
                if npm:
                    code, sortie = executer([npm, "install", "-g", "@openai/codex"], delai=1200)
                    _fin(e, "ok" if trouver("codex") else "erreur", "" if trouver("codex") else sortie.strip()[-300:])
                else:
                    _fin(e, "erreur", "Node.js introuvable")
            e = _etape("Brancher Codex")
            try:
                _fin(e, "ok", brancher_codex())
            except Exception as x:
                _fin(e, "erreur", str(x))

        if chemins.FIGE and win:
            e = _etape("Créer le raccourci « Le Parloir » sur le Bureau")
            try:
                raccourcis()
                _fin(e, "ok")
            except Exception as x:
                _fin(e, "erreur", str(x))

        chemins.ecrire_config({"installe": True})
    finally:
        _install["en_cours"] = False


# ---------- lancer les IA ----------

def lancer_agent(nom):
    exe = trouver(nom)
    if not exe:
        raise RuntimeError(f"{nom} n'est pas installé")
    if not chemins.WINDOWS:
        raise RuntimeError(f"Ouvre ton terminal et lance : {nom} \"{PHRASE}\"")
    chemins.DONNEES.mkdir(parents=True, exist_ok=True)
    # Une nouvelle fenêtre, avec la phrase déjà dite.
    subprocess.Popen([exe, PHRASE], cwd=str(chemins.DONNEES), creationflags=NOUVELLE_CONSOLE)
    return {"ok": True}


def lancer_invite(cle_fournisseur, cle_api=None, modele=None):
    f = FOURNISSEURS.get(cle_fournisseur)
    if not f:
        raise RuntimeError("fournisseur inconnu")
    cfg = chemins.lire_config()
    cles = cfg.get("cles", {})
    modeles = cfg.get("modeles", {})
    if cle_api:
        cles[cle_fournisseur] = cle_api.strip()
    if modele:
        modeles[cle_fournisseur] = modele.strip()
    chemins.ecrire_config({"cles": cles, "modeles": modeles})
    if f["cle"] and not cles.get(cle_fournisseur):
        raise RuntimeError(f"Colle d'abord ta clé API {f['nom']}")
    arreter_invite(cle_fournisseur)
    e = dict(os.environ)
    if f["cle"]:
        e[f["cle"]] = cles[cle_fournisseur]
    args = [cle_fournisseur, "--modele", modeles.get(cle_fournisseur) or f["modele"]]
    cmd = [sys.executable, "--invite", *args] if chemins.FIGE else [sys.executable, str(chemins.ICI / "invite.py"), *args]
    chemins.DONNEES.mkdir(parents=True, exist_ok=True)
    journal = chemins.DONNEES / f"invite-{cle_fournisseur}.log"
    with open(journal, "wb") as sortie:
        p = subprocess.Popen(cmd, env=e, creationflags=SANS_FENETRE,
                             stdout=sortie, stderr=sortie, stdin=subprocess.DEVNULL)
    with _verrou:
        _invites[cle_fournisseur] = p
    time.sleep(1.5)
    if p.poll() is not None:
        err = journal.read_text(encoding="utf-8", errors="replace").strip()
        raise RuntimeError(err[-300:] or "l'invité s'est arrêté")
    return {"ok": True}


def arreter_invite(cle_fournisseur):
    with _verrou:
        p = _invites.pop(cle_fournisseur, None)
    if p and p.poll() is None:
        p.terminate()
        try:
            p.wait(5)
        except subprocess.TimeoutExpired:
            p.kill()
        import serveur
        nom = FOURNISSEURS[cle_fournisseur]["nom"]
        if serveur.presence.get(nom, {}).get("etat") == "present":
            serveur.presence[nom]["etat"] = "parti"
            serveur.ajouter(nom, f"{nom} a quitté le parloir.", systeme=True)
    return {"ok": True}


# ---------- mémoire ----------

def suggestions_memoire():
    """Dossiers synchronisés repérés sur ce PC."""
    res = []

    def ajout(nom, base):
        if base and Path(base).is_dir():
            res.append({"nom": nom, "chemin": str(Path(base) / "Parloir")})

    if chemins.WINDOWS:
        for lettre in "DEFGHIJKLMNOPQRSTUVWXYZ":
            for d in ("Mon Drive", "My Drive"):
                ajout("Google Drive", f"{lettre}:\\{d}")
        ajout("OneDrive", env("OneDrive"))
        ajout("iCloud Drive", MAISON / "iCloudDrive")
    else:
        cs = MAISON / "Library" / "CloudStorage"
        if cs.is_dir():
            for d in cs.iterdir():
                if d.name.startswith("GoogleDrive"):
                    for sous in ("Mon Drive", "My Drive"):
                        ajout("Google Drive", d / sous)
                elif d.name.startswith("OneDrive"):
                    ajout("OneDrive", d)
        ajout("iCloud Drive", MAISON / "Library" / "Mobile Documents" / "com~apple~CloudDocs")
    ajout("Google Drive", MAISON / "Google Drive")
    ajout("Dropbox", MAISON / "Dropbox")
    vus, uniques = set(), []
    for s in res:
        if s["chemin"] not in vus:
            vus.add(s["chemin"])
            uniques.append(s)
    return uniques


def changer_memoire(chemin, machine=None):
    import serveur
    p = Path(os.path.expanduser(chemin.strip().strip('"')))
    if not p.parent.is_dir():
        raise RuntimeError(f"Dossier introuvable : {p.parent}")
    p.mkdir(exist_ok=True)
    modifs = {"memoire": str(p).replace("\\", "/")}
    if machine:
        modifs["machine"] = re.sub(r"[^\w.-]+", "-", machine).strip("-") or serveur.MACHINE
    chemins.ecrire_config(modifs)
    serveur.changer_memoire(p, modifs.get("machine"))
    return {"ok": True}


# ---------- mises à jour ----------

def verifier_maj(force=False):
    if not force and time.time() - _maj["verifie"] < 600 and _maj["derniere"]:
        return _maj
    _maj["erreur"] = None
    try:
        req = urllib.request.Request(f"https://api.github.com/repos/{DEPOT}/releases/latest",
                                     headers={"Accept": "application/vnd.github+json", "User-Agent": "Parloir"})
        with urllib.request.urlopen(req, timeout=15) as r:
            d = json.loads(r.read().decode("utf-8"))
        _maj["derniere"] = d.get("tag_name", "").lstrip("v")
        _maj["notes"] = (d.get("body") or "")[:1500]
        _maj["url"] = next((a["browser_download_url"] for a in d.get("assets", []) if a["name"] == "Parloir.exe"), None)
        _maj["verifie"] = time.time()
    except urllib.error.HTTPError as x:
        _maj["erreur"] = "Aucune version publiée pour l'instant." if x.code == 404 else f"GitHub répond : erreur {x.code}"
    except Exception:
        _maj["erreur"] = "Impossible de joindre GitHub : vérifie ta connexion Internet."
    _maj["disponible"] = bool(_maj["derniere"]) and version_tuple(_maj["derniere"]) > version_tuple(chemins.VERSION)
    return _maj


def _telecharger_et_relancer():
    try:
        if not (chemins.FIGE and chemins.WINDOWS):
            raise RuntimeError("Mode script : fais « git pull » dans le dossier du parloir.")
        verifier_maj(force=True)
        if not _maj["url"]:
            raise RuntimeError("Aucune version téléchargeable trouvée.")
        chemins.INSTALL.mkdir(parents=True, exist_ok=True)
        nouveau = chemins.INSTALL / "Parloir-nouveau.exe"
        req = urllib.request.Request(_maj["url"], headers={"User-Agent": "Parloir"})
        with urllib.request.urlopen(req, timeout=60) as r, open(nouveau, "wb") as f:
            total = int(r.headers.get("Content-Length") or 0)
            lu = 0
            while True:
                bloc = r.read(256 * 1024)
                if not bloc:
                    break
                f.write(bloc)
                lu += len(bloc)
                _maj["progression"] = int(lu * 100 / total) if total else 50
        # Petit script qui remplace l'exe une fois celui-ci fermé, puis relance l'app.
        cible = chemins.INSTALL / "Parloir.exe"
        script = Path(tempfile.gettempdir()) / "parloir-maj.cmd"
        script.write_text(
            "@echo off\r\n"
            "set /a n=0\r\n"
            ":boucle\r\n"
            "ping 127.0.0.1 -n 2 >nul\r\n"
            f'move /y "{nouveau}" "{cible}" >nul 2>&1 && goto fini\r\n'
            "set /a n+=1\r\n"
            "if %n% lss 30 goto boucle\r\n"
            ":fini\r\n"
            f'start "" "{cible}" --apres-maj\r\n'
            'del "%~f0"\r\n', encoding="utf-8")
        subprocess.Popen(["cmd", "/c", str(script)], creationflags=SANS_FENETRE, close_fds=True)
        _maj["progression"] = 100
        time.sleep(1.0)
        if arreter_serveur:
            threading.Thread(target=arreter_serveur, daemon=True).start()
        time.sleep(0.5)
        os._exit(0)
    except Exception as x:
        _maj["erreur"] = str(x)
    finally:
        _maj["en_cours"] = False


def au_demarrage():
    """Après une mise à jour : on remet à jour le connecteur parloir-mcp.exe installé."""
    if chemins.FIGE and chemins.WINDOWS and chemins.lire_config().get("installe"):
        try:
            copier_application()
        except OSError:
            pass   # connecteur en cours d'utilisation : on réessaiera au prochain démarrage


def arreter_tout():
    for k in list(_invites):
        arreter_invite(k)
    if arreter_serveur:
        threading.Thread(target=arreter_serveur, daemon=True).start()
    threading.Timer(0.8, lambda: os._exit(0)).start()
    return {"ok": True}


# ---------- point d'entrée HTTP ----------

def action(nom, d):
    if nom == "installer":
        if _install["en_cours"]:
            return {"ok": True}
        _install.update(en_cours=True, etapes=[])
        threading.Thread(target=_installer, args=(bool(d.get("claude", True)), bool(d.get("codex", True))),
                         daemon=True).start()
        return {"ok": True}
    if nom == "brancher":
        return {"detail": brancher_claude() if d.get("ia") == "claude" else brancher_codex()}
    if nom == "lancer":
        return lancer_agent(d.get("ia", ""))
    if nom == "inviter":
        return lancer_invite(d.get("fournisseur", ""), d.get("cle"), d.get("modele"))
    if nom == "renvoyer":
        return arreter_invite(d.get("fournisseur", ""))
    if nom == "memoire":
        return changer_memoire(d.get("chemin", ""), d.get("machine"))
    if nom == "verifier_maj":
        return verifier_maj(force=True)
    if nom == "mettre_a_jour":
        if not _maj["en_cours"]:
            _maj.update(en_cours=True, erreur=None, progression=0)
            threading.Thread(target=_telecharger_et_relancer, daemon=True).start()
        return {"ok": True}
    if nom == "quitter":
        return arreter_tout()
    raise RuntimeError(f"action inconnue : {nom}")
