# BotJanus-RC-images

**BotJanus RC-images** — Surveillance des téléversements : licences, nettoyage des images supprimées et analyse IA.

(Nom et description uniques : `bot_config.py` → `BOT_NAME` / `BOT_DESCRIPTION`. Ce sont les mêmes
textes dans les logs Discord, les embeds, le dashboard et `BotJanus-agent/services.json`.)

## Les 3 scripts

Lancés ensemble par `main.py`, dans **un seul cycle commun toutes les 10 minutes**, avec une heure et un état partagés.

| Script | Rôle |
|---|---|
| `license_bot.py` | Pose `{{LI}}` sur les fichiers sans licence reconnue et prévient le téléverseur. La liste des licences est lue une seule fois, au démarrage, par le contrôle de configuration (jamais relue ensuite : redémarrer le bot pour la mettre à jour). |
| `image_cleanup_bot.py` | Retire des articles les images supprimées. **Avant de retirer quoi que ce soit, cherche une image similaire sur Common** (même nom, même contenu SHA-1, ou nom quasi identique) : si elle existe, rien n'est supprimé. Si Common est injoignable, rien n'est supprimé non plus et l'image est reprise au cycle suivant (3 essais). |
| `ai_detector.py` | Détection d'images IA en 3 étapes (métadonnées → OpenAI → SightEngine) + modération. |

## Organisation du code

| Fichier | Rôle |
|---|---|
| `bot_clock.py` | **Seule** source de l'heure (UTC conscient, format ISO unique). Plus aucun `datetime.now` / `time.time` / `Timestamp.utcnow` ailleurs. |
| `bot_state.py` | `/data/rc-images/bot_state.txt` (JSON ; dossier réglable via `BOTJANUS_DATA_DIR`) : `since` (fenêtre de lecture) + `heartbeat` (battu toutes les 30 s par un thread). |
| `bot_config.py` | Nom/description, chargeur `.env` unique, logging. |
| `cycle.py` | Boucle commune (main.py **et** modes autonomes) + un seul appel API pour le journal des téléversements. |
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
| `AI_SCORE_THRESHOLD` / `MODERATION_THRESHOLD` / `MODERATION_TEMPLATE` / `MODERATE_ALWAYS` | Réglages de l'analyse IA |

## Statut « Dégradé » du dashboard

L'agent lit le champ `heartbeat` de `/data/rc-images/bot_state.txt`. Il passe « Dégradé » si le heartbeat a plus de 5 min
(le bot bat toutes les 30 s ; arrêt des battements si un cycle dépasse 30 min), ou si au moins 3 erreurs
`ERROR` ont été loguées dans les 15 dernières minutes.

## Tests

    python -m unittest discover -s tests -v
