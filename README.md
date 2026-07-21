# Engram

Engram est le memory broker de la trilogie :

- Datacron conserve la source de vérité Markdown et son historique.
- Cortex recherche dans la documentation large.
- Engram capture et rappelle une mémoire opérationnelle partagée entre assistants.

Ce dépôt contient un spike d'évaluation avec stockage SQLite et serveur MCP Streamable HTTP.
Le serveur expose uniquement `remember` et `recall`. Le rappel utilise FTS5/BM25 par défaut,
avec départage par récence et filet substring quand FTS5 ne trouve aucun résultat.

## Configuration

Engram exige Python 3.13 ou plus récent et SQLite 3.51.3 ou plus récent. Une version SQLite
antérieure est refusée au démarrage à cause du correctif de réinitialisation WAL absent.

Copier `engram.example.toml` vers `engram.toml`, puis adapter les valeurs. Chaque valeur peut
être surchargée par une variable d'environnement préfixée `ENGRAM_`. Exemples :
`ENGRAM_DATABASE_PATH`, `ENGRAM_TTL_DAYS_EPISODE` et `ENGRAM_LOGGING_FILE_LEVEL`.
`ENGRAM_CONFIG` sélectionne un autre fichier TOML.

La section `[server]` configure l'adresse, le port, le chemin MCP et le délai maximal
d'acquisition du verrou d'écriture. La section `[capsule]` borne le budget des rappels.
La section `[retrieval]` sélectionne `fts` ou le mode expérimental `hybrid`. Ce dernier exige
un modèle et un endpoint local compatible OpenAI; une panne le dégrade explicitement en FTS.

## Serveur

```powershell
uv run engram serve
```

Avec la configuration d'exemple, le point d'accès est `http://127.0.0.1:8377/mcp`. Les
souvenirs déposés par `remember` restent en quarantaine. Ils ne sont visibles que dans
`own_pending` pour le même client MCP, avec l'étiquette `unconfirmed candidate`.

Les index FTS et vectoriels sont dérivés et reconstructibles :

```powershell
uv run engram reindex
```

## Evaluation

Le harnais versionné charge 72 entrées dans une base temporaire et note 64 requêtes avec des
graders déterministes. Il ne contacte jamais le vault Datacron. Le mode `both` mesure aussi
l'hybride si le modèle configuré répond :

```powershell
uv run engram eval --mode both --out local/eval
```

Le dossier de sortie contient `metrics.json`, avec le verdict P2 émis par le code, et
`rapport-eval.md`, une synthèse française courte. Le modèle de référence est
`nomic-embed-text-v1.5` via LM Studio; `bge-m3` reste une alternative configurable.

## Vérification

```powershell
uv sync --group dev
uv run ruff check .
uv run ruff format --check .
uv run mypy
uv run pytest
```

Licence Apache 2.0. Copyright 2026 Julien Bombled.
