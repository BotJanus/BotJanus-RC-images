#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Launcher — lance BotJanus (license_bot.py), le bot de nettoyage d'images
supprimées (image_cleanup_bot.py) et le détecteur d'images IA (ai_detector.py)
en parallèle dans des threads distincts.

Usage :
    python main.py
    (Ctrl+C pour arrêter l'ensemble)
"""

import signal
import sys
import time
import logging
import threading

import pywikibot
import license_bot
import image_cleanup_bot
import ai_detector

# Initialisation de la session Pywikibot globale
site = pywikibot.Site("fr", "vikidia")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("launcher")

# Envoie chaque log vers le webhook Discord (DISCORD_BOT_TOKEN + DISCORD_LOG_CHANNEL_ID, voir env/agent.env).
import discord_logger
discord_logger.install()


def run_bot(name, entry_point):
    """Encapsule main() de chaque bot pour logger proprement les erreurs sans
    arrêter les autres threads."""
    log.info("Démarrage du thread : %s", name)
    try:
        entry_point()
    except Exception:
        log.exception("Le bot '%s' s'est arrêté suite à une exception non gérée.", name)


def main():
    discord_logger.announce_started()   # embed « démarré / redémarré » : posté AVANT les logs
    threads = [
        threading.Thread(
            target=run_bot,
            args=("license_bot", license_bot.main),
            name="license_bot",
            daemon=True,
        ),
        threading.Thread(
            target=run_bot,
            args=("image_cleanup_bot", image_cleanup_bot.main),
            name="image_cleanup_bot",
            daemon=True,
        ),
        threading.Thread(
            target=run_bot,
            args=("ai_detector", ai_detector.main),
            name="ai_detector",
            daemon=True,
        ),
    ]

    for t in threads:
        t.start()
        time.sleep(2)  # Décalage pour éviter des requêtes simultanées lors du login

    log.info("Les 3 bots tournent en parallèle. Appuie sur Ctrl+C pour tout arrêter.")

    def _on_sigterm(signum, frame):     # arrêt demandé par l'agent
        discord_logger.announce_stopping()   # embed « arrêté » : posté AVANT les logs d'arrêt
        log.info("Arrêt demandé (SIGTERM). Fin du launcher.")
        logging.shutdown()                   # vide la file Discord avant de quitter
        sys.exit(0)

    signal.signal(signal.SIGTERM, _on_sigterm)

    try:
        while True:
            time.sleep (3600)
    except KeyboardInterrupt:
        log.info("Arrêt demandé (Ctrl+C). Fin du launcher.")
        sys.exit(0)


if __name__ == "__main__":
    main()
