# Engram

> L'hippocampe local de la trilogie : une memoire operationnelle partagee qui reste
> explicable, bornee et consolidee vers Datacron apres revue humaine.

[![License: Apache 2.0](https://img.shields.io/badge/License-Apache%202.0-blue.svg)](LICENSE)
[![Python 3.13+](https://img.shields.io/badge/Python-3.13%2B-3776AB.svg)](pyproject.toml)
[![MCP Streamable HTTP](https://img.shields.io/badge/MCP-Streamable%20HTTP-5A45FF.svg)](server.json)
[![CI](https://github.com/VBlackJack/Engram/actions/workflows/ci.yml/badge.svg)](https://github.com/VBlackJack/Engram/actions/workflows/ci.yml)

Francais | [English](README.en.md)

Engram est un serveur MCP local-first qui capture des souvenirs de travail et restitue des
capsules compactes, classees selon leur confiance. Dans la trilogie, **Datacron** est le carnet
Markdown durable et la source de verite, **Cortex** est le bibliothecaire de documentation large,
et **Engram** est l'hippocampe : il maintient la memoire operationnelle entre clients puis propose
sa consolidation vers Datacron.

## Ce qui est en place

| Capacite | Etat |
| --- | --- |
| Stockage | SQLite WAL, migrations, TTL, idempotence, supersession |
| Ecriture | Un processus Engram est le writer unique |
| Audit | Journal append-only sans contenu de souvenir |
| MCP | Streamable HTTP, outils stricts `remember` et `recall` |
| Recherche | FTS5/BM25 par defaut, hybride local optionnel derriere un flag |
| Confiance | Provenance serveur, plafond de confiance, quarantaine anti-poisoning |
| Rappel | Capsule bornee : current, next_action, relevant, conflicts, own_pending, sources |
| Consolidation | Plan humain, ecriture Datacron par CAS, relecture, controle de fraicheur |
| Evaluation | Corpus seede et graders deterministes, sans acces au vault Datacron |

## Installation

Prerequis :

- Python 3.13 ou plus recent ;
- `uv` 0.11.3 ou plus recent recommande ;
- **SQLite 3.51.3 ou plus recent dans le runtime Python**.

Le plancher SQLite est dur. Les versions 3.7.0 a 3.51.2 sont affectees par le bug WAL-reset
documente par SQLite. Engram verifie `sqlite3.sqlite_version` a l'ouverture et refuse un runtime
trop ancien. Voir [installation Windows](docs/fr/installation-windows.md) pour installer la DLL
SQLite 3.53.x officielle. La page SQLite decrit le
[bug WAL-reset](https://sqlite.org/wal.html#walreset) et publie les
[binaires 3.53.3](https://www.sqlite.org/download.html).

```powershell
git clone https://github.com/VBlackJack/Engram.git
cd Engram
uv sync --extra dev --python 3.14.3
uv run --python 3.14.3 python -c "import sqlite3; print(sqlite3.sqlite_version)"
Copy-Item engram.example.toml engram.toml
```

Le paquet PyPI n'est pas publie dans cette release. L'installation se fait depuis les sources ou
les artefacts wheel/sdist attaches a la release GitHub.

## Demarrage rapide

```powershell
uv run --python 3.14.3 engram serve
```

Le point MCP par defaut est `http://127.0.0.1:8377/mcp`. Conserver cette adresse loopback : le
serveur n'implemente pas d'authentification reseau.

Ajoutez ensuite ce serveur a Claude Code, Codex ou Gemini, puis installez le
[protocole client](docs/fr/client-protocol.md). Les blocs de configuration exacts sont dans le
[guide de mise en place](docs/fr/setup.md).

## Configuration

Engram charge `engram.toml`. `ENGRAM_CONFIG` peut selectionner un autre fichier. Toute cle TOML
peut etre surchargee par `ENGRAM_<SECTION>_<CLE>` ; les chemins relatifs sont resolus depuis le
dossier du fichier TOML.

| Section TOML | Variables principales | Role |
| --- | --- | --- |
| `[database]` | `ENGRAM_DATABASE_PATH`, `ENGRAM_DATABASE_BUSY_TIMEOUT_MS` | Base et attente SQLite |
| `[ttl_days]` | `ENGRAM_TTL_DAYS_PREFERENCE`, `_DECISION`, `_FACT`, `_PROJECT_STATE`, `_EPISODE` | Duree par kind ; `0` desactive l'expiration |
| `[limits]` | `ENGRAM_LIMITS_MAX_STATEMENT_CHARS`, `ENGRAM_LIMITS_MAX_SUBJECT_KEYS` | Bornes d'entree |
| `[logging]` | `ENGRAM_LOGGING_PATH`, `_FILE_LEVEL`, `_CONSOLE_LEVEL` | Fichier et niveaux de log |
| `[attestation]` | `ENGRAM_ATTESTATION_DEFAULT_ACTOR` | Acteur par defaut des mutations locales de confiance |
| `[server]` | `ENGRAM_SERVER_HOST`, `_PORT`, `_PATH`, `_WRITE_WAIT_TIMEOUT_MS`, `_TTL_SWEEP_INTERVAL_SECONDS` | Endpoint HTTP, backpressure et balayage d'expiration logique |
| `[capsule]` | `ENGRAM_CAPSULE_DEFAULT_TOKEN_BUDGET`, `_MIN_TOKEN_BUDGET`, `_MAX_TOKEN_BUDGET` | Budget du rappel |
| `[retrieval]` | `ENGRAM_RETRIEVAL_MODE`, `_EMBEDDINGS_ENDPOINT`, `_EMBEDDINGS_MODEL`, `_EMBEDDINGS_TIMEOUT_MS`, `_RRF_K` | FTS ou hybride local |
| `[datacron]` | `ENGRAM_DATACRON_COMMAND`, `_ARGS`, `_VAULT_ROOT`, `_READ_PATHS`, `_WRITE_PATHS`, `_NEW_NOTE_DIRECTORY`, `_NEIGHBOR_LIMIT` | Gateway et confinement Datacron |

Pour une variable de liste, `ARGS` suit le decoupage shell et `READ_PATHS`/`WRITE_PATHS` utilisent
le separateur de chemins de l'OS. Le fichier complet et ses valeurs sures sont dans
[`engram.example.toml`](engram.example.toml). Les ecritures Datacron restent desactivees si
`write_paths` est vide.

## Outils MCP

| Outil | Entrees essentielles | Resultat et politique |
| --- | --- | --- |
| `remember` | `statement`, `kind`, `scope`, `subject_keys`, `observed_at`, `evidence` | Cree un candidat `model_inferred`, `quarantined`, confiance au plus `medium` |
| `recall` | `query`, `scope`, `kinds`, `include_conflicts`, `token_budget` | Retourne une capsule trust-aware ; seuls les candidats du client courant figurent dans `own_pending` |

Kinds acceptes : `preference`, `decision`, `project_state`, `fact`, `episode`. Le serveur attribue
la provenance ; un client ne peut jamais declarer lui-meme une source `human`.

## Securite et vie privee

- Toutes les donnees, l'index lexical, l'audit et les logs restent locaux.
- Aucun appel a un LLM cloud ni aucune telemetrie n'est implemente.
- Les candidats d'un client sont quarantaines pour eviter qu'une affirmation non attestee ne
  devienne la verite partagee.
- Le mode hybride contacte uniquement l'endpoint d'embeddings explicitement configure ; FTS est
  le mode par defaut.
- Les ecritures Datacron passent par des allowlists sous `_memory/`, une verification CAS et une
  relecture.
- Ne pas exposer le serveur sur `0.0.0.0` sans proxy d'authentification et controle reseau.

Voir le [modele de securite complet](docs/fr/security.md).

## Commandes CLI

```text
engram --version
engram serve
engram reindex
engram list --status quarantined
engram attest "Statement relu" fact user --subject-key "topic/key"
engram supersede --old OLD_ID --new NEW_ID
engram eval --mode both --out local/eval
engram consolidate --plan --out local/consolidation/plan.json
engram consolidate --apply local/consolidation/plan.json
engram consolidate --check-freshness
```

`consolidate --plan` ne modifie rien. Editez chaque `decision` du JSON (`approve` ou `reject`) avant
`--apply`. Un hash Datacron divergent produit `stale` et exige un nouveau plan ; il n'est jamais
force.
Arreter le daemon avant `attest`, `supersede`, `reindex` ou `consolidate`, puis le redemarrer avant
recall. Ces commandes prennent le meme verrou OS que le daemon et echouent clairement tant qu'il
est actif ; `list` reste disponible via une connexion SQLite read-only. Les commandes de confiance
utilisent `[attestation].default_actor`, sauf si `--actor` est fourni.

## Limites actuelles

- Engram ne voit pas passivement les conversations : chaque client doit appeler `recall` et
  `remember` selon le protocole documente.
- Le transport est HTTP local. Le connecteur distant Claude Desktop exige une URL HTTPS publique ;
  Claude Code se connecte directement a localhost.
- Le mode hybride est experimental et depend d'un endpoint compatible OpenAI local. Il se degrade
  explicitement vers FTS en cas de panne.
- La publication PyPI et la soumission au MCP Registry sont differees. Le manifeste est pret pour
  le paquet et son endpoint HTTP local.
- Porter et les recherches par prefixe ne seront evalues que si l'usage reel montre des ratages
  morphologiques.

## Documentation

| Demarrer | Comprendre | Exploiter en confiance |
| --- | --- | --- |
| [Installation](docs/fr/setup.md) | [Contrat de donnees](docs/fr/spec.md) | [Securite](docs/fr/security.md) |
| [Windows et SQLite](docs/fr/installation-windows.md) | [Architecture](docs/fr/architecture.md) | [FAQ](docs/fr/faq.md) |
| [Guide utilisateur](docs/fr/user-guide.md) | [Protocole client](docs/fr/client-protocol.md) | [Hub documentaire](docs/fr/index.md) |

## Developpement

```powershell
uv sync --extra dev --python 3.14.3
uv run --python 3.14.3 ruff check .
uv run --python 3.14.3 ruff format --check .
uv run --python 3.14.3 mypy
uv run --python 3.14.3 pytest
uv build --python 3.14.3
```

## Licence

Apache License 2.0. Copyright 2026 Julien Bombled. Voir [LICENSE](LICENSE) et les
[notices tiers](THIRD_PARTY_NOTICES.md).
