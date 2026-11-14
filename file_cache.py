#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
file_cache — mémoire des images déjà analysées (Vikidia ET Common).

Quand le bot repasse sur un fichier déjà analysé, il ne relance pas l'analyse (OpenAI /
SightEngine = quota payant) : il réutilise le verdict et applique simplement ce qu'il faut
(étiquette, révocation, demande de masquage...).

Un verdict n'est réutilisé que si le fichier est LE MÊME : même nom, même SHA-1 et même date de
dernière version. Fichier supprimé puis re-téléversé, ou nouvelle version → autre date / autre
SHA-1 → nouvelle analyse.

Fichier texte simple, une ligne par fichier, colonnes séparées par des tabulations :

    dépôt <TAB> nom <TAB> sha1 <TAB> date de la version <TAB> date d'analyse <TAB> verdict (JSON)

Il est écrit dans le dossier de données (/data/rc-images, ou BOTJANUS_DATA_DIR) : JAMAIS dans le
dossier du code (/app). Écriture atomique, borné à MAX_ENTRIES lignes et TTL_DAYS jours.
"""

import json
import logging
import os
import threading

import bot_clock
import bot_config

log = logging.getLogger("file-cache")

CACHE_FILE = os.path.join(bot_config.DATA_DIR, "analysed_files.txt")
MAX_ENTRIES = 5000
TTL_DAYS = 30

_lock = threading.RLock()
_data = None          # {(dépôt, nom): {"sha1", "ts", "at", "verdict"}}


def norm_name(name):
    name = " ".join(str(name).replace("_", " ").split())
    return name[:1].upper() + name[1:]


def is_in_code_dir():
    """True si le cache est tombé dans le dossier du code (dossier de données inaccessible)."""
    return os.path.dirname(os.path.abspath(CACHE_FILE)) == bot_config.HERE


def _load():
    global _data
    if _data is None:
        _data = {}
        try:
            with open(CACHE_FILE, "r", encoding="utf-8") as f:
                for line in f:
                    parts = line.rstrip("\n").split("\t", 5)
                    if len(parts) != 6:
                        continue
                    repo, name, sha1, ts, at, raw = parts
                    try:
                        _data[(repo, name)] = {"sha1": sha1, "ts": ts, "at": at, "verdict": json.loads(raw)}
                    except ValueError:
                        continue
        except OSError:
            pass
    return _data


def _write():
    tmp = CACHE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        for (repo, name), e in _data.items():
            f.write("\t".join((repo, name, e["sha1"], e["ts"], e["at"],
                               json.dumps(e["verdict"], ensure_ascii=False))) + "\n")
    os.replace(tmp, CACHE_FILE)


def _prune():
    limit = bot_clock.minus(bot_clock.now(), TTL_DAYS * 86400)
    for k in [k for k, v in _data.items() if (bot_clock.parse(v["at"]) or limit) < limit]:
        del _data[k]
    if len(_data) > MAX_ENTRIES:
        for k in sorted(_data, key=lambda k: _data[k]["at"])[:len(_data) - MAX_ENTRIES]:
            del _data[k]


def get(repo, name, sha1="", ts=""):
    """Verdict mémorisé (dict) si le fichier est inchangé, sinon None."""
    with _lock:
        e = _load().get((repo, norm_name(name)))
        if not e:
            return None
        if sha1 and e["sha1"] and sha1.lower() != e["sha1"]:
            return None
        if ts and e["ts"] and str(ts) != e["ts"]:
            return None
        return dict(e["verdict"])


def put(repo, name, sha1, ts, verdict):
    """Mémorise le verdict d'un fichier. Ne lève jamais."""
    with _lock:
        try:
            _load()[(repo, norm_name(name))] = {"sha1": str(sha1 or "").lower(), "ts": str(ts or ""),
                                                "at": bot_clock.iso(), "verdict": verdict}
            _prune()
            _write()
        except Exception as e:                                   # noqa: BLE001
            log.warning("Cache des images non écrit (%s) : %s", CACHE_FILE, e)


def verdict_from_result(res):
    """Verdict compact tiré d'un ai_detector.DetectionResult."""
    mod = getattr(res, "moderation", None)
    return {
        "ai": res.is_ai,
        "ai_source": res.source,
        "ai_score": res.ai_score,
        "flagged": bool(mod and mod.flagged),
        "reasons": list(mod.reasons) if mod else [],
        "moderated": mod is not None,
    }
