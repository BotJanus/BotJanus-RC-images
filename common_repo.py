#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
common_repo — recherche d'une image « similaire » sur le dépôt Common.

Utilisé par image_cleanup_bot AVANT de retirer du wikicode une image supprimée : si le
fichier (ou un équivalent) existe sur Common, l'article s'affiche toujours correctement et
on ne touche à rien.

Une image est jugée « similaire » si, sur Common, on trouve :
  1. un fichier de même nom ;
  2. un fichier de même contenu (empreinte SHA-1 ; lue dans les archives de suppression,
     si le compte a le droit « deletedhistory » — sinon cette étape est silencieusement sautée) ;
  3. un fichier dont le nom est quasi identique (casse, accents, espaces/underscores,
     extension .jpg/.jpeg/.png différente, similarité >= COMMONS_SIMILARITY, 0.92 par défaut).

Quel dépôt ? Dans l'ordre :
  a. variable COMMONS_API  (ex. https://commons.wikimedia.org/w/api.php) ;
  b. dépôt partagé déclaré par pywikibot pour la famille du wiki ;
  c. dépôt externe annoncé par l'API du wiki (action=query&meta=siteinfo&siprop=repos).
Si aucun n'est trouvé, la vérification est désactivée (et le contrôle de configuration le dit).

En cas d'erreur réseau/API, on lève CommonError : l'appelant NE supprime RIEN et réessaie
plus tard (supprimer à tort est bien pire que de retarder).
"""

import difflib
import logging
import os
import re
import unicodedata
from dataclasses import dataclass

import requests

import bot_config

log = logging.getLogger("common-repo")

USER_AGENT = "BotJanus-RC-images/3.0 (Vikidia bot)"
HTTP_TIMEOUT = 15
MIN_STEM_FOR_FUZZY = 5          # en dessous, on exige un nom identique (évite les faux positifs)


class CommonError(Exception):
    """Common injoignable ou réponse inexploitable : on ne peut pas conclure."""


@dataclass
class Match:
    name: str           # nom du fichier trouvé sur Common (sans espace de noms)
    reason: str         # pourquoi on le juge similaire

    def __str__(self):
        return "%s (%s)" % (self.name, self.reason)


def normalize(name):
    """Forme comparable d'un nom de fichier : sans extension, accents, casse ni séparateurs."""
    stem = os.path.splitext(name.replace("_", " ").strip())[0]
    stem = unicodedata.normalize("NFKD", stem)
    stem = "".join(c for c in stem if not unicodedata.combining(c))
    return re.sub(r"[\W_]+", "", stem.casefold())


def similarity(a, b):
    na, nb = normalize(a), normalize(b)
    if not na or not nb:
        return 0.0
    if na == nb:
        return 1.0
    if min(len(na), len(nb)) < MIN_STEM_FOR_FUZZY:
        return 0.0
    return difflib.SequenceMatcher(None, na, nb).ratio()


def _strip_ns(title):
    return title.split(":", 1)[1] if ":" in title else title


class CommonRepo:
    def __init__(self, site):
        self.site = site
        self._api = None
        self._resolved = False
        self.threshold = bot_config.env_float("COMMONS_SIMILARITY", 0.92)
        self._session = requests.Session()
        self._session.headers["User-Agent"] = USER_AGENT

    # ---- détection du dépôt -------------------------------------------------
    def api_url(self):
        """URL de api.php du dépôt Common, ou None s'il n'y en a pas. Lève CommonError si
        la détection elle-même échoue (réseau) : on réessaiera au prochain appel."""
        if self._resolved:
            return self._api
        url = bot_config.env_str("COMMONS_API")
        if not url:
            try:
                if self.site.has_image_repository():
                    repo = self.site.image_repository()
                    url = "%s://%s%s" % (repo.protocol(), repo.hostname(), repo.apipath())
            except Exception as e:
                log.debug("image_repository() indisponible : %s", e)
        if not url:
            try:
                data = self.site.simple_request(action="query", meta="siteinfo", siprop="repos").submit()
            except Exception as e:
                raise CommonError("détection du dépôt Common impossible : %s" % e)
            for repo in (data.get("query", {}) or {}).get("repos", []) or []:
                if repo.get("name") != "local" and repo.get("apiurl"):
                    url = repo["apiurl"]
                    break
        self._api, self._resolved = (url or None), True
        if self._api:
            log.info("Dépôt Common : %s", self._api)
        else:
            log.warning("Aucun dépôt Common détecté (définir COMMONS_API) : vérification désactivée.")
        return self._api

    @property
    def available(self):
        try:
            return bool(self.api_url())
        except CommonError:
            return False

    # ---- API ----------------------------------------------------------------
    def _query(self, **params):
        api = self.api_url()
        if not api:
            raise CommonError("aucun dépôt Common configuré")
        params.update(action="query", format="json", formatversion=2)
        last = None
        for _ in range(2):
            try:
                resp = self._session.get(api, params=params, timeout=HTTP_TIMEOUT)
                resp.raise_for_status()
                data = resp.json()
            except (requests.RequestException, ValueError) as e:
                last = e
                continue
            if "error" in data:
                raise CommonError("API Common : %s" % (data["error"].get("info") or data["error"]))
            return data.get("query", {}) or {}
        raise CommonError("Common injoignable : %s" % last)

    def probe(self):
        """Contrôle de configuration : renvoie l'URL de l'API si Common répond, None si non configuré.
        Lève CommonError si configuré mais injoignable."""
        api = self.api_url()
        if api:
            self._query(meta="siteinfo", siprop="general")
        return api

    # ---- recherche ----------------------------------------------------------
    def _deleted_sha1s(self, name):
        """Empreintes SHA-1 du fichier supprimé (archives de suppression). [] si illisible."""
        try:
            data = self.site.simple_request(action="query", list="filearchive", faprefix=name,
                                            faprop="sha1|name", falimit=20).submit()
        except Exception as e:
            log.debug("SHA-1 de %s indisponible (droit deletedhistory ?) : %s", name, e)
            return []
        wanted = name.replace("_", " ")
        found = []
        for item in (data.get("query", {}) or {}).get("filearchive", []) or []:
            if str(item.get("name", "")).replace("_", " ") == wanted and item.get("sha1"):
                if item["sha1"] not in found:
                    found.append(item["sha1"])
        return found

    def find_similar(self, name):
        """Renvoie un Match si une image similaire existe sur Common, sinon None.
        `name` : nom du fichier sans espace de noms. Lève CommonError si on ne peut pas conclure."""
        if not self.api_url():
            return None
        name = name.replace("_", " ")

        # 1. même nom
        pages = self._query(titles="File:" + name, prop="info").get("pages", [])
        if pages and not pages[0].get("missing") and not pages[0].get("invalid"):
            return Match(_strip_ns(pages[0].get("title", name)), "même nom")

        # 2. même contenu
        for sha1 in self._deleted_sha1s(name):
            images = self._query(list="allimages", aisha1=sha1, ailimit=5).get("allimages", [])
            if images:
                return Match(images[0].get("name", "?").replace("_", " "), "même contenu (SHA-1)")

        # 3. nom quasi identique (la recherche est un « bonus » : son échec n'empêche pas de conclure)
        words = " ".join(re.findall(r"\w+", os.path.splitext(name)[0])[:8])
        if words:
            try:
                hits = self._query(list="search", srnamespace=6, srsearch=words, srlimit=20).get("search", [])
            except CommonError as e:
                log.debug("Recherche Common ignorée : %s", e)
                hits = []
            best = None
            for hit in hits:
                cand = _strip_ns(hit.get("title", ""))
                score = similarity(name, cand)
                if score >= self.threshold and (best is None or score > best[0]):
                    best = (score, cand)
            if best:
                return Match(best[1], "nom très proche, %d %%" % round(best[0] * 100))
        return None
