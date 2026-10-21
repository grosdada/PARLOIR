"""Où se trouvent les fichiers du Parloir, en mode script (git clone) ou en mode application (Parloir.exe)."""
import json
import os
import sys
from pathlib import Path

FIGE = getattr(sys, "frozen", False)          # True quand on tourne depuis Parloir.exe
WINDOWS = os.name == "nt"
ICI = Path(__file__).resolve().parent

# Fichiers embarqués (index.html, accueil.html, parloir-mcp.exe…)
RESSOURCES = Path(getattr(sys, "_MEIPASS", ICI))

if FIGE and WINDOWS:
    # Données de l'utilisateur : %APPDATA%\Parloir ; programme installé : %LOCALAPPDATA%\Programs\Parloir
    DONNEES = Path(os.environ.get("APPDATA", Path.home())) / "Parloir"
    INSTALL = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "Programs" / "Parloir"
else:
    DONNEES = Path(os.environ.get("PARLOIR_DONNEES", ICI))
    INSTALL = ICI

CONFIG = DONNEES / "config.json"

try:   # écrit par la fabrication de l'exe (GitHub Actions) à partir du numéro de version
    VERSION = (RESSOURCES / "version.txt").read_text(encoding="utf-8").strip().lstrip("v")
except OSError:
    VERSION = "0.0.0-dev"


def lire_config():
    try:
        return json.loads(CONFIG.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def ecrire_config(modifs):
    cfg = lire_config()
    cfg.update(modifs)
    CONFIG.parent.mkdir(parents=True, exist_ok=True)
    CONFIG.write_text(json.dumps(cfg, indent=2, ensure_ascii=False), encoding="utf-8")
    return cfg


def port():
    return int(os.environ.get("PARLOIR_PORT") or lire_config().get("port") or 7777)
