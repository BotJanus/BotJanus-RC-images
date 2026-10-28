#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Launcher — lance BotJanus (license_bot.py), le bot de nettoyage d'images
supprimées (image_cleanup_bot.py) et le détecteur d'images IA (ai_detector.py)
dans UN SEUL cycle commun, toutes les 10 minutes (600 s),
à la suite, avec un horodatage partagé (bot_state.txt).

Usage :
    python main.py
    (Ctrl+C pour arrêter l'ensemble)
"""

import signal
import sys
import time
import logging

import pywikibot
import license_bot
import image_cleanup_bot
import ai_detector
import bot_state

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


def run_step(name, fn):
    """Exécute une étape du cycle ; une erreur n'arrête ni les autres étapes ni la boucle.
    Renvoie True si l'étape a réussi."""
    try:
        return bool(fn())
    except Exception:
        log.exception("L'étape '%s' a échoué (le cycle continue).", name)
        return False


def main():
    discord_logger.announce_started()   # embed « démarré / redémarré » : posté AVANT les logs

    site.login()
    known_licenses = license_bot.get_known_license_templates(site)
    log.info("%d modèle(s) de licence reconnu(s).", len(known_licenses))

    def _on_sigterm(signum, frame):     # arrêt demandé par l'agent
        discord_logger.announce_stopping()   # embed « arrêté » : posté AVANT les logs d'arrêt
        log.info("Arrêt demandé (SIGTERM). Fin du launcher.")
        logging.shutdown()                   # vide la file Discord avant de quitter
        sys.exit(0)

    signal.signal(signal.SIGTERM, _on_sigterm)

    # Reprise depuis le dernier passage partagé, sinon "maintenant".
    since = bot_state.load_last_check() or pywikibot.Timestamp.now()
    log.info("Cycle commun toutes les %d s, première fenêtre depuis %s UTC.",
             bot_state.POLL_INTERVAL, since.isoformat())

    try:
        while True:
            start = pywikibot.Timestamp.now()

            steps = [
                ("license_bot", lambda: license_bot.run_cycle(site, since, known_licenses)),
                ("image_cleanup_bot", lambda: image_cleanup_bot.run_cycle(site, since)),
                ("ai_detector", lambda: ai_detector.run_cycle(site, since)),
            ]
            results = [run_step(name, fn) for name, fn in steps]

            # La fenêtre n'avance que si les 3 scripts ont pu lire leur journal :
            # sinon on réessaie la même fenêtre au prochain cycle (doublons filtrés).
            if all(results):
                since = bot_state.next_since(start)
                try:
                    bot_state.update_last_check(since)
                except OSError as e:
                    log.warning("Impossible d'écrire l'état partagé : %s", e)
            else:
                log.warning("Cycle incomplet %s : fenêtre conservée.", results)

            # Un cycle toutes les 10 minutes, durée d'exécution comprise.
            elapsed = (pywikibot.Timestamp.now() - start).total_seconds()
            time.sleep(max(1, bot_state.POLL_INTERVAL - elapsed))
    except KeyboardInterrupt:
        log.info("Arrêt demandé (Ctrl+C). Fin du launcher.")
        sys.exit(0)


if __name__ == "__main__":
    main()