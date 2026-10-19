# insta-mcp

Serveur MCP distant pour **un seul compte Instagram professionnel** : lire et
répondre aux commentaires et aux messages privés, lire les statistiques. Il se
branche à Claude comme connecteur personnalisé et se protège par OAuth.

Fork réduit de [AleemHaider/instagram-mcp](https://github.com/AleemHaider/instagram-mcp)
(MIT). Ce qui change :

- **Connexion Instagram** (`graph.instagram.com`), sans page Facebook.
- **Jeton Instagram renouvelé automatiquement** chaque semaine (il expire à 60 jours).
- **Transport HTTP** (streamable HTTP) au lieu de stdio, pour un hébergement Docker.
- **OAuth intégré pour un seul propriétaire** : Claude s'enregistre (DCR), tu te
  connectes avec un mot de passe sur la page `/login`, PKCE obligatoire.
- **Outils réduits** : pas de publication (Postiz s'en charge), pas de suppression.
- Outils `list_new_comments` et `list_new_messages` pour ne traiter que les nouveautés.
- `mcp` épinglé en 1.x (la 2.x casse l'original).

## Outils

| Outil | Rôle |
|---|---|
| `account_status` | Profil et nombre de jours avant expiration du jeton Instagram |
| `list_recent_media` | Derniers posts et Reels |
| `list_comments` | Commentaires d'un post, avec réponses |
| `list_new_comments` | Commentaires reçus depuis une date, hors les tiens |
| `reply_to_comment` | Répondre à un commentaire |
| `hide_comment` | Masquer / ré-afficher un commentaire (réversible) |
| `list_conversations` | Conversations privées |
| `get_conversation` | Messages d'une conversation |
| `list_new_messages` | Messages privés reçus depuis une date, hors les tiens |
| `send_dm` | Répondre en message privé (fenêtre de 24 h, 1000 caractères max) |
| `get_account_insights` / `get_media_insights` | Statistiques |

Limite connue de l'accès standard Meta : le nom de l'auteur d'un commentaire
n'est pas toujours renvoyé. Il l'est dans les messages privés.

## Sécurité

- `/mcp` répond `401` sans jeton valide. Les jetons OAuth durent 1 h ; les jetons
  de renouvellement 30 jours et changent à chaque usage.
- Seules deux adresses de retour sont acceptées à l'enregistrement : le callback
  de Claude (`https://claude.ai/api/mcp/auth_callback`) et les adresses locales
  (Claude Code). Personne ne peut enregistrer un autre client.
- La page `/login` bloque 15 minutes après 5 mauvais mots de passe.
- Sur le disque (`/data`) : le jeton Instagram et les **empreintes** des jetons
  OAuth, en `chmod 600`. Jamais le mot de passe.
- Le jeton Instagram voyage dans l'URL des appels Meta (c'est ce qu'impose l'API) :
  les logs `httpx` sont coupés pour qu'il n'apparaisse jamais.
- Optionnel : filtrer `/mcp` et `/token` sur la plage d'Anthropic
  `160.79.104.0/21` au niveau du reverse proxy. **Ne filtre pas `/authorize` ni
  `/login`** : c'est ton navigateur qui les ouvre.

## Prérequis Meta

1. Une app Meta de type **Business** avec le produit **Instagram › API setup with
   Instagram login**, en mode **Live** (politique de confidentialité et
   suppression des données renseignées).
2. Ton compte Instagram ajouté en **Testeur(se) Instagram**, invitation acceptée.
3. Un jeton généré dans *Generate access tokens*, avec les permissions
   `instagram_business_basic`, `instagram_business_manage_comments`,
   `instagram_business_manage_messages`, `instagram_business_manage_insights`.

## Installation (Docker)

L'image est publiée automatiquement sur `ghcr.io/mac-s/insta-mcp` à chaque
modification de `main` (amd64 et arm64), après les tests.

Ajoute ce service à ton `docker-compose.yml` existant :

```yaml
  insta-mcp:
    image: ghcr.io/mac-s/insta-mcp:latest
    container_name: insta-mcp
    restart: unless-stopped
    environment:
      PUBLIC_URL: "${INSTA_MCP_PUBLIC_URL}"
      IG_ACCESS_TOKEN: "${INSTA_MCP_IG_TOKEN}"
      MCP_ADMIN_PASSWORD: "${INSTA_MCP_ADMIN_PASSWORD}"
    volumes:
      - ./insta-mcp:/data
    ports:
      - "127.0.0.1:8010:8000"
    read_only: true
    tmpfs:
      - /tmp
    security_opt:
      - no-new-privileges:true
    cap_drop:
      - ALL
```

Et ces variables à ton `.env` (voir `.env.example`) :

```bash
INSTA_MCP_PUBLIC_URL=https://insta-mcp.example.com
INSTA_MCP_IG_TOKEN=IGAA...
INSTA_MCP_ADMIN_PASSWORD=...   # openssl rand -base64 24
```

Puis :

```bash
mkdir -p insta-mcp && sudo chown 10001 insta-mcp   # le conteneur tourne en uid 10001
docker compose pull insta-mcp && docker compose up -d insta-mcp
docker compose logs -f insta-mcp
```

Ton reverse proxy envoie `https://insta-mcp.example.com` vers le port `8010`, sans
réécriture de chemin. Si ton proxy tourne lui-même dans Docker, remplace
`127.0.0.1:8010:8000` par `8010:8000`, ou branche le conteneur sur le réseau du proxy.

Mise à jour : `docker compose pull insta-mcp && docker compose up -d insta-mcp`.

Vérification rapide :

```bash
curl -i https://insta-mcp.example.com/mcp            # → 401 + WWW-Authenticate
curl https://insta-mcp.example.com/.well-known/oauth-protected-resource/mcp
```

## Brancher sur Claude

*Personnaliser › Connecteurs › Ajouter un connecteur personnalisé* :

- **URL** : `https://insta-mcp.example.com/mcp`
- **Authentification** : connexion OAuth (enregistrement automatique)

Claude ouvre la page `/login` : entre la valeur de `INSTA_MCP_ADMIN_PASSWORD`.

Pour Claude Code :
`claude mcp add --transport http insta https://insta-mcp.example.com/mcp`

## Changer le jeton Instagram

Remplace `INSTA_MCP_IG_TOKEN` dans `.env` puis `docker compose up -d insta-mcp`. Un jeton
différent de celui du premier démarrage remplace celui stocké dans `data/`.
Si tu n'utilises pas le serveur pendant plus de 60 jours, le jeton expire :
génère-en un nouveau de la même façon.

## Développement

```bash
python -m venv .venv && . .venv/bin/activate
pip install -e '.[dev]'
pytest
```
