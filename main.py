#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Launcher — BotJanus RC-images.

Surveillance des téléversements : licences, nettoyage des images supprimées et analyse IA.
(Nom et description : bot_config.BOT_NAME / BOT_DESCRIPTION.)

Lance les 3 scripts — license_bot.py, image_cleanup_bot.py, ai_detector.py — dans UN SEUL
cycle commun, toutes les 10 minutes, à la suite, avec une heure et un état partagés
(bot_clock.py, bot_state.py, boucle dans cycle.py).

Usage :
    python main.py           lance le bot (Ctrl+C pour arrêter l'ensemble)
    python main.py --check   contrôle de configuration seul (logs + embed Discord), puis quitte
"""

import argparse
import logging
import signal
import sys

import pywikibot

import ai_detector
import bot_config
import config_check
import cycle
import discord_logger
import image_cleanup_bot
import license_bot

bot_config.setup_logging()
log = logging.getLogger("launcher")

# Envoie chaque log vers Discord (DISCORD_BOT_TOKEN + DISCORD_LOG_CHANNEL_ID, ou webhook).
discord_logger.install()


def main():
    parser = argparse.ArgumentParser(description=bot_config.BOT_DESCRIPTION)
    parser.add_argument("--check", action="store_true",
                        help="contrôle de configuration seul, sans lancer le bot")
    args = parser.parse_args()

    if not args.check:
        discord_logger.announce_started()   # embed « démarré / redémarré » : posté AVANT les logs

    site = pywikibot.Site("fr", "vikidia")
    site.login()

    # Message de configuration (logs + embed) : tout est-il chargé et utilisable ?
    report = config_check.run(site)
    if args.check:
        logging.shutdown()               # vide la file Discord avant de quitter
        sys.exit(0 if report.status != config_check.ERR else 1)

    def _on_sigterm(signum, frame):     # arrêt demandé par l'agent
        discord_logger.announce_stopping()   # embed « arrêté » : posté AVANT les logs d'arrêt
        log.info("Arrêt demandé (SIGTERM). Fin du launcher.")
        logging.shutdown()                   # vide la file Discord avant de quitter
        sys.exit(0)

    signal.signal(signal.SIGTERM, _on_sigterm)

    steps = [
        ("license_bot", license_bot.run_cycle),
        ("image_cleanup_bot", image_cleanup_bot.run_cycle),
        ("ai_detector", ai_detector.run_cycle),
    ]
    try:
        cycle.run_loop(site, steps)
    except KeyboardInterrupt:
        log.info("Arrêt demandé (Ctrl+C). Fin du launcher.")
        sys.exit(0)


if __name__ == "__main__":
    main()
