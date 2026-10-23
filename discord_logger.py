#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
discord_logger — envoie chaque nouveau log vers un webhook Discord.

Rendu : un titre en gras (« 📡 **BotJanus RC (...)** ») suivi d'un bloc de code
contenant les lignes de log, comme sur la capture d'écran.

Les logs sont regroupés par lots (quelques secondes) pour ne pas se faire
bloquer par le rate-limit de Discord, et envoyés depuis un thread dédié :
le bot n'est jamais ralenti, même si Discord est lent ou injoignable.

Utilisation (dans main.py, après logging.basicConfig) :

    import discord_logger
    discord_logger.install()          # lit DISCORD_WEBHOOK_URL dans l'env

Variables d'environnement :
    DISCORD_WEBHOOK_URL   URL du webhook (obligatoire, sinon rien n'est fait)
    DISCORD_LOG_LEVEL     niveau minimum envoyé (défaut : INFO)
    DISCORD_LOG_TITLE     titre affiché au-dessus de chaque message
    DISCORD_LOG_USERNAME  nom d'affichage du webhook (optionnel)
    DISCORD_LOG_BATCH     secondes d'attente pour regrouper les logs (défaut : 2)
"""

import atexit
import logging
import os
import queue
import threading
import time

import requests

DEFAULT_TITLE = "📡 **BotJanus RC Images (licences + nettoyage)**"
DISCORD_MAX = 2000          # limite de caractères d'un message Discord
FENCE_OVERHEAD = 8          # "```\n" + "\n```"
_STOP = object()


class DiscordWebhookHandler(logging.Handler):
    def __init__(self, url, title=DEFAULT_TITLE, username=None,
                 batch_seconds=2.0, level=logging.INFO):
        super().__init__(level)
        self.url = url
        self.title = title
        self.username = username
        self.batch_seconds = batch_seconds
        self._queue = queue.Queue(maxsize=5000)
        self._session = requests.Session()
        self._dropped = 0
        self._thread = threading.Thread(
            target=self._worker, name="discord_logger", daemon=True)
        self._thread.start()
        atexit.register(self.close)

    # --- côté logging : ne fait que mettre en file, jamais de réseau ------
    def emit(self, record):
        # Ne jamais renvoyer vers Discord les logs de la couche réseau
        # (évite toute boucle infinie).
        if record.name.startswith(("urllib3", "requests", "discord_logger")):
            return
        try:
            self._queue.put_nowait(self.format(record))
        except queue.Full:
            self._dropped += 1
        except Exception:
            self.handleError(record)

    def close(self):
        if self._thread.is_alive():
            self._queue.put(_STOP)
            self._thread.join(timeout=10)
        super().close()

    # --- thread d'envoi ---------------------------------------------------
    def _worker(self):
        stop = False
        while not stop:
            item = self._queue.get()
            if item is _STOP:
                break
            lines = [item]
            deadline = time.monotonic() + self.batch_seconds
            while True:  # regroupe les logs arrivant dans la fenêtre
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                try:
                    nxt = self._queue.get(timeout=remaining)
                except queue.Empty:
                    break
                if nxt is _STOP:
                    stop = True
                    break
                lines.append(nxt)
            if self._dropped:
                lines.append(f"[discord_logger] {self._dropped} log(s) ignoré(s) (file pleine)")
                self._dropped = 0
            for message in self._build_messages(lines):
                self._post(message)

    def _build_messages(self, lines):
        """Découpe en messages <= 2000 caractères : titre + bloc de code."""
        room = DISCORD_MAX - len(self.title) - 1 - FENCE_OVERHEAD
        chunks, current, size = [], [], 0
        for line in lines:
            line = line.replace("```", "'''")
            while len(line) > room:               # ligne géante : on coupe
                head, line = line[:room], line[room:]
                if current:
                    chunks.append(current)
                    current, size = [], 0
                chunks.append([head])
            extra = len(line) + (1 if current else 0)
            if size + extra > room:
                chunks.append(current)
                current, size, extra = [], 0, len(line)
            current.append(line)
            size += extra
        if current:
            chunks.append(current)
        return [f"{self.title}\n```\n" + "\n".join(c) + "\n```" for c in chunks]

    def _post(self, content):
        payload = {"content": content, "allowed_mentions": {"parse": []}}
        if self.username:
            payload["username"] = self.username
        for _ in range(5):
            try:
                r = self._session.post(self.url, json=payload, timeout=10)
            except requests.RequestException:
                time.sleep(5)
                continue
            if r.status_code == 429:               # rate-limit : on attend
                try:
                    wait = float(r.json().get("retry_after", 1))
                except Exception:
                    wait = float(r.headers.get("Retry-After", 1))
                time.sleep(min(wait, 30) + 0.1)
                continue
            if r.status_code >= 500:
                time.sleep(3)
                continue
            return                                  # succès ou erreur 4xx définitive
        # abandon silencieux : on ne logge pas ici pour éviter une boucle


def install(url=None, logger=None):
    """Branche le handler sur le logger racine (tous les bots) et le renvoie.
    Ne fait rien si aucune URL de webhook n'est configurée."""
    url = url or os.environ.get("DISCORD_WEBHOOK_URL", "").strip()
    if not url:
        logging.getLogger("discord_logger").warning(
            "DISCORD_WEBHOOK_URL absent : logs Discord désactivés.")
        return None
    level = getattr(logging, os.environ.get("DISCORD_LOG_LEVEL", "INFO").upper(), logging.INFO)
    handler = DiscordWebhookHandler(
        url,
        title=os.environ.get("DISCORD_LOG_TITLE", DEFAULT_TITLE),
        username=os.environ.get("DISCORD_LOG_USERNAME") or None,
        batch_seconds=float(os.environ.get("DISCORD_LOG_BATCH", "2")),
        level=level,
    )
    handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
    (logger or logging.getLogger()).addHandler(handler)
    return handler