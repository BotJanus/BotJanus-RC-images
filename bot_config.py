#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
bot_config — identité, variables d'environnement et logging, définis UNE fois.

  * BOT_NAME / BOT_DESCRIPTION : le nom et la description affichés partout
    (titre des logs Discord, embeds, dashboard, README, services.json de l'agent).
    Pour les changer : ici ET dans BotJanus-agent/services.json (label/description).
  * load_env() : charge le fichier d'environnement (un seul chargeur pour les 3 scripts
    et discord_logger). Les variables déjà présentes dans l'environnement gagnent.
  * setup_logging() : configuration du logging, identique pour main.py et les modes autonomes.
"""

import logging
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))

BOT_NAME = "BotJanus RC-images"
BOT_DESCRIPTION = "Surveillance des téléversements : licences, nettoyage des images supprimées et analyse IA"
BOT_TITLE = "📡 **%s** — %s" % (BOT_NAME, BOT_DESCRIPTION)     # en-tête des messages de log Discord
SERVICE_ID = os.environ.get("BOTJANUS_SERVICE_ID", "rc-images")

LOG_FORMAT = "%(asctime)s [%(levelname)s] %(message)s"

_ENV_CANDIDATES = (
    "$DISCORD_ENV_FILE",
    os.path.join(HERE, "env", "rc-images.env"),
    os.path.join(HERE, "env", "agent.env"),
    os.path.join(HERE, ".env"),
    ".env",
    os.path.join("env", "agent.env"),
)
_loaded = []


def load_env():
    """Charge tous les fichiers d'environnement trouvés (sans écraser l'existant).
    Idempotent. Renvoie la liste des fichiers lus."""
    if _loaded:
        return list(_loaded)
    for path in _ENV_CANDIDATES:
        if path == "$DISCORD_ENV_FILE":
            path = os.environ.get("DISCORD_ENV_FILE")
        if not path or not os.path.isfile(path) or os.path.abspath(path) in _loaded:
            continue
        try:
            with open(path, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("#") or "=" not in line:
                        continue
                    key, value = line.split("=", 1)
                    os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))
            _loaded.append(os.path.abspath(path))
        except OSError:
            continue
    return list(_loaded)


def env_str(name, default=""):
    return os.environ.get(name, default).strip()


def env_float(name, default):
    try:
        return float(os.environ.get(name, default))
    except ValueError:
        return float(default)


def env_bool(name, default=False):
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in ("1", "true", "oui", "yes", "on")


def setup_logging():
    """Configure le logging racine une seule fois (sortie standard, format commun)."""
    if not logging.getLogger().handlers:
        logging.basicConfig(level=logging.INFO, format=LOG_FORMAT, stream=sys.stdout)


load_env()
