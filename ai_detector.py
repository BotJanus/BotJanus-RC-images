#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Détection d'images IA — pipeline conditionnel à 3 étapes (du moins cher au plus cher).

  Étape 1 (local, gratuit)  : métadonnées EXIF / PNG / XMP (exifread + Pillow) et
                              manifestes C2PA / Content Credentials (c2pa-python).
  Étape 2 (API OpenAI)      : Content Provenance Checks (C2PA + SynthID) via le SDK openai.
  Étape 3 (API SightEngine) : modèle `genai` (score `ai_generated`) + modération de
                              contenu (nudité, contenu offensant, gore) dans le même appel.

Dès qu'une étape apporte une preuve d'IA, on s'arrête : les étapes suivantes (donc les
appels API payants) ne sont pas exécutées.

Mode test ponctuel :
    python ai_detector.py "Fichier:Exemple.png" --dry-run
Mode surveillance continue (lancé via main.py) :
    python ai_detector.py

Variables d'environnement (ou fichier .env / env/agent.env) :
    OPENAI_API_KEY            clé OpenAI (étape 2 ; étape ignorée si absente)
    SIGHTENGINE_API_USER      identifiant SightEngine (étape 3)
    SIGHTENGINE_API_SECRET    secret SightEngine (étape 3)
    AI_SCORE_THRESHOLD        seuil `ai_generated` en % (défaut 95)
    MODERATION_THRESHOLD      seuil de modération en % (défaut 80)
    MODERATION_TEMPLATE       modèle wiki posé si contenu inapproprié, ex. {{Image inappropriée}}
                              (vide par défaut = alerte Discord uniquement, aucune écriture)
    MODERATE_ALWAYS           "1" = modérer même si l'étape 1/2 conclut déjà à de l'IA
                              (1 appel SightEngine supplémentaire ; défaut : 0)
    (les embeds Discord passent par discord_logger : DISCORD_BOT_TOKEN + DISCORD_LOG_CHANNEL_ID,
     plus de webhook dédié)
"""

import argparse
import io
import json
import logging
import os
import re
import sys
import time
from dataclasses import dataclass, field
from typing import List, Optional

import requests

import discord_logger

try:                                    # pywikibot n'est requis que pour le mode wiki
    import pywikibot
    import bot_state
except ImportError:                     # permet de tester le pipeline seul
    pywikibot = None
    bot_state = None

# --- dépendances optionnelles : leur absence ne doit jamais faire planter le bot ---
try:
    import exifread
except ImportError:
    exifread = None
try:
    from PIL import Image
except ImportError:
    Image = None
try:
    from openai import OpenAI
except ImportError:
    OpenAI = None
try:
    import c2pa
except ImportError:
    c2pa = None


# ==========================================================================
# CONFIGURATION
# ==========================================================================

def load_env_file(filepath):
    """Injecte dans os.environ les variables d'un fichier .env (sans écraser l'existant)."""
    if not os.path.exists(filepath):
        return
    with open(filepath, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                os.environ.setdefault(key.strip(), value.strip().strip("'\""))


_here = os.path.dirname(os.path.abspath(__file__))
for _p in (".env", "env/agent.env", os.path.join(_here, ".env"), os.path.join(_here, "env", "agent.env")):
    load_env_file(_p)


def _float_env(name, default):
    try:
        return float(os.getenv(name, default))
    except ValueError:
        return float(default)


OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()
SIGHTENGINE_API_USER = os.getenv("SIGHTENGINE_API_USER", "").strip()
SIGHTENGINE_API_SECRET = os.getenv("SIGHTENGINE_API_SECRET", "").strip()
SIGHTENGINE_URL = "https://api.sightengine.com/1.0/check.json"
SIGHTENGINE_MODELS = "genai,nudity-2.0,offensive-2.0,gore-2.0"
SIGHTENGINE_MODERATION_MODELS = "nudity-2.0,offensive-2.0,gore-2.0"

AI_SCORE_THRESHOLD = _float_env("AI_SCORE_THRESHOLD", 90.0)        # en %
MODERATION_THRESHOLD = _float_env("MODERATION_THRESHOLD", 80.0)    # en %
MODERATION_TEMPLATE = os.getenv("MODERATION_TEMPLATE", "").strip()
MODERATE_ALWAYS = os.getenv("MODERATE_ALWAYS", "0").strip().lower() in ("1", "true", "oui", "yes")

TAG_TEMPLATE = "{{SI|Image IA|BotJanus}}\n"             # sans pourcentage connu
TAG_TEMPLATE_PERCENT = "{{SI|Image IA (%s%%)|BotJanus}}\n"  # avec le score SightEngine


def build_ai_tag(ai_score):
    """Bandeau à placer en haut de la page : pourcentage inclus s'il y en a un."""
    if ai_score is None:
        return TAG_TEMPLATE
    return TAG_TEMPLATE_PERCENT % int(round(ai_score))
EDIT_SUMMARY_AI = "Bot : ajout du bandeau Image IA (détection automatique : %s)"
EDIT_SUMMARY_MODERATION = "Bot : signalement d'un contenu potentiellement inapproprié (%s)"
POLL_INTERVAL = int(_float_env("AI_POLL_INTERVAL", 60))   # secondes
HTTP_TIMEOUT = 20
MAX_IMAGE_BYTES = 30 * 1024 * 1024
USER_AGENT = "BotJanus-ai-detector/2.0 (Vikidia bot)"

EMBED_COLOR_AI_DETECTED = 0xE74C3C
EMBED_COLOR_CLEAN = 0x2ECC71
EMBED_COLOR_SKIP = 0x95A5A6
EMBED_COLOR_ERROR = 0xF1C40F
EMBED_COLOR_DRYRUN = 0x3498DB
EMBED_COLOR_MODERATION = 0xE67E22

log = logging.getLogger("ai-detector")


# ==========================================================================
# RÉSULTAT
# ==========================================================================

@dataclass
class ModerationResult:
    flagged: bool = False
    reasons: List[str] = field(default_factory=list)   # ex. ["nudité 92 %"]
    scores: dict = field(default_factory=dict)


@dataclass
class DetectionResult:
    is_ai: Optional[bool] = None        # None = indéterminé (erreur / pas de clé)
    stage: int = 0                      # étape ayant tranché (0 = aucune)
    source: str = ""                    # explication courte
    evidence: List[str] = field(default_factory=list)
    ai_score: Optional[float] = None    # % (étape 3 uniquement)
    moderation: Optional[ModerationResult] = None
    errors: List[str] = field(default_factory=list)


# ==========================================================================
# ÉTAPE 1 — MÉTADONNÉES LOCALES (EXIF / PNG / XMP + C2PA)
# ==========================================================================

# (motif, libellé). Volontairement spécifiques pour limiter les faux positifs.
AI_KEYWORDS = [
    (r"midjourney", "Midjourney"),
    (r"stable[\s_-]?diffusion", "Stable Diffusion"),
    (r"comfy\s?ui|comfyui", "ComfyUI"),
    (r"automatic1111|a1111|sd-webui", "AUTOMATIC1111"),
    (r"invokeai", "InvokeAI"),
    (r"dall[\s·\-_.]?e", "DALL-E"),
    (r"openai", "OpenAI"),
    (r"adobe firefly|firefly", "Adobe Firefly"),
    (r"leonardo\.?ai", "Leonardo.ai"),
    (r"novelai", "NovelAI"),
    (r"nightcafe", "NightCafe"),
    (r"dreamstudio|stability\.?ai", "Stability AI"),
    (r"black forest labs|flux\.1", "FLUX"),
    (r"google imagen|imagen\s?\d|synthid", "Google Imagen/SynthID"),
    (r"ideogram", "Ideogram"),
    (r"bing image creator|designer\.microsoft", "Microsoft Designer"),
    (r"playground\s?ai", "Playground AI"),
    (r"civitai", "Civitai"),
    (r"algorithmicmedia", "IPTC DigitalSourceType (média algorithmique)"),
    (r"ai[\s_-]?generated", "mention « AI generated »"),
]
_AI_KEYWORDS_RE = [(re.compile(p, re.I), label) for p, label in AI_KEYWORDS]

# Clés texte PNG typiques des générateurs locaux
_PNG_SD_KEYS = {"parameters"}                       # AUTOMATIC1111 / Forge
_PNG_COMFY_KEYS = {"prompt", "workflow"}            # ComfyUI (JSON avec class_type)

# Valeurs IPTC / C2PA "digitalSourceType" qui indiquent une génération par IA
_AI_SOURCE_TYPES = (
    "trainedalgorithmicmedia",
    "compositewithtrainedalgorithmicmedia",
    "algorithmicmedia",
)


def _scan_text_for_ai(text, where):
    hits = []
    if not text:
        return hits
    for rx, label in _AI_KEYWORDS_RE:
        if rx.search(text):
            hits.append("%s : mention de %s" % (where, label))
    return hits


def _read_exif(image_bytes):
    """EXIF via exifread. Retourne une liste de preuves. Ne lève jamais."""
    if exifread is None:
        log.debug("exifread non installé : lecture EXIF ignorée.")
        return []
    hits = []
    try:
        tags = exifread.process_file(io.BytesIO(image_bytes), details=False)
        for key, value in tags.items():
            if key in ("JPEGThumbnail", "TIFFThumbnail"):
                continue
            hits += _scan_text_for_ai("%s=%s" % (key, value), "EXIF " + key) if \
                key.split()[-1] in ("Software", "ImageDescription", "Artist", "Copyright",
                                    "UserComment", "XPComment", "Make", "Model",
                                    "XPKeywords", "XPSubject") else []
    except Exception as e:
        log.debug("Lecture EXIF impossible : %s", e)
    return hits


def _read_pillow_metadata(image_bytes):
    """Chunks texte PNG, commentaires JPEG et XMP via Pillow. Retourne des preuves."""
    if Image is None:
        log.debug("Pillow non installé : lecture des métadonnées PNG/XMP ignorée.")
        return []
    hits = []
    try:
        with Image.open(io.BytesIO(image_bytes)) as img:
            info = dict(img.info or {})
            for key, value in info.items():
                if isinstance(value, bytes):
                    try:
                        value = value.decode("utf-8", "ignore")
                    except Exception:
                        continue
                if not isinstance(value, str):
                    continue
                k = str(key).lower()
                if k in _PNG_SD_KEYS and re.search(r"steps:\s*\d+.*sampler:", value, re.I | re.S):
                    hits.append("PNG « parameters » au format Stable Diffusion (Steps/Sampler)")
                elif k in _PNG_COMFY_KEYS and "class_type" in value:
                    hits.append("PNG « %s » au format ComfyUI (class_type)" % key)
                elif k in ("sd-metadata", "invokeai_metadata", "invokeai_graph"):
                    hits.append("PNG « %s » (InvokeAI)" % key)
                elif k in ("xml:com.adobe.xmp", "xmp", "comment", "description", "software", "author",
                           "source", "dream"):
                    hits += _scan_text_for_ai(value[:200000], "métadonnée " + str(key))
    except Exception as e:
        log.debug("Lecture Pillow impossible : %s", e)
    return hits


def _guess_mime(image_bytes):
    if image_bytes[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if image_bytes[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if image_bytes[:4] == b"RIFF" and image_bytes[8:12] == b"WEBP":
        return "image/webp"
    if image_bytes[4:12] in (b"ftypavif", b"ftypheic", b"ftypheix"):
        return "image/avif" if b"avif" in image_bytes[4:12] else "image/heic"
    if image_bytes[:4] in (b"II*\x00", b"MM\x00*"):
        return "image/tiff"
    if image_bytes[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    return "application/octet-stream"


def _read_c2pa(image_bytes):
    """Lit le manifeste C2PA. Retourne (preuves, erreurs). Ne lève jamais.

    Compatible avec les deux générations de l'API c2pa-python :
    Reader(mime, stream) (récente) et Reader.from_file / c2pa.read_file (ancienne).
    """
    if c2pa is None:
        return [], ["c2pa-python non installé : lecture C2PA ignorée (pip install c2pa-python)"]

    mime = _guess_mime(image_bytes)
    manifest_json = None
    try:
        reader_cls = getattr(c2pa, "Reader", None)
        if reader_cls is not None:
            try:
                reader = reader_cls(mime, io.BytesIO(image_bytes))
                manifest_json = reader.json()
                close = getattr(reader, "close", None)
                if close:
                    close()
            except TypeError:
                manifest_json = None
        if manifest_json is None and hasattr(c2pa, "read_file"):      # ancienne API (fichier)
            import tempfile
            ext = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp"}.get(mime, ".bin")
            with tempfile.NamedTemporaryFile(suffix=ext, delete=True) as tmp:
                tmp.write(image_bytes)
                tmp.flush()
                manifest_json = c2pa.read_file(tmp.name, None)
    except Exception as e:
        # « No manifest » / format non supporté : cas normal pour la plupart des images
        msg = str(e)
        if "ManifestNotFound" in type(e).__name__ or "no claim" in msg.lower() or "jumbf" in msg.lower() \
                or "manifest" in msg.lower() and "not" in msg.lower():
            log.debug("Pas de manifeste C2PA : %s", msg)
            return [], []
        return [], ["Lecture C2PA impossible : %s" % msg]

    if not manifest_json:
        return [], []

    hits = []
    try:
        data = json.loads(manifest_json) if isinstance(manifest_json, (str, bytes)) else manifest_json
    except Exception:
        data = None
    flat = json.dumps(data, ensure_ascii=False).lower() if data is not None else str(manifest_json).lower()

    for st in _AI_SOURCE_TYPES:
        if st in flat:
            hits.append("C2PA : digitalSourceType = %s" % st)
            break
    for rx, label in _AI_KEYWORDS_RE:
        if label in ("IPTC DigitalSourceType (média algorithmique)", "mention « AI generated »"):
            continue
        # on ne cherche les noms de générateurs que dans claim_generator / actions / signataire
        if rx.search(flat) and any(k in flat for k in ("claim_generator", "softwareagent", "issuer", "created")):
            hits.append("C2PA : générateur %s" % label)
    return hits, []


def analyze_local_metadata(image_bytes):
    """Étape 1. Retourne (preuves, erreurs non bloquantes)."""
    evidence, errors = [], []
    c2pa_hits, c2pa_errors = _read_c2pa(image_bytes)
    evidence += c2pa_hits
    errors += c2pa_errors
    evidence += _read_exif(image_bytes)
    evidence += _read_pillow_metadata(image_bytes)
    # dédoublonnage en conservant l'ordre
    seen, unique = set(), []
    for e in evidence:
        if e not in seen:
            seen.add(e)
            unique.append(e)
    return unique, errors


# ==========================================================================
# ÉTAPE 2 — OPENAI
# ==========================================================================

def check_openai_provenance(image_bytes, image_url=""):
    """Étape 2 : OpenAI Content Provenance (client.content_provenance_checks.create).

    Retourne (is_ai: bool|None, détail, erreur|None). Chaque entrée de `results`
    (type "c2pa" ou "synthid") a un `outcome` : "detected" / "not_detected".
    Un seul "detected" suffit à conclure à une origine IA.
    """
    if not OPENAI_API_KEY:
        return None, "étape OpenAI ignorée (OPENAI_API_KEY absente)", None
    if OpenAI is None:
        return None, "", "Paquet « openai » non installé (pip install openai) : étape 2 ignorée"
    try:
        client = OpenAI(api_key=OPENAI_API_KEY, timeout=HTTP_TIMEOUT)
        result = client.content_provenance_checks.create(
            file=("image", io.BytesIO(image_bytes), _guess_mime(image_bytes)),
        )
        return _parse_openai_response(result)
    except AttributeError:
        return None, "", "Version du paquet « openai » trop ancienne (content_provenance_checks absent) : pip install -U openai"
    except Exception as e:
        return None, "", "API OpenAI en erreur : %s" % e


def _parse_openai_response(result):
    """Accepte l'objet du SDK ou un dict."""
    if hasattr(result, "model_dump"):
        result = result.model_dump()
    detected, seen = [], []
    for r in (result or {}).get("results", []) or []:
        kind, outcome = r.get("type", "?"), r.get("outcome", "?")
        seen.append("%s=%s" % (kind, outcome))
        if outcome == "detected":
            extra = ", ".join(str(r[k]) for k in ("issuer", "model", "validation_state") if r.get(k))
            detected.append("OpenAI %s détecté%s" % (kind, " (%s)" % extra if extra else ""))
    if detected:
        return True, "; ".join(detected), None
    if not seen:
        return None, "", "Réponse OpenAI sans résultats"
    return False, "OpenAI : " + ", ".join(seen), None


# ==========================================================================
# ÉTAPE 3 — SIGHTENGINE (genai + modération)
# ==========================================================================

def _parse_moderation(data):
    """Extrait les signalements de modération d'une réponse SightEngine."""
    mod = ModerationResult()
    pct = lambda v: float(v) * 100.0

    nud = data.get("nudity") or {}
    nudity_score = max(
        [pct(nud.get(k, 0) or 0) for k in
         ("sexual_activity", "sexual_display", "erotica")] or [0.0])
    mod.scores["nudité"] = nudity_score
    if nudity_score >= MODERATION_THRESHOLD:
        mod.reasons.append("nudité/contenu sexuel %.0f %%" % nudity_score)

    off = data.get("offensive") or {}
    off_score = max([pct(v) for v in off.values() if isinstance(v, (int, float))] or [0.0])
    mod.scores["offensant"] = off_score
    if off_score >= MODERATION_THRESHOLD:
        mod.reasons.append("symboles/gestes offensants %.0f %%" % off_score)

    gore = data.get("gore") or {}
    gore_score = pct(gore.get("prob", 0) or 0) if isinstance(gore.get("prob", 0), (int, float)) else 0.0
    mod.scores["gore"] = gore_score
    if gore_score >= MODERATION_THRESHOLD:
        mod.reasons.append("violence/gore %.0f %%" % gore_score)

    mod.flagged = bool(mod.reasons)
    return mod


def _sightengine_call(image_bytes, models):
    """Appel SightEngine (upload du fichier). Retourne (json|None, erreur|None)."""
    if not SIGHTENGINE_API_USER or not SIGHTENGINE_API_SECRET:
        return None, "Identifiants SightEngine absents (SIGHTENGINE_API_USER / SIGHTENGINE_API_SECRET)"
    try:
        resp = requests.post(
            SIGHTENGINE_URL,
            files={"media": ("image", image_bytes, _guess_mime(image_bytes))},
            data={"models": models, "api_user": SIGHTENGINE_API_USER,
                  "api_secret": SIGHTENGINE_API_SECRET},
            headers={"User-Agent": USER_AGENT},
            timeout=HTTP_TIMEOUT,
        )
        resp.raise_for_status()
        data = resp.json()
        if data.get("status") != "success":
            err = data.get("error", {})
            return None, "SightEngine : %s" % (err.get("message") or data)
        return data, None
    except requests.exceptions.HTTPError as e:
        code = e.response.status_code if e.response is not None else "?"
        return None, "Erreur HTTP SightEngine (%s)%s" % (
            code, " : identifiants invalides" if code in (401, 403) else "")
    except requests.exceptions.RequestException as e:
        return None, "SightEngine injoignable : %s" % e
    except Exception as e:
        return None, "Réponse SightEngine inattendue : %s" % e


def check_sightengine(image_bytes):
    """Étape 3. Retourne (ai_score % | None, ModerationResult | None, erreur | None)."""
    data, err = _sightengine_call(image_bytes, SIGHTENGINE_MODELS)
    if data is None:
        return None, None, err
    ai = (data.get("type") or {}).get("ai_generated")
    ai_score = float(ai) * 100.0 if isinstance(ai, (int, float)) else None
    return ai_score, _parse_moderation(data), None


def check_moderation_only(image_bytes):
    data, err = _sightengine_call(image_bytes, SIGHTENGINE_MODERATION_MODELS)
    return (_parse_moderation(data) if data else None), err


# ==========================================================================
# PIPELINE
# ==========================================================================

def download_image(url):
    """Télécharge l'image. Essaie d'abord la session pywikibot (même User-Agent et
    mêmes réglages que le reste du bot : certains serveurs de médias renvoient 403
    aux clients inconnus), puis requests en dernier recours."""
    last_err = None
    if pywikibot is not None:
        try:
            from pywikibot.comms import http as pwb_http
            resp = pwb_http.fetch(url)
            status = getattr(resp, "status_code", getattr(resp, "status", 200))
            if status == 200 and resp.content:
                if len(resp.content) > MAX_IMAGE_BYTES:
                    raise ValueError("image trop volumineuse (> %d Mo)" % (MAX_IMAGE_BYTES // 1024 // 1024))
                return resp.content
            last_err = "HTTP %s via pywikibot" % status
        except ValueError:
            raise
        except Exception as e:
            last_err = "pywikibot : %s" % e
        log.warning("Téléchargement via pywikibot impossible (%s), essai avec requests.", last_err)

    resp = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=HTTP_TIMEOUT, stream=True)
    resp.raise_for_status()
    buf = io.BytesIO()
    for chunk in resp.iter_content(65536):
        buf.write(chunk)
        if buf.tell() > MAX_IMAGE_BYTES:
            raise ValueError("image trop volumineuse (> %d Mo)" % (MAX_IMAGE_BYTES // 1024 // 1024))
    return buf.getvalue()


def detect_ai_image(image_bytes, image_url=""):
    """Exécute le pipeline à 3 étapes sur des octets d'image. Ne lève jamais."""
    result = DetectionResult()

    # ---- Étape 1 : local ----
    try:
        evidence, errors = analyze_local_metadata(image_bytes)
        result.errors += errors
        if evidence:
            result.is_ai, result.stage = True, 1
            result.source = "métadonnées locales (EXIF/PNG/C2PA)"
            result.evidence = evidence
            log.info("Étape 1 : preuve d'IA trouvée -> arrêt. %s", "; ".join(evidence))
            if MODERATE_ALWAYS:
                result.moderation, merr = check_moderation_only(image_bytes)
                if merr:
                    result.errors.append(merr)
            return result
        log.info("Étape 1 : aucune preuve dans les métadonnées.")
    except Exception as e:
        result.errors.append("Étape 1 en erreur : %s" % e)
        log.exception("Étape 1 en erreur")

    # ---- Étape 2 : OpenAI ----
    try:
        is_ai, detail, err = check_openai_provenance(image_bytes, image_url)
        if err:
            result.errors.append(err)
            log.warning("Étape 2 : %s", err)
        elif is_ai:
            result.is_ai, result.stage = True, 2
            result.source = "API OpenAI (provenance)"
            result.evidence = [detail]
            log.info("Étape 2 : origine IA confirmée par OpenAI -> arrêt.")
            if MODERATE_ALWAYS:
                result.moderation, merr = check_moderation_only(image_bytes)
                if merr:
                    result.errors.append(merr)
            return result
        else:
            log.info("Étape 2 : %s", detail or "OpenAI ne confirme pas l'origine IA.")
    except Exception as e:
        result.errors.append("Étape 2 en erreur : %s" % e)
        log.exception("Étape 2 en erreur")

    # ---- Étape 3 : SightEngine ----
    try:
        ai_score, moderation, err = check_sightengine(image_bytes)
        result.moderation = moderation
        if err:
            result.errors.append(err)
            log.error("Étape 3 : %s", err)
            return result                       # indéterminé (is_ai reste None)
        result.stage, result.ai_score = 3, ai_score
        if ai_score is None:
            result.errors.append("SightEngine n'a pas renvoyé de score ai_generated")
            return result
        result.is_ai = ai_score >= AI_SCORE_THRESHOLD
        result.source = "SightEngine genai (%.1f %%)" % ai_score
        log.info("Étape 3 : score ai_generated = %.1f %% (seuil %.1f %%)", ai_score, AI_SCORE_THRESHOLD)
    except Exception as e:
        result.errors.append("Étape 3 en erreur : %s" % e)
        log.exception("Étape 3 en erreur")
    return result


# ==========================================================================
# DISCORD
# ==========================================================================

def send_discord_embed(title, description, color, fields=None, thumbnail_url=None):
    """Envoie un embed via discord_logger (pas de webhook dédié). Ne lève jamais."""
    embed = {
        "title": title[:256], "description": description[:4000], "color": color,
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "footer": {"text": "ai_detector · pipeline 3 étapes"},
    }
    if fields:
        embed["fields"] = [{"name": n, "value": str(v)[:1000], "inline": i} for n, v, i in fields]
    if thumbnail_url:
        embed["thumbnail"] = {"url": thumbnail_url}
    try:
        if not discord_logger.send_embed(embed):
            log.debug("discord_logger non installé : embed non envoyé.")
    except Exception as e:
        log.warning("Embed Discord : envoi impossible : %s", e)


# ==========================================================================
# INTÉGRATION WIKI
# ==========================================================================

def _has_template(text, name):
    return re.search(r"\{\{\s*" + re.escape(name.strip("{}")).replace(r"\ ", r"[ _]") + r"\s*[|}]",
                     text, re.I) is not None


def _has_ai_si_banner(text):
    """Détecte {{SI|Image IA ...}} (avec ou sans pourcentage)."""
    return re.search(r"\{\{\s*SI\s*\|\s*Image[ _]IA", text, re.I) is not None


def process_file_page(site, page_title, dry_run=False):
    try:
        file_page = pywikibot.FilePage(site, page_title)
        if not file_page.exists():
            log.error("La page %s n'existe pas.", page_title)
            return
        text = file_page.text
        already_ai = _has_template(text, "image IA") or _has_ai_si_banner(text)
        mod_name = MODERATION_TEMPLATE.strip("{}").split("|")[0] if MODERATION_TEMPLATE else ""
        already_mod = bool(mod_name) and _has_template(text, mod_name)

        if already_ai and (already_mod or not MODERATION_TEMPLATE):
            log.info("%s : déjà balisé, passage.", page_title)
            return

        image_url = file_page.get_file_url()
        page_url = file_page.full_url()
        log.info("Analyse de %s", page_title)
        image_bytes = download_image(image_url)

        res = detect_ai_image(image_bytes, image_url)
        for e in res.errors:
            log.warning("  [%s] %s", page_title, e)

        if res.is_ai is None:
            send_discord_embed("⚠️ Analyse impossible",
                               "[**%s**](%s)\n%s" % (page_title, page_url, "\n".join(res.errors)[:1500]),
                               EMBED_COLOR_ERROR, thumbnail_url=image_url)
            return

        new_text = text
        summaries = []
        fields = [("Étape décisive", str(res.stage), True), ("Source", res.source, True)]
        if res.ai_score is not None:
            fields.append(("Score ai_generated", "%.1f %%" % res.ai_score, True))
        if res.evidence:
            fields.append(("Preuves", "\n".join(res.evidence)[:900], False))

        if res.is_ai and not already_ai:
            new_text = build_ai_tag(res.ai_score) + new_text
            summaries.append(EDIT_SUMMARY_AI % res.source)

        mod = res.moderation
        if mod and mod.flagged:
            fields.append(("Modération", ", ".join(mod.reasons), False))
            if MODERATION_TEMPLATE and not already_mod:
                new_text = MODERATION_TEMPLATE + "\n" + new_text
                summaries.append(EDIT_SUMMARY_MODERATION % ", ".join(mod.reasons))

        if mod and mod.flagged:
            title, color = "🚩 Contenu potentiellement inapproprié", EMBED_COLOR_MODERATION
        elif res.is_ai:
            title, color = "🚨 Image IA détectée", EMBED_COLOR_AI_DETECTED
        else:
            title, color = "✅ Image jugée saine", EMBED_COLOR_CLEAN
        if dry_run:
            title, color = "🧪 [DRY-RUN] " + title, EMBED_COLOR_DRYRUN

        if new_text != text:
            if dry_run:
                log.info("[DRY-RUN] %s : aurait été modifié (%s)", page_title, " + ".join(summaries))
            else:
                file_page.text = new_text
                file_page.save(summary=" ; ".join(summaries), minor=False, bot=True)
                log.info("✓ %s modifié (%s)", page_title, " + ".join(summaries))
        send_discord_embed(title, "[**%s**](%s)" % (page_title, page_url), color,
                           fields=fields, thumbnail_url=image_url)

    except Exception as e:
        log.error("Erreur lors du traitement de %s : %s", page_title, e)
        send_discord_embed("⚠️ Erreur lors de l'analyse",
                           "**%s**\n```%s```" % (page_title, str(e)[:500]), EMBED_COLOR_ERROR)


AI_STATE_FILE = "ai_detector_state.txt"     # état PROPRE à ce détecteur (voir note ci-dessous)


def _load_ai_state():
    try:
        with open(AI_STATE_FILE, "r") as f:
            return pywikibot.Timestamp.fromISOformat(f.read().strip())
    except (FileNotFoundError, ValueError):
        return None


def _save_ai_state(ts):
    try:
        with open(AI_STATE_FILE, "w") as f:
            f.write(ts.isoformat())
    except OSError as e:
        log.warning("Impossible d'écrire %s : %s", AI_STATE_FILE, e)


def watch_uploads_for_ai(site, dry_run=False):
    """Sonde le journal des téléversements.

    On n'utilise PAS l'heure partagée bot_state.txt : les deux autres bots la font
    avancer à chaque cycle, ce qui faisait « sauter » à ce détecteur les fichiers
    téléversés entre deux de ses passages. Il garde donc son propre curseur
    (ai_detector_state.txt), mis à jour uniquement après traitement.
    """
    last_timestamp = _load_ai_state()
    if last_timestamp is not None:
        log.info("Reprise depuis le dernier téléversement traité (%s UTC).", last_timestamp.isoformat())
    else:
        last_timestamp = pywikibot.Timestamp.now()
        _save_ai_state(last_timestamp)
        log.info("Premier lancement : surveillance à partir de maintenant (%s UTC).",
                 last_timestamp.isoformat())
    last_logid = None
    log.info("Sondage du journal des téléversements toutes les %d s.", POLL_INTERVAL)

    while True:
        count = 0
        try:
            for event in list(site.logevents(logtype="upload", start=last_timestamp, reverse=True)):
                try:
                    logid = event.logid()
                except Exception:
                    logid = None
                if last_logid is not None and logid == last_logid:
                    continue
                count += 1
                try:
                    process_file_page(site, event.page().title(), dry_run=dry_run)
                except Exception as e:
                    log.error("Erreur sur l'événement de téléversement : %s", e)
                last_timestamp, last_logid = event.timestamp(), logid
                _save_ai_state(last_timestamp)
        except Exception as e:
            log.error("Erreur dans la boucle de surveillance IA : %s", e)
        log.info("Sondage terminé : %d téléversement(s) examiné(s). Prochain passage dans %d s.",
                 count, POLL_INTERVAL)
        time.sleep(POLL_INTERVAL)


def _log_config():
    log.info("Config : c2pa-python=%s exifread=%s Pillow=%s | OpenAI=%s | SightEngine=%s",
             "oui" if c2pa else "NON", "oui" if exifread else "NON", "oui" if Image else "NON",
             ("configuré" if OPENAI_API_KEY else "non configuré (étape ignorée)") + ("" if OpenAI else " [paquet openai absent]"),
             "configuré" if SIGHTENGINE_API_USER and SIGHTENGINE_API_SECRET else "NON CONFIGURÉ")


def main():
    parser = argparse.ArgumentParser(description="Détection d'images IA (pipeline à 3 étapes).")
    parser.add_argument("positional_file", nargs="?", default=None, help="Fichier wiki à tester")
    parser.add_argument("--file", "-f", dest="option_file", default=None)
    parser.add_argument("--local", dest="local_path", default=None,
                        help="Analyser un fichier image local (sans toucher au wiki)")
    parser.add_argument("--dry-run", "--dry-oui", action="store_true", dest="dry_run")
    args = parser.parse_args()

    if not logging.getLogger().handlers:
        logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s",
                            stream=sys.stdout)
    if discord_logger._handler is None:      # lancement autonome (hors main.py)
        discord_logger.install()
    _log_config()

    if args.local_path:
        with open(args.local_path, "rb") as f:
            res = detect_ai_image(f.read())
        print(json.dumps(res.__dict__, default=lambda o: o.__dict__, ensure_ascii=False, indent=2))
        return

    site = pywikibot.Site()  # code/famille : user-config.py (mylang, family)
    target = args.option_file or args.positional_file
    if target:
        process_file_page(site, target, dry_run=args.dry_run)
    else:
        watch_uploads_for_ai(site, dry_run=args.dry_run)


if __name__ == "__main__":
    main()