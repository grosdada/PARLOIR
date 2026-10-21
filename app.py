"""Parloir.exe — l'application.

    Parloir.exe                 démarre le parloir (si besoin) et ouvre la page d'accueil
    Parloir.exe --serveur       démarre le parloir sans ouvrir le navigateur (utilisé par les IA)
    Parloir.exe --invite grok   fait entrer un invité par API (utilisé par l'app elle-même)
"""
import sys
import threading
import urllib.request
import webbrowser

import chemins


def deja_lance():
    try:
        ouvreur = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        ouvreur.open(f"http://127.0.0.1:{chemins.port()}/api/presence", timeout=2)
        return True
    except Exception:
        return False


def main():
    args = sys.argv[1:]
    if args[:1] == ["--invite"]:
        sys.argv = ["invite", *args[1:]]
        import invite
        invite.main()
        return

    adresse = f"http://127.0.0.1:{chemins.port()}/accueil"
    avec_navigateur = "--serveur" not in args
    if deja_lance():
        if avec_navigateur:
            webbrowser.open(adresse)
        return

    import serveur
    import systeme
    systeme.au_demarrage()
    if avec_navigateur:
        threading.Timer(0.8, webbrowser.open, (adresse,)).start()
    serveur.main()


if __name__ == "__main__":
    main()
