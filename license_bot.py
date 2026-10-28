#!/usr/bin/env python3
"""
BotJanus — surveillance continue des nouveaux téléversements.

Vikidia n'étant pas un wiki Wikimedia, il n'y a pas d'accès à EventStreams
(stream.wikimedia.org, réservé aux projets WMF). On sonde donc périodiquement le
journal des téléversements (action=query&list=logevents&letype=upload) via l'API,
et on lance l'analyse de licence sur chaque nouveau fichier détecté.

Logique d'analyse (par fichier) :
  1. Templates réellement transclus sur la page (résolus par MediaWiki, alias compris)
     comparés à la liste des modèles listés récursivement dans Catégorie:Modèle licence.
  2. Licence reconnue trouvée => rien à faire.
  3. Aucune licence reconnue => on ajoute {{LI}} en haut de la page, et 
     on ajoute une section "== Licence manquante ==" avec {{subst:Image oubli|nom_du_fichier}} sur la page de
     discussion du téléverseur.

Toutes les actions sont idempotentes (vérification avant écriture), donc un même
événement retraité par erreur (redémarrage du bot, chevauchement de fenêtre de sondage)
ne produit pas de doublon.
"""

import logging
import sys
import time
import pywikibot

import bot_state

# ==========================================================================
# CONFIGURATION
# ==========================================================================

DRY_RUN = False               # True = simulation, aucune écriture sur le wiki
POLL_INTERVAL = bot_state.POLL_INTERVAL            # secondes entre deux sondages du journal des téléversements
PROCESS_BACKLOG = False       # False = ne traite que les téléversements à partir du démarrage
                               # True = traite aussi les téléversements déjà existants (attention,
                               # peut représenter énormément de pages au premier lancement),
                               # ignoré si un état partagé (bot_state.txt) existe déjà

LICENSE_MODELS_CATEGORY = "Modèle licence"               # Catégorie:Modèle licence
UNKNOWN_LICENSE_TEMPLATES = {"Licence inconnue", "LI"}    # marqueurs "pas de licence", exclus

LI_TEMPLATE_TEXT = "{{LI}}\n"

TALK_SECTION_TITLE = "Licence manquante"
TALK_MESSAGE = "{{subst:Image oubli|%s}} ~~~~"

EDIT_SUMMARY_FILE = "Bot : pose de {{LI}} — aucune licence reconnue détectée sur ce fichier"
EDIT_SUMMARY_TALK = "Bot : notification — licence manquante sur un fichier téléversé"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("botjanus")


# ==========================================================================
# DÉTECTION DE LICENCE
# ==========================================================================

def get_known_license_templates(site):
    """Ensemble des titres (sans espace de noms) de tous les modèles listés,
    récursivement, dans Catégorie:Modèle licence — hors marqueurs 'licence inconnue'."""
    category = pywikibot.Category(site, LICENSE_MODELS_CATEGORY)
    members = category.members(namespaces=[10], recurse=True)  # 10 = Modèle/Template
    known = {page.title(with_ns=False) for page in members}

    return known - UNKNOWN_LICENSE_TEMPLATES


def page_has_recognized_license(page, known_license_templates):
    try:
        templates = list(page.itertemplates())
    except Exception as e:
        log.warning("  Erreur lors de la récupération des templates de %s : %s", page.title(), e)
        return False, set()

    template_titles = {t.title(with_ns=False) for t in templates}
    found = template_titles & known_license_templates
    return bool(found), template_titles


def page_already_tagged_li(template_titles):
    return bool(template_titles & UNKNOWN_LICENSE_TEMPLATES)


def add_li_banner(page, dry_run):
    text = page.text
    new_text = LI_TEMPLATE_TEXT + text

    if dry_run:
        log.info("  -> [DRY-RUN] aurait ajouté {{LI}} en haut de %s", page.title())
        return

    page.text = new_text
    page.save(summary=EDIT_SUMMARY_FILE, minor=False, bot=False)
    log.info("  -> {{LI}} ajouté avec succès sur %s", page.title())


def notify_uploader(site, page, dry_run):
    try:
        uploader_name = page.oldest_file_info.user
    except Exception as e:
        log.warning("  Impossible de déterminer le téléverseur de %s : %s", page.title(), e)
        return

    user = pywikibot.User(site, uploader_name)
    talk_page = user.getUserTalkPage()

    try:
        talk_text = talk_page.text if talk_page.exists() else ""
    except Exception as e:
        log.warning("  Erreur de lecture de la page de discussion de %s : %s", uploader_name, e)
        return

    # Récupération du titre du fichier
    file_title_without_ns = page.title(with_ns=False)
    
    # Vérification : si le fichier est déjà cité dans la PDD, on n'ajoute rien
    if file_title_without_ns in talk_text:
        log.info("  -> notification pour %s déjà présente chez %s, on ignore.", file_title_without_ns, uploader_name)
        return

    section_header = "== %s ==" % TALK_SECTION_TITLE
    formatted_talk_message = TALK_MESSAGE % file_title_without_ns

    # Construction du bloc à ajouter en fin de page
    new_block = "%s\n%s" % (section_header, formatted_talk_message)

    separator = "\n\n" if talk_text.strip() else ""
    new_talk_text = "%s%s%s\n" % (talk_text.strip(), separator, new_block)

    if dry_run:
        log.info("  -> [DRY-RUN] aurait ajouté l'avertissement pour %s chez %s", file_title_without_ns, uploader_name)
        return

    talk_page.text = new_talk_text
    talk_page.save(summary=EDIT_SUMMARY_TALK, minor=False, bot=False)
    log.info("  -> avertissement pour %s ajouté chez %s", file_title_without_ns, uploader_name)


def process_file(site, page, known_license_templates, dry_run):
    log.info("Analyse de %s", page.title())

    if not page.exists():
        log.info("  -> la page n'existe pas (déjà supprimée ?), on ignore.")
        return

    has_license, template_titles = page_has_recognized_license(page, known_license_templates)

    if has_license:
        log.info("  -> licence détectée (%s), rien à faire.", template_titles & known_license_templates)
        return

    if page_already_tagged_li(template_titles):
        log.info("  -> déjà taggé {{LI}}, on ne repose pas le bandeau.")
    else:
        add_li_banner(page, dry_run)

    notify_uploader(site, page, dry_run)


# ==========================================================================
# SURVEILLANCE DU JOURNAL DES TÉLÉVERSEMENTS
# ==========================================================================

def fetch_new_uploads(site, since_timestamp):
    """Renvoie la liste des événements de téléversement depuis `since_timestamp` (inclus),
    du plus ancien au plus récent."""
    return list(site.logevents(logtype="upload", start=since_timestamp, reverse=True))


_seen_logids = set()


def run_cycle(site, since, known_license_templates, dry_run=DRY_RUN):
    """Un passage : traite les téléversements depuis `since`.
    Renvoie True si le journal a pu être lu (le cycle compte comme réussi)."""
    try:
        events = fetch_new_uploads(site, since)
    except Exception as e:
        log.error("Erreur lors du sondage du journal des téléversements : %s", e)
        return False

    for event in events:
        try:
            logid = event.logid()
        except Exception:
            logid = None

        # La fenêtre se chevauche : on ignore les événements déjà traités.
        if logid is not None and logid in _seen_logids:
            continue

        try:
            page = event.page()
        except Exception as e:
            log.warning("Impossible de récupérer la page associée à l'événement : %s", e)
            continue

        try:
            process_file(site, page, known_license_templates, dry_run=dry_run)
        except Exception as e:
            log.error("Erreur inattendue lors du traitement de %s : %s", page.title(), e)

        if logid is not None:
            _seen_logids.add(logid)

    if len(_seen_logids) > 5000:
        _seen_logids.clear()
    return True


def watch_uploads(site, known_license_templates, dry_run):
    """Mode autonome (hors main.py) : même cycle de 10 min, état partagé."""
    since = bot_state.load_last_check() or pywikibot.Timestamp.utcnow()
    log.info("Bot démarré, sondage toutes les %d secondes (depuis %s UTC).",
             POLL_INTERVAL, since.isoformat())
    while True:
        start = pywikibot.Timestamp.utcnow()
        if run_cycle(site, since, known_license_templates, dry_run):
            since = bot_state.next_since(start)
            bot_state.update_last_check(since)
        time.sleep(POLL_INTERVAL)


def main():
    if DRY_RUN:
        log.info("=== MODE DRY-RUN : aucune modification ne sera écrite sur le wiki ===")

    site = pywikibot.Site("fr", "vikidia")
    site.login()

    log.info("Récupération des modèles de licence reconnus (Catégorie:Modèle licence)...")
    known_license_templates = get_known_license_templates(site)
    log.info("%d modèle(s) de licence reconnu(s).", len(known_license_templates))

    try:
        watch_uploads(site, known_license_templates, dry_run=DRY_RUN)
    except KeyboardInterrupt:
        log.info("Arrêt demandé (Ctrl+C), fin du bot.")


if __name__ == "__main__":
    main()
