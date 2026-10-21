#!/usr/bin/env python3
"""Installe Le Parloir dans Claude Code et dans Codex, sans rien taper.

Sous Windows : double-clique sur « installer.bat ».
Ailleurs : python3 installer.py
"""
import json
import shutil
import subprocess
import sys
from pathlib import Path

ICI = Path(__file__).resolve().parent
PONT = ICI / "mcp_parloir.py"
PYTHON = sys.executable
MAISON = Path.home()


def titre(t):
    print(f"\n=== {t} ===")


def claude():
    titre("Claude Code")
    exe = shutil.which("claude")
    if not exe:
        print("  Claude Code n'est pas installé sur ce PC : étape sautée.")
        return
    subprocess.run([exe, "mcp", "remove", "parloir", "--scope", "user"],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    r = subprocess.run([exe, "mcp", "add", "--scope", "user", "parloir", "--",
                        PYTHON, str(PONT), "--nom", "Claude"], capture_output=True, text=True)
    if r.returncode != 0:
        print("  Échec du branchement :", (r.stderr or r.stdout).strip())
        return
    print("  Connecteur branché.")

    # Autoriser les outils du parloir pour éviter une demande de permission à chaque message.
    reglages = MAISON / ".claude" / "settings.json"
    try:
        d = json.loads(reglages.read_text(encoding="utf-8")) if reglages.exists() else {}
    except json.JSONDecodeError:
        print(f"  (Je n'ai pas touché à {reglages} : fichier illisible.)")
        return
    permis = d.setdefault("permissions", {}).setdefault("allow", [])
    if "mcp__parloir" not in permis:
        permis.append("mcp__parloir")
        reglages.parent.mkdir(parents=True, exist_ok=True)
        if reglages.exists():
            shutil.copy(reglages, reglages.with_name("settings.json.avant-parloir"))
        reglages.write_text(json.dumps(d, indent=2, ensure_ascii=False), encoding="utf-8")
    print("  Outils du parloir autorisés.")


def codex():
    titre("Codex")
    dossier = MAISON / ".codex"
    if not shutil.which("codex") and not dossier.exists():
        print("  Codex n'est pas installé sur ce PC : étape sautée.")
        return
    conf = dossier / "config.toml"
    texte = conf.read_text(encoding="utf-8") if conf.exists() else ""
    if "[mcp_servers.parloir]" in texte:
        print("  Déjà branché.")
        return
    bloc = ("\n[mcp_servers.parloir]\n"
            f"command = '{PYTHON}'\n"
            f"args = ['{PONT}', '--nom', 'Codex']\n"
            "tool_timeout_sec = 120\n")
    dossier.mkdir(parents=True, exist_ok=True)
    if conf.exists():
        shutil.copy(conf, conf.with_name("config.toml.avant-parloir"))
    with conf.open("a", encoding="utf-8") as f:
        f.write(bloc)
    print("  Connecteur branché.")


def main():
    print("Installation du Parloir")
    print(f"  Dossier : {ICI}")
    claude()
    codex()
    titre("C'est fini")
    print("  1. Double-clique sur « ouvrir-parloir.bat » (ou lance : python serveur.py).")
    print("  2. Dans Claude Code et dans Codex, dis : « Connecte-toi au parloir ».")
    print("  Si Claude Code ou Codex étaient déjà ouverts, ferme-les et rouvre-les d'abord.")


if __name__ == "__main__":
    main()
