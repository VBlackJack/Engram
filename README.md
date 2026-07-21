# Engram

Engram est le memory broker de la trilogie :

- Datacron conserve la source de vérité Markdown et son historique.
- Cortex recherche dans la documentation large.
- Engram capture et rappelle une mémoire opérationnelle partagée entre assistants.

Ce dépôt contient un spike d'évaluation avec stockage SQLite et serveur MCP Streamable HTTP.
Le serveur expose uniquement `remember` et `recall`. Le matching de rappel est volontairement
naif dans ce lot: filtres, sous-chaine insensible a la casse et recence.

## Configuration

Engram exige Python 3.13 ou plus récent et SQLite 3.51.3 ou plus récent. Une version SQLite
antérieure est refusée au démarrage à cause du correctif de réinitialisation WAL absent.

Copier `engram.example.toml` vers `engram.toml`, puis adapter les valeurs. Chaque valeur peut
être surchargée par une variable d'environnement préfixée `ENGRAM_`. Exemples :
`ENGRAM_DATABASE_PATH`, `ENGRAM_TTL_DAYS_EPISODE` et `ENGRAM_LOGGING_FILE_LEVEL`.
`ENGRAM_CONFIG` sélectionne un autre fichier TOML.

La section `[server]` configure l'adresse, le port, le chemin MCP et le délai maximal
d'acquisition du verrou d'écriture. La section `[capsule]` borne le budget des rappels.

## Serveur

```powershell
uv run engram serve
```

Avec la configuration d'exemple, le point d'accès est `http://127.0.0.1:8377/mcp`. Les
souvenirs déposés par `remember` restent en quarantaine. Ils ne sont visibles que dans
`own_pending` pour le même client MCP, avec l'étiquette `unconfirmed candidate`.

## Vérification

```powershell
uv sync --group dev
uv run ruff check .
uv run ruff format --check .
uv run mypy
uv run pytest
```

Licence Apache 2.0. Copyright 2026 Julien Bombled.
