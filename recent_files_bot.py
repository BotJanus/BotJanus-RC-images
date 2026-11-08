#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Surveillance des modifications récentes : ajout d'un fichier dans une page.

À chaque cycle commun (10 min, voir cycle.py), le script lit les modifications récentes
(list=recentchanges), compare chaque révision à la précédente et repère les fichiers AJOUTÉS :

    [[Fichier:X.jpg|...]]   [[File:X.jpg|...]]   [[Image:X.jpg|...]]   et les lignes de <gallery>

Pour chaque fichier ajouté, on regarde où il se trouve :

  • sur VIKIDIA (téléversé localement) → mêmes contrôles que pour un téléversement :
        licence ({{LI}} + message au téléverseur : license_bot.process_file) et
        IA + admissibilité (ai_detector.process_file_page).

  • sur COMMON (dépôt partagé) → IA et admissibilité seulement, l'admissibilité étant l'enjeu.
        Le bot ne peut ni poser {{SI}} ni toucher aux fichiers de Common : si l'image est NON
        ADMISSIBLE, il révoque la modification qui l'a ajoutée (annulation ; à défaut, retrait
        du fichier dans le wikicode) puis dépose une demande de masquage de cette modification dans
        « Vikidia:Demandes aux administrateurs/AAAA MM ».
        Une image IA (mais admissible) sur Common ne donne qu'une alerte Discord.

  • introuvable (lien rouge) → rien à faire.

Une image n'est analysée qu'une fois (file_cache.py : clé = dépôt + SHA-1), même ajoutée à
plusieurs pages ; le verdict mémorisé sert à agir sur les ajouts suivants.

Réglages (.env, tous facultatifs) :
    RC_FILES_DRY_RUN=1       simulation : aucune écriture sur le wiki
    RC_FILES_NAMESPACES=0|1  espaces de noms surveillés (défaut : tous sauf Fichier)
"""

import logging
import io
import re
import time
import urllib.parse
from dataclasses import dataclass
from typing import List, Optional

import pywikibot
import requests

import ai_detector
import bot_clock
import bot_config
import common_repo
import cycle
import discord_logger
import file_cache
import license_bot
import patrol_actions

# ==========================================================================
# CONFIGURATION
# ==========================================================================

DRY_RUN = bot_config.env_bool("RC_FILES_DRY_RUN", False)
NAMESPACES = bot_config.env_str("RC_FILES_NAMESPACES")        # ex. "0|1|2" ; vide = tous
FILE_NAMESPACE = 6
MAX_RC_ENTRIES = 5000          # garde-fou : modifications lues par cycle
REVISION_BATCH = 25            # révisions demandées par appel API
THUMB_WIDTH = 1500             # miniature de secours si l'original est trop gros
MAX_ATTEMPTS = 3               # essais max si l'analyse est impossible, ensuite on abandonne
SLEEP_BETWEEN_EDITS = 2        # secondes

ADMIN_REQUESTS_PREFIX = patrol_actions.ADMIN_REQUESTS_PREFIX

EDIT_SUMMARY_REVERT = "Bot : révocation d'une modification ajoutant une image non admissible (demande de masquage déposée)"
EDIT_SUMMARY_REMOVE = "Bot : retrait d'une image non admissible (demande de masquage déposée)"

EMBED_COLOR_FLAGGED = 0xE67E22
EMBED_COLOR_AI = 0xE74C3C
EMBED_COLOR_ERROR = 0xF1C40F

# Résultats de traitement d'une modification
DONE, RETRY = "done", "retry"

log = logging.getLogger("recent-files")


# ==========================================================================
# EXTRACTION DES FICHIERS D'UN WIKICODE
# ==========================================================================

_PREFIX = r"(?:fichier|file|image)"
_MASK_RE = re.compile(r"<!--.*?-->|<nowiki\b[^>]*>.*?</nowiki\s*>|<pre\b[^>]*>.*?</pre\s*>",
                      re.S | re.I)
_GALLERY_RE = re.compile(r"<gallery\b[^>]*>(.*?)</gallery\s*>", re.S | re.I)
_LINK_OPEN_RE = re.compile(r"\[\[\s*" + _PREFIX + r"\s*:", re.I)
_GALLERY_PREFIX_RE = re.compile(r"^\s*" + _PREFIX + r"\s*:\s*", re.I)

RASTER_EXTENSIONS = (".jpg", ".jpeg", ".png", ".gif", ".webp", ".tif", ".tiff", ".bmp")


def normalize_name(name):
    """Nom de fichier canonique : sans « Fichier: », espaces (pas de « _ »), 1re lettre en majuscule."""
    name = name.strip()
    if "%" in name:
        name = urllib.parse.unquote(name)
    name = re.sub(r"[\s_]+", " ", name).strip()
    return name[:1].upper() + name[1:] if name else ""


def _mask(text):
    """Remplace commentaires, <nowiki> et <pre> par des espaces (mêmes positions, mêmes retours
    à la ligne) : ce qui est commenté n'est pas un fichier affiché."""
    return _MASK_RE.sub(lambda m: re.sub(r"[^\n]", " ", m.group()), text)


def find_file_spans(text):
    """[(début, fin, nom normalisé)] des fichiers affichés, positions sur le texte d'origine."""
    masked = _mask(text)
    spans, gallery_ranges = [], []

    # <gallery> : une ligne = un fichier, préfixe facultatif, légende après « | »
    for gm in _GALLERY_RE.finditer(masked):
        gallery_ranges.append((gm.start(), gm.end()))
        pos = gm.start(1)
        for line in masked[gm.start(1):gm.end(1)].splitlines(keepends=True):
            start, pos = pos, pos + len(line)
            body = _GALLERY_PREFIX_RE.sub("", line.strip(), count=1)
            name = normalize_name(body.split("|", 1)[0])
            if name and "{{" not in name and "[[" not in name:
                spans.append((start, pos, name))

    # [[Fichier:...]] : on suit les crochets imbriqués (légendes contenant des liens)
    n = len(masked)
    for m in _LINK_OPEN_RE.finditer(masked):
        if any(a <= m.start() < b for a, b in gallery_ranges):
            continue
        depth, i, end = 1, m.start() + 2, None
        while i < n - 1:
            two = masked[i:i + 2]
            if two == "[[":
                depth, i = depth + 1, i + 2
            elif two == "]]":
                depth, i = depth - 1, i + 2
                if depth == 0:
                    end = i
                    break
            else:
                i += 1
        if end is None:
            continue
        raw = masked[m.end():end - 2].split("|", 1)[0]
        if "[[" in raw or "{{" in raw or "\n" in raw.strip():
            continue
        name = normalize_name(raw)
        if name:
            spans.append((m.start(), end, name))
    spans.sort()
    return spans


def extract_files(text):
    """Noms normalisés des fichiers affichés par le wikicode, sans doublon, dans l'ordre."""
    seen, out = set(), []
    for _s, _e, name in find_file_spans(text or ""):
        if name not in seen:
            seen.add(name)
            out.append(name)
    return out


def added_files(old_text, new_text):
    """Fichiers présents dans `new_text` et absents de `old_text`."""
    before = set(extract_files(old_text))
    return [n for n in extract_files(new_text) if n not in before]


def remove_files(text, names):
    """Retire du wikicode toutes les occurrences des fichiers `names` (noms normalisés).
    Renvoie (nouveau texte, nombre de retraits)."""
    wanted = {normalize_name(n) for n in names}
    spans = [s for s in find_file_spans(text) if s[2] in wanted]
    for start, end, _name in sorted(spans, reverse=True):
        text = text[:start] + text[end:]
    return text, len(spans)


def is_raster_image(mime, name=""):
    """Image analysable par IA / modération (pas de SVG, de son, de vidéo, de PDF)."""
    if mime:
        return mime.startswith("image/") and "svg" not in mime
    return name.lower().endswith(RASTER_EXTENSIONS)


# ==========================================================================
# API
# ==========================================================================

def _pages(data):
    """Pages d'une réponse query, que formatversion soit 1 (dict) ou 2 (liste)."""
    pages = (data.get("query", {}) or {}).get("pages", [])
    return list(pages.values()) if isinstance(pages, dict) else list(pages or [])


def _rev_text(rev):
    main = (rev.get("slots") or {}).get("main") or {}
    for source in (main, rev):
        for key in ("content", "*"):
            if isinstance(source.get(key), str):
                return source[key]
    return None


def fetch_recent_edits(site, since):
    """Modifications et créations depuis `since`, de la plus ancienne à la plus récente."""
    params = dict(action="query", list="recentchanges", rcdir="newer", rcstart=bot_clock.iso(since),
                  rctype="edit|new", rcprop="title|ids|user|timestamp|comment|flags",
                  rclimit=500, formatversion=2)
    me = site.username() if hasattr(site, "username") else None
    if me:
        params["rcexcludeuser"] = me            # jamais nos propres modifications
    if NAMESPACES:
        params["rcnamespace"] = NAMESPACES
    entries = []
    while len(entries) < MAX_RC_ENTRIES:
        data = site.simple_request(**params).submit()
        entries += (data.get("query", {}) or {}).get("recentchanges", []) or []
        if not data.get("continue"):
            break
        params.update(data["continue"])
    return entries


def fetch_revision_texts(site, revids):
    """{revid: wikicode} ; les révisions masquées ou supprimées sont absentes."""
    ids = sorted({int(r) for r in revids if r})
    texts = {}
    for i in range(0, len(ids), REVISION_BATCH):
        chunk = ids[i:i + REVISION_BATCH]
        data = site.simple_request(action="query", prop="revisions", rvprop="ids|content",
                                   rvslots="main", revids="|".join(map(str, chunk)),
                                   formatversion=2).submit()
        for page in _pages(data):
            for rev in page.get("revisions", []) or []:
                text = _rev_text(rev)
                if text is not None and rev.get("revid") is not None:
                    texts[int(rev["revid"])] = text
    return texts


@dataclass
class FileInfo:
    name: str
    repo: str                  # "local" (Vikidia) ou "shared" (Common)
    url: str = ""
    thumb: str = ""
    sha1: str = ""
    mime: str = ""
    timestamp: str = ""        # date de la dernière version (distingue un fichier re-téléversé)

    @property
    def raster(self):
        return is_raster_image(self.mime, self.name)


_commons = {}


def commons_repo(site):
    """Dépôt Common (COMMONS_API du .env, sinon détecté via le wiki), partagé avec le reste du bot."""
    if site not in _commons:
        _commons[site] = common_repo.CommonRepo(site)
    return _commons[site]


def _commons_lookup(site, name):
    """Le fichier existe-t-il sur Common ? Interrogé directement via COMMONS_API : le wiki n'a pas
    forcément Common comme dépôt partagé. FileInfo(repo="shared") ou None.
    Lève common_repo.CommonError si Common est injoignable (on réessaiera)."""
    repo = commons_repo(site)
    if not repo.api_url():
        return None
    q = repo._query(titles="File:" + name, prop="imageinfo", iiprop="url|sha1|mime|size|timestamp",
                    iiurlwidth=THUMB_WIDTH)
    for page in q.get("pages", []) or []:
        infos = page.get("imageinfo") or []
        if page.get("missing") or page.get("invalid") or not infos:
            return None
        ii = infos[0]
        return FileInfo(name=name, repo="shared", url=ii.get("url", ""), thumb=ii.get("thumburl", ""),
                        sha1=ii.get("sha1", ""), mime=ii.get("mime", ""), timestamp=ii.get("timestamp", ""))
    return None


def lookup_file(site, name):
    """Où est le fichier ? FileInfo (local / shared) ou None s'il n'existe nulle part.
    1. Vikidia (et dépôt partagé éventuel du wiki) : une requête, l'API dit d'où vient le fichier ;
    2. sinon Common, interrogé directement (COMMONS_API)."""
    info = _wiki_lookup(site, name)
    return info if info is not None else _commons_lookup(site, name)


def _wiki_lookup(site, name):
    data = site.simple_request(action="query", titles="File:" + name, prop="imageinfo|info",
                               iiprop="url|sha1|mime|size|timestamp", iiurlwidth=THUMB_WIDTH,
                               formatversion=2).submit()
    for page in _pages(data):
        if "invalid" in page:
            return None
        repo = page.get("imagerepository", "")
        infos = page.get("imageinfo") or []
        if repo not in ("local", "shared") or not infos:
            return None
        ii = infos[0]
        return FileInfo(name=name, repo=repo, url=ii.get("url", ""), thumb=ii.get("thumburl", ""),
                        sha1=ii.get("sha1", ""), mime=ii.get("mime", ""),
                        timestamp=ii.get("timestamp", ""))
    return None


# ==========================================================================
# ANALYSE D'UNE IMAGE DE COMMON
# ==========================================================================

class Indeterminate(Exception):
    """Analyse impossible pour l'instant (réseau, quota, clé...) : à refaire au cycle suivant."""


DOWNLOAD_TIMEOUT = 30
DOWNLOAD_TRIES = 3
MAX_RETRY_AFTER = 30           # secondes : on n'attend pas plus longtemps un « 429 »


def download_commons(url):
    """Télécharge un fichier de Common avec un User-Agent conforme à la politique de Wikimedia
    (nom du bot + contact). « 429 » : on patiente (Retry-After) puis on réessaie.
    Lève ValueError si le fichier dépasse la taille permise, requests.RequestException sinon."""
    last = None
    for attempt in range(DOWNLOAD_TRIES):
        resp = requests.get(url, headers={"User-Agent": bot_config.user_agent()},
                            timeout=DOWNLOAD_TIMEOUT, stream=True)
        if resp.status_code == 429:
            try:
                wait = min(float(resp.headers.get("Retry-After", 5)), MAX_RETRY_AFTER)
            except ValueError:
                wait = 5
            last = requests.HTTPError("429 Too Many Requests", response=resp)
            log.warning("Common répond 429 (essai %d/%d) : pause de %.0f s.", attempt + 1, DOWNLOAD_TRIES, wait)
            resp.close()
            time.sleep(wait)
            continue
        resp.raise_for_status()
        buf = io.BytesIO()
        for chunk in resp.iter_content(65536):
            buf.write(chunk)
            if buf.tell() > ai_detector.MAX_IMAGE_BYTES:
                raise ValueError("image trop volumineuse")
        return buf.getvalue()
    raise last


def analyze_shared(info):
    """IA + admissibilité d'une image de Common. Renvoie le verdict (dict, cf. file_cache).
    Lève Indeterminate si l'admissibilité n'a pas pu être contrôlée (on ne conclut jamais à
    la légère : ni révocation, ni « image saine »)."""
    thumb_only = False
    try:
        data = download_commons(info.url)
    except ValueError:                                  # trop volumineuse : miniature, modération seule
        if not info.thumb:
            raise Indeterminate("image trop volumineuse et sans miniature")
        data, thumb_only = download_commons(info.thumb), True
    except Exception as e:                              # noqa: BLE001
        raise Indeterminate("téléchargement impossible : %s" % e)

    if thumb_only:                                      # les métadonnées sont perdues dans une miniature
        res = ai_detector.DetectionResult()
        res.moderation, err = ai_detector.check_moderation_only(data)
        if err:
            res.errors.append(err)
    else:
        res = ai_detector.detect_ai_image(data, info.url)
        # Étapes 1/2 : l'IA est prouvée mais la modération n'a pas tourné → on la lance (c'est l'enjeu)
        if res.moderation is None and res.stage in (1, 2):
            res.moderation, err = ai_detector.check_moderation_only(data)
            if err:
                res.errors.append(err)

    if res.moderation is None:
        raise Indeterminate("; ".join(res.errors) or "modération indisponible")
    verdict = file_cache.verdict_from_result(res)
    verdict["thumb_only"] = thumb_only
    return verdict


# ==========================================================================
# ACTIONS SUR LE WIKI
# ==========================================================================

def admin_request_title():
    return patrol_actions.admin_request_title()


def request_hiding(site, revid, dry_run, where="venant de Commons"):
    """Demande de masquage de la modification `revid` (idempotente : cherche la diff dans la page)."""
    return patrol_actions.request_hiding(site, "[[Special:Diff/%d]]" % revid, "Special:Diff/%d" % revid,
                                         dry_run, where=where)


def revert_edit(site, title, revid, old_revid, names, dry_run):
    """Révoque la modification `revid`. Renvoie une phrase décrivant ce qui a été fait.
    1. annulation pure (undo) — impossible pour une création de page ;
    2. à défaut (modifications intermédiaires en conflit, création) : retrait des fichiers du wikicode."""
    if dry_run:
        log.info("[DRY-RUN] aurait révoqué la modification %s sur %s", revid, title)
        return "simulé"
    if old_revid:
        try:
            patrol_actions.api_edit(site, title=title, undo=revid, undoafter=old_revid,
                      summary=EDIT_SUMMARY_REVERT, nocreate=1)
            log.info("✓ Modification %s annulée sur %s", revid, title)
            return "modification annulée"
        except pywikibot.exceptions.APIError as e:
            if getattr(e, "code", "") != "undofailure":
                raise
            log.info("Annulation impossible sur %s (modifications intermédiaires) : retrait du fichier.", title)

    page = pywikibot.Page(site, title)
    new_text, removed = remove_files(page.text, names)
    if not removed:
        return "rien à retirer (fichier déjà absent de la page)"
    page.text = new_text
    page.save(summary=EDIT_SUMMARY_REMOVE, minor=False, bot=False)
    log.info("✓ Fichier retiré de %s", title)
    return "fichier retiré du wikicode"


# ==========================================================================
# DISCORD
# ==========================================================================

def _page_url(site, title):
    try:
        return pywikibot.Page(site, title).full_url()
    except Exception:                                   # noqa: BLE001
        return ""


def _diff_url(site, revid):
    try:
        return "https://%s%s/index.php?diff=%d" % (site.hostname(), site.scriptpath(), revid)
    except Exception:                                   # noqa: BLE001
        return ""


def _embed(title, description, color, fields, thumbnail_url=None, bulletin=False):
    """Embed dans le salon des logs ; `bulletin=True` : copié aussi vers BULLETIN_PATROUILLEURS."""
    try:
        embed = discord_logger.build_embed(title, description, color, fields=fields,
                                           thumbnail_url=thumbnail_url, footer="recent_files_bot")
        discord_logger.send_embed(embed)
        if bulletin:
            patrol_actions.forward_bulletin(embed)
    except Exception as e:                              # noqa: BLE001
        log.warning("Embed Discord : envoi impossible : %s", e)


# ==========================================================================
# TRAITEMENT D'UNE MODIFICATION
# ==========================================================================

def check_local(site, info, dry_run):
    """Fichier de Vikidia : comme s'il venait d'être téléversé (licence, IA, admissibilité :
    SI + bulletin, par ai_detector). Renvoie le verdict si l'image est non admissible, sinon None.
    La demande de masquage est faite ici au niveau de la modification, pas du fichier."""
    page = pywikibot.FilePage(site, "File:" + info.name)

    known = license_bot.known_licenses(site)
    if known:
        license_bot.process_file(site, page, known, dry_run)
    else:
        log.warning("Liste des licences vide : contrôle de licence ignoré pour %s.", info.name)

    if not info.raster:
        log.info("%s n'est pas une image raster : pas d'analyse IA / admissibilité.", info.name)
        return
    # Déjà analysée et inchangée : ai_detector réutilise le verdict (file_cache) sans ré-analyser.
    res = ai_detector.process_file_page(site, page.title(), dry_run=dry_run,
                                        request_hiding=False, forward=False)
    if res is not None and res.moderation is not None and res.moderation.flagged:
        return file_cache.verdict_from_result(res)
    return None


def check_shared(info):
    """Fichier de Common : verdict mémorisé ou analyse. None si ce n'est pas une image raster."""
    if not info.raster:
        log.info("%s (Common) n'est pas une image raster : ignorée.", info.name)
        return None
    cached = file_cache.get("shared", info.name, info.sha1, info.timestamp)
    if cached is not None:
        log.info("%s (Common) déjà analysée : verdict mémorisé.", info.name)
        return cached
    verdict = analyze_shared(info)                      # peut lever Indeterminate
    file_cache.put("shared", info.name, info.sha1, info.timestamp, verdict)
    return verdict


def process_entry(site, rc, names, dry_run):
    """Traite une modification qui ajoute les fichiers `names`. Renvoie DONE ou RETRY."""
    title, revid, user = rc.get("title", "?"), int(rc["revid"]), rc.get("user", "?")
    log.info("Modification %s sur %s (%s) : fichier(s) ajouté(s) : %s", revid, title, user, ", ".join(names))

    flagged, ai_only = [], []              # [(info, verdict)]
    retry = False
    for name in names:
        try:
            info = lookup_file(site, name)
        except Exception as e:                          # noqa: BLE001
            log.warning("Recherche de %s impossible (%s) : on réessaiera.", name, e)
            retry = True
            continue
        if info is None:
            log.info("%s n'existe ni sur Vikidia ni sur Common : rien à faire.", name)
            continue
        try:
            if info.repo == "local":
                verdict = check_local(site, info, dry_run)
            else:
                verdict = check_shared(info)
        except Indeterminate as e:
            log.warning("Analyse de %s (Common) impossible : %s", name, e)
            retry = True
            continue
        except Exception as e:                          # noqa: BLE001
            log.error("Erreur sur %s : %s", name, e)
            retry = True
            continue
        if verdict is None:
            log.info("%s (%s) : contrôles faits, rien à signaler.", name,
                     "Vikidia" if info.repo == "local" else "Common")
            continue
        if verdict.get("flagged"):
            flagged.append((info, verdict))
        elif verdict.get("ai"):
            ai_only.append((info, verdict))
        else:
            log.info("%s (Common) : image jugée saine.", name)

    page_url = _page_url(site, title)
    for info, verdict in ai_only:
        _embed("🚨 Image IA venant de Common", "[**%s**](%s)\nAjoutée par **%s** (modification %s)."
               % (info.name, page_url, user, revid), EMBED_COLOR_AI,
               [("Source", verdict.get("ai_source") or "—", True), ("Page", title, True),
                ("Action", "alerte seulement : {{SI}} impossible sur Common", False)])

    if flagged:
        names_flagged = [i.name for i, _v in flagged]
        where = "venant de Commons" if any(i.repo == "shared" for i, _v in flagged) else "sur Vikidia"
        reasons = "; ".join("%s : %s" % (i.name, ", ".join(v.get("reasons") or ["?"])) for i, v in flagged)
        outcome = []
        try:
            outcome.append(revert_edit(site, title, revid, int(rc.get("old_revid") or 0),
                                       names_flagged, dry_run))
        except Exception as e:                          # noqa: BLE001
            log.error("Révocation de la modification %s impossible : %s", revid, e)
            outcome.append("révocation ÉCHOUÉE (%s)" % str(e)[:200])
        try:
            requested = request_hiding(site, revid, dry_run, where=where)
            outcome.append("demande de masquage déposée" if requested else "demande déjà présente")
        except Exception as e:                          # noqa: BLE001
            log.error("Demande de masquage de %s impossible : %s", revid, e)
            retry = True                                # la révocation est idempotente : on recommence tout
            outcome.append("demande de masquage ÉCHOUÉE (%s)" % str(e)[:200])
        bot_clock.sleep(SLEEP_BETWEEN_EDITS)
        # Pas de miniature : l'image est non admissible.
        _embed("%s🚩 Image non admissible %s" % ("🧪 [DRY-RUN] " if dry_run else "", where.replace("venant de Commons", "venant de Common")),
               "[**%s**](%s)\nAjoutée par **%s** — [modification %s](%s)"
               % (title, page_url, user, revid, _diff_url(site, revid)),
               EMBED_COLOR_FLAGGED,
               [("Fichier(s)", ", ".join(names_flagged), False), ("Motif", reasons[:900], False),
                ("Actions", "\n".join(outcome), False)], bulletin=True)
    return RETRY if retry else DONE


# ==========================================================================
# CYCLE
# ==========================================================================

_seen_revids = set()         # la fenêtre de lecture se chevauche
_attempts = {}               # revid -> nombre d'essais


def run_cycle(site, since, dry_run=DRY_RUN):
    """Un passage : examine les modifications depuis `since`.
    Renvoie True si les modifications ont pu être lues et qu'aucune analyse n'est en attente."""
    try:
        entries = fetch_recent_edits(site, since)
    except Exception as e:                              # noqa: BLE001
        log.error("Erreur lors de la lecture des modifications récentes : %s", e)
        return False

    log.info("Modifications récentes : %d lue(s) depuis %s.", len(entries), bot_clock.iso(since))
    fresh = [rc for rc in entries
             if rc.get("revid") and int(rc["revid"]) not in _seen_revids
             and rc.get("ns") != FILE_NAMESPACE]
    if not fresh:
        log.info("Aucune nouvelle modification à examiner.")
        return True
    try:
        texts = fetch_revision_texts(
            site, [int(rc["revid"]) for rc in fresh] + [int(rc.get("old_revid") or 0) for rc in fresh])
    except Exception as e:                              # noqa: BLE001
        log.error("Lecture des révisions impossible : %s", e)
        return False

    to_retry, examined = False, 0
    for rc in fresh:
        revid = int(rc["revid"])
        new_text = texts.get(revid)
        old_revid = int(rc.get("old_revid") or 0)
        old_text = "" if not old_revid else texts.get(old_revid)
        if new_text is None or old_text is None:        # révision masquée / supprimée
            _seen_revids.add(revid)
            continue
        names = added_files(old_text, new_text)
        if not names:
            _seen_revids.add(revid)
            continue
        examined += 1
        try:
            result = process_entry(site, rc, names, dry_run)
        except Exception as e:                          # noqa: BLE001
            log.error("Erreur inattendue sur la modification %s : %s", revid, e)
            result = RETRY
        if result == RETRY:
            _attempts[revid] = _attempts.get(revid, 0) + 1
            if _attempts[revid] < MAX_ATTEMPTS:
                to_retry = True                         # la fenêtre n'avance pas : reprise au prochain cycle
                continue
            log.error("Abandon de la modification %s (%s) après %d essais.", revid, rc.get("title"), MAX_ATTEMPTS)
            _embed("⚠️ Analyse abandonnée", "Modification %s sur **%s** : analyse impossible après %d essais."
                   % (revid, rc.get("title"), MAX_ATTEMPTS), EMBED_COLOR_ERROR, None)
        _attempts.pop(revid, None)
        _seen_revids.add(revid)

    if len(_seen_revids) > 5000:
        _seen_revids.clear()
    log.info("Sondage terminé : %d modification(s) nouvelle(s), dont %d ajoutant des fichiers.",
             len(fresh), examined)
    return not to_retry


def main():
    """Mode autonome (hors main.py) : même cycle commun, même état partagé."""
    import config_check

    bot_config.setup_logging()
    if not discord_logger.is_installed():
        discord_logger.install()
    if DRY_RUN:
        log.info("=== MODE DRY-RUN : aucune modification ne sera écrite sur le wiki ===")
    site = pywikibot.Site("fr", "vikidia")
    site.login()
    config_check.run(site, scripts=("license", "ai", "recent"))
    try:
        cycle.run_loop(site, [("recent_files_bot", run_cycle)])
    except KeyboardInterrupt:
        log.info("Arrêt demandé (Ctrl+C), fin de la surveillance des fichiers ajoutés.")


if __name__ == "__main__":
    main()
