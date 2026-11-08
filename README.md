# BotJanus-RC-images

**BotJanus RC-images** — Surveillance des téléversements : licences, nettoyage des images supprimées et analyse IA.

(Nom et description uniques : `bot_config.py` → `BOT_NAME` / `BOT_DESCRIPTION`. Ce sont les mêmes
textes dans les logs Discord, les embeds, le dashboard et `BotJanus-agent/services.json`.)

## Les 4 scripts

Lancés ensemble par `main.py`, dans **un seul cycle commun toutes les 10 minutes**, avec une heure et un état partagés.

| Script | Rôle |
|---|---|
| `license_bot.py` | Pose `{{LI}}` sur les fichiers sans licence reconnue et prévient le téléverseur. La liste des licences est lue une seule fois, au démarrage, par le contrôle de configuration (jamais relue ensuite : redémarrer le bot pour la mettre à jour). |
| `image_cleanup_bot.py` | Retire des articles les images supprimées. **Avant de retirer quoi que ce soit, cherche une image similaire sur Common** (même nom, même contenu SHA-1, ou nom quasi identique) : si elle existe, rien n'est supprimé. Si Common est injoignable, rien n'est supprimé non plus et l'image est reprise au cycle suivant (3 essais). |
| `ai_detector.py` | Détection d'images IA en 3 étapes (métadonnées → OpenAI → SightEngine) + modération. |
| `recent_files_bot.py` | Lit les **modifications récentes** et repère les fichiers **ajoutés** à une page (`[[Fichier:`, `[[File:`, `[[Image:`, lignes de `<gallery>`). Fichier de **Vikidia** → mêmes contrôles qu'un téléversement (licence `{{LI}}`, IA, admissibilité). Fichier de **Common** → IA + admissibilité seulement ; si l'image n'est **pas admissible**, le bot **révoque la modification** (annulation, sinon retrait du fichier) et dépose une **demande de masquage** dans `Vikidia:Demandes aux administrateurs/AAAA MM`. Une image IA admissible sur Common ne déclenche qu'une alerte Discord. Image non admissible **sur Vikidia** : `{{SI}}`, révocation de la modification, demande de masquage. Chaque cas est copié vers `BULLETIN_PATROUILLEURS`. |

## Organisation du code

| Fichier | Rôle |
|---|---|
| `bot_clock.py` | **Seule** source de l'heure (UTC conscient, format ISO unique). Plus aucun `datetime.now` / `time.time` / `Timestamp.utcnow` ailleurs. |
| `bot_state.py` | `/data/rc-images/bot_state.txt` (JSON ; dossier réglable via `BOTJANUS_DATA_DIR`) : `since` (fenêtre de lecture) + `heartbeat` (battu toutes les 30 s par un thread). |
| `bot_config.py` | Nom/description, chargeur `.env` unique, logging. |
| `cycle.py` | Boucle commune (main.py **et** modes autonomes) + un seul appel API pour le journal des téléversements. |
| `file_cache.py` | Mémoire des images déjà analysées, Vikidia et Common : fichier texte `/data/rc-images/analysed_files.txt` (une ligne par fichier, jamais dans `/app`). Un fichier inchangé (même nom, SHA-1 et date de version) n'est pas ré-analysé : son verdict est ré-appliqué. Supprimé puis re-téléversé = nouvelle analyse. 30 jours, 5000 lignes max. |
| `common_repo.py` | Recherche d'images similaires sur Common. |
| `config_check.py` | Contrôle de configuration : logs + embed Discord. |
| `discord_logger.py` | Logs et embeds Discord (`build_embed` commun). |

## Message de configuration

Au démarrage (et en mode autonome de chaque script), un embed Discord liste pour chaque script ce qui est
**chargé et utilisable** : connexion et droits wiki, modèles de licence, journaux lisibles, Common joignable,
bibliothèques, clés OpenAI / SightEngine… Vert = tout est bon, orange = avertissements, rouge = erreurs.
Les mêmes lignes vont dans les logs (en `WARNING`, jamais `ERROR`, pour ne pas fausser le statut du dashboard).

    python main.py --check     # contrôle seul, puis quitte (code 1 s'il y a une erreur)

## Variables d'environnement (`.env`)

| Variable | Rôle |
|---|---|
| `DISCORD_BOT_TOKEN` + `DISCORD_LOG_CHANNEL_ID` *ou* `DISCORD_WEBHOOK_URL` | Logs et embeds Discord |
| `COMMONS_API` | URL de `api.php` du dépôt Common (ex. `https://commons.wikimedia.org/w/api.php`). Sinon détecté automatiquement via le wiki. |
| `COMMONS_SIMILARITY` | Seuil de similarité des noms (défaut `0.92`) |
| `OPENAI_API_KEY` | Étape 2 de l'analyse IA (ignorée si absente) |
| `SIGHTENGINE_API_USER` / `SIGHTENGINE_API_SECRET` | Étape 3 de l'analyse IA |
| `RC_FILES_DRY_RUN` | `1` = `recent_files_bot` simule (aucune révocation, aucune demande, aucune écriture). **À essayer d'abord.** |
| `RC_FILES_NAMESPACES` | Espaces de noms surveillés par `recent_files_bot`, ex. `0\|1\|2` (défaut : tous sauf Fichier). |
| `BULLETIN_PATROUILLEURS` | Salon Discord (identifiant, avec `DISCORD_BOT_TOKEN`) **ou URL de webhook** où sont copiés, sans miniature, les embeds « image non admissible » (Vikidia et Common). Vide = aucune copie. |
| `AI_SCORE_THRESHOLD` / `MODERATION_THRESHOLD` / `MODERATION_TEMPLATE` / `MODERATE_ALWAYS` | Réglages de l'analyse IA. Image non admissible sur Vikidia : `{{SI\|Image inappropriée\|BotJanus}}` (ou `MODERATION_TEMPLATE`) + demande de masquage. |

## Statut « Dégradé » du dashboard

L'agent lit le champ `heartbeat` de `/data/rc-images/bot_state.txt`. Il passe « Dégradé » si le heartbeat a plus de 5 min
(le bot bat toutes les 30 s ; arrêt des battements si un cycle dépasse 30 min), ou si au moins 3 erreurs
`ERROR` ont été loguées dans les 15 dernières minutes.

## Tests

    python -m unittest discover -s tests -v
