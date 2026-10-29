#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
bot_clock — LA source unique de l'heure pour tout BotJanus-RC-images.

Aucun autre fichier n'appelle datetime.now / time.time / time.sleep /
pywikibot.Timestamp.utcnow : tout passe par ici. Ça garantit :
  * une seule convention : datetimes *conscients* (UTC) partout. Avant, on
    mélangeait des Timestamp naïfs (utcnow, fromISOformat) et des datetimes
    conscients, ce qui fait planter les soustractions ;
  * un seul format texte (ISO 8601, « 2026-10-29T11:08:00Z ») pour le fichier
    d'état, les embeds Discord et le heartbeat lu par l'agent ;
  * un seul endroit à modifier (ou à simuler dans un test) pour changer le
    comportement du temps.

now() renvoie un pywikibot.Timestamp (sous-classe de datetime) quand pywikibot
est installé, pour pouvoir le passer tel quel à site.logevents(start=...).
"""

import time as _time
from datetime import datetime, timedelta, timezone

try:                                    # le mode « test local » d'ai_detector n'a pas besoin de pywikibot
    from pywikibot import Timestamp as _Timestamp
except ImportError:                     # pragma: no cover
    _Timestamp = None

UTC = timezone.utc
ISO_FORMAT = "%Y-%m-%dT%H:%M:%SZ"


def _wrap(dt):
    """datetime conscient -> pywikibot.Timestamp conscient (UTC) si disponible."""
    dt = dt.astimezone(UTC)
    if _Timestamp is None:
        return dt
    return _Timestamp.fromtimestamp(dt.timestamp(), tz=UTC)


def now():
    """Heure actuelle, UTC, consciente."""
    return _wrap(datetime.now(UTC))


def epoch():
    """Secondes depuis l'epoch (pour les durées « mur » : « traité en 3 s », etc.)."""
    return _time.time()


def monotonic():
    """Horloge monotone : pour les délais et les intervalles, insensible aux changements d'heure."""
    return _time.monotonic()


def minus(dt, seconds):
    """dt - N secondes (même type que dt)."""
    return dt - timedelta(seconds=seconds)


def elapsed(since):
    """Secondes écoulées depuis `since` (datetime conscient ou naïf considéré UTC)."""
    return (now() - ensure_utc(since)).total_seconds()


def ensure_utc(dt):
    """Force un datetime en UTC conscient (un datetime naïf est supposé UTC)."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return _wrap(dt)


def iso(dt=None):
    """Format texte unique : « 2026-10-29T11:08:00Z » (maintenant par défaut)."""
    dt = now() if dt is None else ensure_utc(dt)
    return dt.strftime(ISO_FORMAT)


def parse(text):
    """Texte ISO 8601 (avec ou sans « Z » / décalage) -> datetime conscient UTC,
    ou None si le texte est vide / illisible."""
    if not text:
        return None
    text = str(text).strip().replace("Z", "+00:00")
    try:
        return ensure_utc(datetime.fromisoformat(text))
    except ValueError:
        return None


def sleep(seconds, tick=None, tick_every=30.0):
    """Dort `seconds` secondes. Si `tick` est fourni, l'appelle toutes les `tick_every`
    secondes pendant l'attente (heartbeat, vérification d'arrêt...)."""
    end = monotonic() + max(0.0, seconds)
    while True:
        remaining = end - monotonic()
        if remaining <= 0:
            return
        _time.sleep(min(remaining, tick_every) if tick else remaining)
        if tick and end - monotonic() > 0:
            tick()
