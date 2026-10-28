#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
État partagé entre les 3 bots (license_bot, image_cleanup_bot, ai_detector).

Les 3 scripts ne forment en réalité qu'un seul "bot" (lancés ensemble par
main.py). Ils partagent donc un seul fichier contenant l'heure du dernier
passage : à chaque cycle de sondage réussi, n'importe lequel des 3 met à jour
cette heure.

Au démarrage :
  - si le fichier existe, on reprend depuis cette heure -> tout ce qui s'est
    passé pendant que le bot était éteint (nouveaux fichiers, suppressions)
    est rattrapé automatiquement au premier cycle ;
  - sinon (premier lancement), on part de "maintenant" et on scanne en direct
    normalement, comme avant.

Le fichier est protégé par un verrou (threading.Lock) car les 3 bots tournent
en threads dans le même processus Python et peuvent lire/écrire en même temps.
"""

import threading
import pywikibot

STATE_FILE = "bot_state.txt"

# Cycle commun aux 3 bots : un seul passage toutes les 10 minutes.
POLL_INTERVAL = 600          # secondes
OVERLAP_MARGIN = 60          # secondes de chevauchement entre deux fenêtres (les doublons sont filtrés par logid)

_lock = threading.Lock()


def load_last_check():
    """Renvoie le pywikibot.Timestamp du dernier passage partagé, ou None si
    le fichier n'existe pas encore (tout premier démarrage du bot)."""
    with _lock:
        try:
            with open(STATE_FILE, "r") as f:
                ts_str = f.read().strip()
            return pywikibot.Timestamp.fromISOformat(ts_str)
        except (FileNotFoundError, ValueError):
            return None


def update_last_check(ts=None):
    """Met à jour l'heure du dernier passage partagé (par défaut : maintenant).
    À appeler à la fin de chaque cycle de sondage réussi, par n'importe lequel
    des 3 bots. N'échoue jamais bruyamment : une erreur d'écriture est juste
    loguée par l'appelant si besoin, elle n'arrête pas le bot."""
    if ts is None:
        ts = pywikibot.Timestamp.now()
    with _lock:
        with open(STATE_FILE, "w") as f:
            f.write(ts.isoformat())
    return ts


def next_since(cycle_start):
    """Borne de début de la prochaine fenêtre : début du cycle - marge."""
    from datetime import timedelta
    return cycle_start - timedelta(seconds=OVERLAP_MARGIN)