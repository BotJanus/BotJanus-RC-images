#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
bot_state — état partagé entre les 3 scripts (license_bot, image_cleanup_bot, ai_detector).

Un seul fichier (bot_state.txt, JSON) contient deux heures, toutes lues via bot_clock :

  "since"     début de la prochaine fenêtre de lecture des journaux. Au démarrage, on
              reprend depuis cette heure : tout ce qui s'est passé pendant que le bot était
              éteint (téléversements, suppressions) est rattrapé au premier cycle. Sans
              fichier (premier lancement), on part de « maintenant ».
  "heartbeat" « je suis vivant » : réécrit toutes les 30 s par un thread dédié tant que le
              processus tourne (et qu'aucun cycle n'est bloqué). C'est CE champ que l'agent
              lit pour le dashboard.

Pourquoi deux champs : auparavant l'agent lisait « since » comme heartbeat. Or « since » vaut
début du cycle − 60 s et n'est écrit qu'une fois toutes les 10 min, et seulement si les 3
scripts ont réussi : son âge dépassait donc en permanence le seuil de 600 s → le service
apparaissait « Dégradé » alors qu'il fonctionnait.

Ancien format (une simple ligne ISO) : toujours lu, converti au premier enregistrement.
"""

import json
import logging
import os
import threading

import bot_clock
import bot_config

log = logging.getLogger("bot_state")

STATE_FILE = os.path.join(bot_config.DATA_DIR, "bot_state.txt")

POLL_INTERVAL = 600          # secondes : un cycle commun aux 3 scripts toutes les 10 minutes
OVERLAP_MARGIN = 60          # secondes de chevauchement entre deux fenêtres (doublons filtrés par logid)
HEARTBEAT_INTERVAL = 30      # secondes entre deux battements
MAX_CYCLE_SECONDS = 1800     # un cycle plus long est considéré bloqué : on cesse de battre

_lock = threading.RLock()
_state = None                # cache mémoire du fichier
_cycle_started = None        # bot_clock.monotonic() du cycle en cours, None si au repos
_beat_thread = None


# --------------------------------------------------------------------------
# Lecture / écriture du fichier
# --------------------------------------------------------------------------

def _read_file():
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            raw = f.read().strip()
    except (FileNotFoundError, OSError):
        return {}
    if not raw:
        return {}
    if raw.startswith("{"):
        try:
            data = json.loads(raw)
            return data if isinstance(data, dict) else {}
        except ValueError:
            return {}
    return {"since": raw}                    # ancien format : une ligne ISO


def _load():
    global _state
    if _state is None:
        _state = _read_file()
    return _state


def _write():
    """Écriture atomique (fichier temporaire + remplacement). Lève OSError."""
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(_state, f, ensure_ascii=False)
    os.replace(tmp, STATE_FILE)


def check_writable():
    """True si le fichier d'état peut être écrit (utilisé par le contrôle de configuration)."""
    with _lock:
        try:
            _load()
            _write()
            return True
        except OSError:
            return False


# --------------------------------------------------------------------------
# Fenêtre de lecture des journaux
# --------------------------------------------------------------------------

def load_last_check():
    """Début de fenêtre enregistré (datetime UTC conscient), ou None au tout premier démarrage."""
    with _lock:
        return bot_clock.parse(_load().get("since"))


def update_last_check(ts=None):
    """Enregistre le début de la prochaine fenêtre (par défaut : maintenant) et bat le cœur.
    Une erreur d'écriture est loguée mais n'arrête jamais le bot."""
    ts = ts or bot_clock.now()
    with _lock:
        state = _load()
        state["since"] = bot_clock.iso(ts)
        state["heartbeat"] = bot_clock.iso()
        try:
            _write()
        except OSError as e:
            log.warning("Impossible d'écrire l'état partagé : %s", e)
    return ts


def next_since(cycle_start):
    """Borne de début de la prochaine fenêtre : début du cycle − marge de chevauchement."""
    return bot_clock.minus(cycle_start, OVERLAP_MARGIN)


# --------------------------------------------------------------------------
# Heartbeat
# --------------------------------------------------------------------------

def beat():
    """Écrit « je suis vivant » maintenant. Ne lève jamais."""
    with _lock:
        state = _load()
        state["heartbeat"] = bot_clock.iso()
        try:
            _write()
        except OSError as e:
            log.debug("Heartbeat non écrit : %s", e)


def cycle_started():
    global _cycle_started
    _cycle_started = bot_clock.monotonic()
    beat()


def cycle_finished():
    global _cycle_started
    _cycle_started = None
    beat()


def _cycle_is_stuck():
    started = _cycle_started
    return started is not None and bot_clock.monotonic() - started > MAX_CYCLE_SECONDS


def _beat_loop():
    while True:
        bot_clock.sleep(HEARTBEAT_INTERVAL)
        if _cycle_is_stuck():                # cycle bloqué : on se tait, le dashboard passera « Dégradé »
            continue
        beat()


def start_heartbeat():
    """Démarre (une seule fois) le thread qui bat toutes les HEARTBEAT_INTERVAL secondes."""
    global _beat_thread
    with _lock:
        if _beat_thread is not None and _beat_thread.is_alive():
            return
        beat()
        _beat_thread = threading.Thread(target=_beat_loop, name="bot_heartbeat", daemon=True)
        _beat_thread.start()
