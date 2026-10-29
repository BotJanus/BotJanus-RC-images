#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
config_check — contrôle de configuration des 3 scripts, au démarrage.

Vérifie que tout est *chargé* (modules, clés, listes) ET *utilisable* (connexion au wiki,
droits, journaux lisibles, Common joignable, clé OpenAI acceptée...), puis publie le
résultat à deux endroits, identiques pour main.py et les modes autonomes :

  * dans les logs (une ligne par section ; les problèmes en WARNING, jamais en ERROR : un
    problème de configuration ne doit pas faire passer le service en « Dégradé » dans le
    dashboard, l'embed Discord est là pour ça) ;
  * dans un embed Discord : vert = tout est bon, orange = avertissements, rouge = erreurs.

Lancer seulement le contrôle :   python main.py --check
"""

import logging
from dataclasses import dataclass, field
from typing import List

import bot_clock
import bot_config
import bot_state
import discord_logger

log = logging.getLogger("config")

OK, WARN, ERR = "ok", "warn", "error"
ICON = {OK: "✅", WARN: "⚠️", ERR: "❌"}
COLOR = {OK: 0x2ECC71, WARN: 0xE67E22, ERR: 0xE74C3C}
_RANK = {OK: 0, WARN: 1, ERR: 2}

SECTION_TITLES = {
    "common": "🔧 Commun",
    "license": "📜 license_bot — licences",
    "cleanup": "🧹 image_cleanup_bot — nettoyage",
    "ai": "🤖 ai_detector — analyse IA",
}


@dataclass
class Check:
    status: str
    label: str
    detail: str = ""

    def line(self):
        return "%s %s%s" % (ICON[self.status], self.label, " — " + self.detail if self.detail else "")


@dataclass
class Report:
    sections: List[tuple] = field(default_factory=list)      # [(clé, [Check])]

    @property
    def status(self):
        worst = OK
        for _key, checks in self.sections:
            for c in checks:
                if _RANK[c.status] > _RANK[worst]:
                    worst = c.status
        return worst

    def count(self, status):
        return sum(1 for _k, checks in self.sections for c in checks if c.status == status)

    def headline(self):
        if self.status == OK:
            return "tout est chargé et utilisable"
        return "%d erreur(s), %d avertissement(s)" % (self.count(ERR), self.count(WARN))


def _guard(label, fn):
    """Exécute un contrôle ; une exception devient une ❌ au lieu de faire planter le démarrage."""
    try:
        result = fn()
        return result if isinstance(result, list) else [result]
    except Exception as e:                                    # noqa: BLE001
        return [Check(ERR, label, "contrôle impossible : %s" % e)]


# --------------------------------------------------------------------------
# Contrôles
# --------------------------------------------------------------------------

def _common_checks(site):
    checks = [Check(OK, "Horloge", "UTC, %s" % bot_clock.iso())]

    since = bot_state.load_last_check()
    writable = bot_state.check_writable()
    checks.append(Check(
        OK if writable else ERR, "État partagé",
        ("reprise depuis %s" % bot_clock.iso(since)) if since and writable else
        ("premier lancement (départ de maintenant)" if writable else
         "%s non inscriptible : heartbeat et reprise impossibles" % bot_state.STATE_FILE)))

    if site is None:
        checks.append(Check(WARN, "Wiki", "mode local : aucune connexion"))
    else:
        if site.logged_in():
            checks.append(Check(OK, "Connexion wiki", "%s sur %s" % (site.user(), site.hostname())))
            checks.append(Check(OK if site.has_right("edit") else ERR, "Droit d'édition"))
            checks.append(Check(OK if site.has_group("bot") else WARN, "Drapeau bot",
                                "" if site.has_group("bot") else "absent : les modifs bot=True apparaîtront dans les RC"))
        else:
            checks.append(Check(ERR, "Connexion wiki", "non connecté"))

    mode = discord_logger.mode()
    checks.append(Check(OK if mode else WARN, "Discord",
                        {"bot": "compte bot", "webhook": "webhook"}.get(mode) or
                        "logs et embeds désactivés (DISCORD_BOT_TOKEN + DISCORD_LOG_CHANNEL_ID, ou DISCORD_WEBHOOK_URL)"))
    env_files = bot_config.load_env()
    checks.append(Check(OK if env_files else WARN, "Fichier d'environnement",
                        ", ".join(p.rsplit("/", 1)[-1] for p in env_files) if env_files else
                        "aucun .env trouvé (variables système uniquement)"))
    return checks


def _license_checks(site):
    import license_bot
    import pywikibot

    checks = []
    if license_bot.DRY_RUN:
        checks.append(Check(WARN, "Mode", "DRY_RUN actif : aucune écriture sur le wiki"))
    if site is None:
        return checks + [Check(WARN, "Modèles de licence", "non vérifiés (pas de wiki)")]

    known = license_bot.known_licenses(site)
    checks.append(Check(OK if known else ERR, "Modèles de licence",
                        "%d reconnus (Catégorie:%s)" % (len(known), license_bot.LICENSE_MODELS_CATEGORY) if known else
                        "Catégorie:%s vide ou illisible : le script refusera de tourner" % license_bot.LICENSE_MODELS_CATEGORY))
    for name in ("LI", "Image oubli"):
        exists = pywikibot.Page(site, name, ns=10).exists()
        checks.append(Check(OK if exists else ERR, "Modèle {{%s}}" % name, "" if exists else "introuvable sur le wiki"))
    return checks


def _cleanup_checks(site):
    import image_cleanup_bot
    from common_repo import CommonError

    checks = []
    if image_cleanup_bot.DRY_RUN:
        checks.append(Check(WARN, "Mode", "DRY_RUN actif : aucune écriture sur le wiki"))
    if site is None:
        return checks + [Check(WARN, "Journal de suppression", "non vérifié (pas de wiki)")]

    try:
        next(iter(site.logevents(logtype="delete", namespace=image_cleanup_bot.FILE_NAMESPACE, total=1)), None)
        checks.append(Check(OK, "Journal de suppression", "lisible"))
    except Exception as e:                                    # noqa: BLE001
        checks.append(Check(ERR, "Journal de suppression", "illisible : %s" % e))

    repo = image_cleanup_bot.get_common(site)
    try:
        api = repo.probe()
        if api:
            checks.append(Check(OK, "Common", "joignable (%s), similarité ≥ %d %%" % (api, round(repo.threshold * 100))))
        else:
            checks.append(Check(WARN, "Common", "aucun dépôt détecté : images similaires NON vérifiées "
                                                "(définir COMMONS_API)"))
    except CommonError as e:
        checks.append(Check(ERR, "Common", "injoignable (%s) : aucune image ne sera retirée des articles tant que ça dure" % e))
    return checks


def _openai_check(ai):
    if not ai.OPENAI_API_KEY:
        return Check(WARN, "OpenAI (étape 2)", "OPENAI_API_KEY absente : étape ignorée")
    if ai.OpenAI is None:
        return Check(WARN, "OpenAI (étape 2)", "paquet « openai » absent : étape ignorée")
    client = ai.OpenAI(api_key=ai.OPENAI_API_KEY, timeout=10)
    if not hasattr(client, "content_provenance_checks"):
        return Check(ERR, "OpenAI (étape 2)", "paquet trop ancien (pip install -U openai)")
    try:
        client.models.list()
    except Exception as e:                                    # noqa: BLE001
        name = type(e).__name__
        if "Authentication" in name or "PermissionDenied" in name:
            return Check(ERR, "OpenAI (étape 2)", "clé refusée par OpenAI")
        return Check(WARN, "OpenAI (étape 2)", "clé présente mais API non vérifiable (%s)" % name)
    return Check(OK, "OpenAI (étape 2)", "clé acceptée, contrôle de provenance disponible")


def _ai_checks(_site):
    import ai_detector as ai

    checks = []
    libs = (("c2pa-python", ai.c2pa, "manifestes C2PA ignorés"),
            ("ExifRead", ai.exifread, "EXIF ignoré"),
            ("Pillow", ai.Image, "métadonnées PNG/XMP ignorées"))
    missing = [(n, why) for n, mod, why in libs if mod is None]
    checks.append(Check(WARN if missing else OK, "Étape 1 (métadonnées)",
                        "; ".join("%s absent : %s" % m for m in missing) if missing else "c2pa-python, ExifRead, Pillow chargés"))
    checks.append(_openai_check(ai))

    if ai.SIGHTENGINE_API_USER and ai.SIGHTENGINE_API_SECRET:
        checks.append(Check(OK, "SightEngine (étape 3)", "identifiants présents (non testés, pour ne pas consommer de quota)"))
    else:
        checks.append(Check(ERR, "SightEngine (étape 3)",
                            "SIGHTENGINE_API_USER / SIGHTENGINE_API_SECRET absents : aucune image ne pourra être tranchée"))
    checks.append(Check(OK, "Seuils", "IA ≥ %g %%, modération ≥ %g %%, %s" % (
        ai.AI_SCORE_THRESHOLD, ai.MODERATION_THRESHOLD,
        "modèle %s" % ai.MODERATION_TEMPLATE if ai.MODERATION_TEMPLATE else "modération : alerte Discord seulement")))
    return checks


_BUILDERS = {"common": _common_checks, "license": _license_checks,
             "cleanup": _cleanup_checks, "ai": _ai_checks}


# --------------------------------------------------------------------------
# Rapport
# --------------------------------------------------------------------------

def collect(site, scripts=("license", "cleanup", "ai")):
    report = Report()
    for key in ("common",) + tuple(scripts):
        report.sections.append((key, _guard(SECTION_TITLES[key], lambda k=key: _BUILDERS[k](site))))
    return report


def log_report(report):
    log.info("Config : %s %s — %s", ICON[report.status], bot_config.BOT_NAME, report.headline())
    for key, checks in report.sections:
        for c in checks:
            level = logging.INFO if c.status == OK else logging.WARNING     # jamais ERROR (voir docstring)
            log.log(level, "Config [%s] %s", key, c.line())


def send_embed(report):
    fields = []
    for key, checks in report.sections:
        value, used = [], 0
        for c in checks:
            line = c.line()
            if used + len(line) + 1 > 980:
                value.append("…")
                break
            value.append(line)
            used += len(line) + 1
        fields.append((SECTION_TITLES[key], "\n".join(value), False))
    title = {OK: "⚙️ Configuration chargée", WARN: "⚠️ Configuration incomplète",
             ERR: "❌ Configuration en erreur"}[report.status]
    embed = discord_logger.build_embed(
        title, "**%s**\n%s\n\n%s %s" % (bot_config.BOT_NAME, bot_config.BOT_DESCRIPTION,
                                        ICON[report.status], report.headline()),
        COLOR[report.status], fields=fields, footer="config_check")
    return discord_logger.send_embed(embed)


def run(site, scripts=("license", "cleanup", "ai")):
    """Collecte, logue et envoie l'embed. Ne lève jamais. Renvoie le Report."""
    try:
        report = collect(site, scripts)
        log_report(report)
        send_embed(report)
        return report
    except Exception as e:                                    # noqa: BLE001
        log.warning("Contrôle de configuration impossible : %s", e)
        return Report()
