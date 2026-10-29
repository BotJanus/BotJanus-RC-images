#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
cycle — la boucle commune aux 3 scripts (main.py ET les modes autonomes).

Avant, la boucle « sonder / avancer la fenêtre / dormir » existait en 4 exemplaires
(main.py + un par script). Elle n'existe plus qu'ici : chaque script expose
run_cycle(site, since) -> bool et ne s'occupe que de son métier.
"""

import logging

import bot_clock
import bot_state

log = logging.getLogger("cycle")

# Un seul appel API pour le journal des téléversements, partagé par license_bot et ai_detector.
_upload_cache = {"key": None, "at": 0.0, "events": []}
_UPLOAD_CACHE_TTL = 120      # secondes


def upload_events(site, since):
    """Événements du journal des téléversements depuis `since`, du plus ancien au plus récent.
    Les deux scripts qui lisent ce journal dans le même cycle ne font qu'UN appel API."""
    key = bot_clock.iso(since)
    if _upload_cache["key"] == key and bot_clock.monotonic() - _upload_cache["at"] < _UPLOAD_CACHE_TTL:
        return list(_upload_cache["events"])
    events = list(site.logevents(logtype="upload", start=since, reverse=True))
    _upload_cache.update(key=key, at=bot_clock.monotonic(), events=events)
    return list(events)


def run_step(name, fn):
    """Exécute une étape ; une erreur n'arrête ni les autres étapes ni la boucle.
    Renvoie True si l'étape a réussi."""
    try:
        return bool(fn())
    except Exception:
        log.exception("L'étape '%s' a échoué (le cycle continue).", name)
        return False


def run_loop(site, steps):
    """Boucle infinie. `steps` : liste de (nom, fonction(site, since) -> bool).

    La fenêtre n'avance que si TOUTES les étapes ont pu lire leur journal : sinon on
    réessaie la même fenêtre au cycle suivant (les doublons sont filtrés par logid)."""
    bot_state.start_heartbeat()
    since = bot_state.load_last_check() or bot_clock.now()
    log.info("Cycle commun toutes les %d s, première fenêtre depuis %s.",
             bot_state.POLL_INTERVAL, bot_clock.iso(since))

    while True:
        start = bot_clock.now()
        bot_state.cycle_started()
        results = [run_step(name, lambda fn=fn: fn(site, since)) for name, fn in steps]
        bot_state.cycle_finished()

        if all(results):
            since = bot_state.next_since(start)
            bot_state.update_last_check(since)
        else:
            log.warning("Cycle incomplet %s : fenêtre conservée.",
                        dict(zip((n for n, _ in steps), results)))

        # Un cycle toutes les 10 minutes, durée d'exécution comprise.
        bot_clock.sleep(max(1, bot_state.POLL_INTERVAL - bot_clock.elapsed(start)))
