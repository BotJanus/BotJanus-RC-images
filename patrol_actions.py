#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
patrol_actions — ce qui est commun quand une image NON ADMISSIBLE est détectée
(ai_detector pour un téléversement, recent_files_bot pour un fichier ajouté à une page) :

  * request_hiding() : demande de masquage dans « Vikidia:Demandes aux administrateurs/AAAA MM » ;
  * forward_bulletin() : copie l'embed Discord vers le salon des patrouilleurs (BULLETIN_PATROUILLEURS).

BULLETIN_PATROUILLEURS (.env) : l'identifiant du salon (envoyé avec DISCORD_BOT_TOKEN),
ou l'URL d'un webhook. Vide = aucune copie.
"""

import logging

import pywikibot
import requests

import bot_clock
import bot_config

log = logging.getLogger("patrol-actions")

ADMIN_REQUESTS_PREFIX = "Vikidia:Demandes aux administrateurs/"
EDIT_SUMMARY_REQUEST = "Bot : demande de masquage (image non admissible détectée)"

ADMIN_REQUEST = """{{{{Demande admin|état=<!-- réservé aux administrateurs-->|contenu=
<!-- Écris ta demande en dessous de cette ligne. Pour faire un saut de ligne, utilise <br />. -->
Bonjour,

Mon analyse d'admissibilité a détecté une image non admissible {where} : {link}.

Serait-il possible de masquer au plus vite {what} ?

Merci d'avance <br />
~~~~<!-- Ne rien modifier sur cette ligne (signature automatique). -->
<!-- Réponse de l'administrateur ci-dessous. -->

<!-- Ne rien modifier en dessous de cette ligne. -->
}}}}"""


def api_edit(site, **params):
    params.update(action="edit", token=site.tokens["csrf"])
    return site.simple_request(**params).submit()


def admin_request_title():
    return ADMIN_REQUESTS_PREFIX + bot_clock.month_label()


def request_hiding(site, link, marker, dry_run, where="venant de Commons", what="cette modification"):
    """Dépose la demande en fin de page du mois. `link` : wikicode du lien (diff ou fichier) ;
    `marker` : texte qui, déjà présent dans la page, prouve que la demande existe (idempotence).
    Renvoie True si la demande a été ajoutée."""
    title = admin_request_title()
    page = pywikibot.Page(site, title)
    if page.exists() and marker in page.text:
        log.info("Demande déjà déposée (%s) sur %s.", marker, title)
        return False
    if dry_run:
        log.info("[DRY-RUN] aurait déposé la demande de masquage (%s) sur %s", marker, title)
        return True
    api_edit(site, title=title, appendtext="\n\n" + ADMIN_REQUEST.format(where=where, link=link, what=what),
             summary=EDIT_SUMMARY_REQUEST)
    log.info("✓ Demande de masquage déposée sur %s (%s)", title, marker)
    return True


def forward_bulletin(embed):
    """Copie l'embed vers BULLETIN_PATROUILLEURS (salon ou webhook), sans miniature (image
    inappropriée). Ne lève jamais. Renvoie True si envoyé."""
    target = bot_config.env_str("BULLETIN_PATROUILLEURS")
    if not target:
        return False
    embed = {k: v for k, v in embed.items() if k != "thumbnail"}
    try:
        if target.startswith("http"):
            resp = requests.post(target, json={"embeds": [embed]}, timeout=10)
        else:
            token = bot_config.env_str("DISCORD_BOT_TOKEN")
            if not token:
                log.warning("BULLETIN_PATROUILLEURS est un salon mais DISCORD_BOT_TOKEN est absent.")
                return False
            resp = requests.post(
                "https://discord.com/api/v10/channels/%s/messages" % target, json={"embeds": [embed]},
                headers={"Authorization": "Bot " + token, "User-Agent": "DiscordBot (BotJanus-RC-images, 1.0)"},
                timeout=10)
        resp.raise_for_status()
        return True
    except Exception as e:                                    # noqa: BLE001
        log.warning("Bulletin patrouilleurs : envoi impossible : %s", e)
        return False
