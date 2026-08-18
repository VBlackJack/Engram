# Engram

> L'hippocampe local de la trilogie : une mémoire opérationnelle partagée qui reste
> explicable, bornée et consolidée vers Datacron après revue humaine.

[![License: Apache 2.0](https://img.shields.io/badge/License-Apache%202.0-blue.svg)](LICENSE)
[![Python 3.13+](https://img.shields.io/badge/Python-3.13%2B-3776AB.svg)](pyproject.toml)
[![MCP Streamable HTTP](https://img.shields.io/badge/MCP-Streamable%20HTTP-5A45FF.svg)](server.json)
[![CI](https://github.com/VBlackJack/Engram/actions/workflows/ci.yml/badge.svg)](https://github.com/VBlackJack/Engram/actions/workflows/ci.yml)

Français | [English](README.en.md)

Engram est un serveur MCP local-first qui capture des souvenirs de travail et restitue des
capsules compactes, classées selon leur confiance. Dans la trilogie, **Datacron** est le carnet
Markdown durable et la source de vérité, **Cortex** est le bibliothécaire de documentation large,
et **Engram** est l'hippocampe : il maintient la mémoire opérationnelle entre clients puis propose
sa consolidation vers Datacron.

## Choisir son parcours

| Je veux... | Guide |
| --- | --- |
| Lancer Engram maintenant | [Démarrage en 5 minutes](docs/fr/quick-start.md) |
| Utiliser la mémoire au quotidien | [Guide utilisateur](docs/fr/user-guide.md) |
| Comprendre Engram, Datacron et Cortex | [Guide de la trilogie](docs/fr/datacron-cortex.md) |
| Administrer, migrer ou consolider | [Guide opérateur](docs/fr/operator-guide.md) |
| Garder Engram actif après une déconnexion | [Tâche de session Windows](docs/fr/setup.md#windows-la-tache-douverture-de-session) ou [systemd / launchd](docs/fr/installation-unix.md) |
| Savoir pourquoi Engram ne fonctionne pas | `engram doctor`, puis la [FAQ](docs/fr/faq.md) |

Le reste de ce README est une référence de release. Il n'est pas nécessaire de tout lire pour
commencer. Documentation vérifiée avec Engram `2026.0730.02` le 2026-08-13.

## Ce qui est en place

| Capacité | État |
| --- | --- |
| Stockage | SQLite WAL, migrations, TTL, idempotence, supersession |
| Écriture | Un processus Engram est le writer unique |
| Audit | Journal append-only sans contenu de souvenir |
| MCP | Streamable HTTP, outils stricts `remember` et `recall` |
| Recherche | FTS5/BM25 par défaut, hybride local optionnel derrière un flag |
| Confiance | Provenance serveur, plafond de confiance, quarantaine anti-poisoning |
| Rappel | Capsule bornée : current, next_action, relevant, conflicts, own_pending, sources |
| Consolidation | Plan humain, create/link Datacron vérifié, relecture, contrôle de fraîcheur |
| Évaluation | Corpus seedé et graders déterministes, sans accès au vault Datacron |

## Installation

Prérequis :

- Git pour l'installation depuis les sources ;
- Python 3.13 ou plus récent ;
- **`uv` 0.12.1 ou plus récent** — exigé, et non simplement conseillé : c'est la première version
  qui connaît le build `3.14.6`, et l'intégration continue épingle `uv==0.12.1` sur ses deux legs,
  Windows et Linux ;
- **SQLite 3.51.3 ou plus récent dans le runtime Python**.

Le plancher SQLite est dur. Les versions 3.7.0 à 3.51.2 sont affectées par le bug WAL-reset
documenté par SQLite. Engram vérifie `sqlite3.sqlite_version` à l'ouverture et refuse un runtime
trop ancien ; le refus nomme `engram doctor` et l'URL de documentation, qui indiquent tous deux la
réparation applicable à la machine devant vous. Voir
[installation Windows](docs/fr/installation-windows.md) pour installer la DLL SQLite 3.53.x
officielle. La page SQLite décrit le
[bug WAL-reset](https://sqlite.org/wal.html#walreset) et publie les
[binaires 3.53.3](https://www.sqlite.org/download.html).

Identique sous Windows, macOS et Linux :

```text
git clone https://github.com/VBlackJack/Engram.git
cd Engram
uv sync --python 3.14.6
uv run --python 3.14.6 engram init
uv run --python 3.14.6 engram doctor
```

`engram init` écrit `engram.toml` depuis la copie empaquetée dans la distribution : elle fonctionne
depuis une installation par wheel comme depuis un checkout, et refuse de remplacer un fichier
existant sauf avec `--force`. `engram doctor` rapporte ensuite l'interpréteur, le plancher SQLite,
la configuration résolue, la base, le verrou, l'endpoint et le fichier de log — chacun avec la
commande qui le répare.

Le paquet PyPI n'est pas publié. Avant la publication de cette release GitHub, utilisez le checkout
source courant ; après publication, les artefacts wheel/sdist attachés à la release sont aussi
utilisables.

## Démarrage rapide

```text
uv run --python 3.14.6 engram serve
```

Le point MCP par défaut est `http://127.0.0.1:8377/mcp`. Engram refuse toute adresse d'écoute qui
n'est pas un literal IP loopback : le serveur n'implémente pas d'authentification réseau.

`engram serve` dure exactement aussi longtemps que son terminal. Pour garder Engram après une
fermeture de session :

| Plateforme | Commande |
| --- | --- |
| Windows | `uv run --python 3.14.6 engram setup autostart --install` enregistre une tâche d'ouverture de session qui lance le démon sans fenêtre de console. `--status` la rapporte, `--uninstall` la supprime. |
| macOS / Linux | `engram setup autostart` est réservé à Windows et sort en `2` ailleurs. Utilisez l'unité utilisateur systemd ou le LaunchAgent launchd de [Installer en service sous macOS et Linux](docs/fr/installation-unix.md). |

Arrêtez le démon depuis n'importe quelle installation avec
`uv run --python 3.14.6 engram stop`, qui lui demande de fermer la base, attend sur le verrou de
propriété et rapporte s'il s'est réellement arrêté.

Connectez un client en une commande, avec l'endpoint de votre propre configuration :

```text
uv run --python 3.14.6 engram setup client claude --protocol
```

Remplacez `claude` par `codex` ou `gemini`. La commande écrit `.mcp.json`,
`~/.codex/config.toml` ou `~/.gemini/settings.json` en fusionnant au lieu d'écraser, et
`--protocol` ajoute le [protocole client](docs/fr/client-protocol.md) dans `CLAUDE.md`, `AGENTS.md`
ou `GEMINI.md`. Les blocs exacts à écrire à la main sont dans le
[guide de mise en place](docs/fr/setup.md).

## Configuration

Engram charge `engram.toml`. `ENGRAM_CONFIG` peut sélectionner un autre fichier. Toute clé TOML
peut être surchargée par `ENGRAM_<SECTION>_<CLE>` ; les chemins relatifs sont résolus depuis le
dossier du fichier TOML.

| Section TOML | Variables principales | Rôle |
| --- | --- | --- |
| `[database]` | `ENGRAM_DATABASE_PATH`, `ENGRAM_DATABASE_BUSY_TIMEOUT_MS` | Base et attente SQLite |
| `[ttl_days]` | `ENGRAM_TTL_DAYS_PREFERENCE`, `_DECISION`, `_FACT`, `_PROJECT_STATE`, `_EPISODE`, `_CANDIDATE_MAX_DAYS` | Durée de vie fiable par kind, où `0` désactive l'expiration ; `candidate_max_days` (90) borne en plus un candidat non attesté |
| `[limits]` | `ENGRAM_LIMITS_MAX_STATEMENT_CHARS`, `ENGRAM_LIMITS_MAX_SUBJECT_KEYS` | Bornes d'entrée |
| `[logging]` | `ENGRAM_LOGGING_PATH`, `_FILE_LEVEL`, `_CONSOLE_LEVEL` | Fichier et niveaux de log |
| `[attestation]` | `ENGRAM_ATTESTATION_DEFAULT_ACTOR` | Acteur par défaut des mutations locales de confiance |
| `[server]` | `ENGRAM_SERVER_HOST`, `_PORT`, `_PATH`, `_WRITE_WAIT_TIMEOUT_MS`, `_TTL_SWEEP_INTERVAL_SECONDS`, `_MAX_REQUEST_BODY_BYTES` | Endpoint HTTP local, backpressure, corps borné à 512 KiB maximum et balayage d'expiration logique |
| `[capsule]` | `ENGRAM_CAPSULE_DEFAULT_TOKEN_BUDGET`, `_MIN_TOKEN_BUDGET`, `_MAX_TOKEN_BUDGET` | Budget du rappel |
| `[retrieval]` | `ENGRAM_RETRIEVAL_MODE`, `_FTS_TOP_K`, `_FTS_MAX_QUERY_CHARS`, `_FTS_MAX_QUERY_TERMS`, `_FTS_MIN_PREFIX_CHARS`, `_FTS_QUERY_TIMEOUT_MS`, `_HYBRID_MAX_CANDIDATES`, `_EMBEDDINGS_ENDPOINT`, `_EMBEDDINGS_MODEL`, `_EMBEDDINGS_TIMEOUT_MS`, `_RRF_K` | FTS borné avec deadline absolue ou hybride local |
| `[datacron]` | `ENGRAM_DATACRON_COMMAND`, `_ARGS`, `_VAULT_ROOT`, `_READ_PATHS`, `_WRITE_PATHS`, `_NEW_NOTE_DIRECTORY`, `_NEIGHBOR_LIMIT`, `_STARTUP_TIMEOUT_MS`, `_REQUEST_TIMEOUT_MS`, `_SHUTDOWN_TIMEOUT_MS` | Gateway, timeouts et confinement Datacron |

Pour une variable de liste, `ARGS` suit le découpage shell et `READ_PATHS`/`WRITE_PATHS` utilisent
le séparateur de chemins de l'OS. Le fichier complet et ses valeurs sûres sont dans
[`engram.example.toml`](engram.example.toml) ; `engram init` écrit la même chose depuis la copie
empaquetée. Les écritures Datacron restent désactivées si `write_paths` est vide, même si le
processus parent définit `DATACRON_WRITE_PATHS`. Le transport local par défaut lance
`datacron mcp serve`.

## Outils MCP

| Outil | Entrées essentielles | Résultat et politique |
| --- | --- | --- |
| `remember` | `statement`, `kind`, `scope`, `subject_keys`, `observed_at`, `evidence` | Retourne `created`, `retry`, `corroborated`, `existing_trusted` ou `renewed` ; les contenus nouveaux/renouvelés restent quarantainés |
| `recall` | `query`, `scope`, `kinds`, `include_conflicts`, `token_budget` | Retourne une capsule trust-aware ; toujours inspecter `notes.recall_complete` et ses codes |

Kinds acceptés : `preference`, `decision`, `project_state`, `fact`, `episode`. Le serveur attribue
la provenance ; un client ne peut jamais déclarer lui-même une source `human`.

## Sécurité et vie privée

- Toutes les données, l'index lexical, l'audit et les logs restent locaux.
- Aucun appel à un LLM cloud ni aucune télémétrie n'est implémenté.
- Les candidats d'un client sont quarantainés pour éviter qu'une affirmation non attestée ne
  devienne la vérité partagée.
- Le nom/version MCP est un espace de noms auto-déclaré, pas une authentification ni une frontière
  de confidentialité.
- Le mode hybride contacte uniquement l'endpoint d'embeddings explicitement configuré ; FTS est
  le mode par défaut.
- Les écritures Datacron passent par des allowlists sous `_memory/`, un chemin canonique
  déterministe et une relecture exacte.
- L'écoute directe est limitée aux literals IP loopback. Un proxy distant éventuel doit joindre
  Engram localement et fournir lui-même authentification, TLS et contrôle réseau.

Voir le [modèle de sécurité complet](docs/fr/security.md).

## Commandes CLI

```text
engram --version
engram --debug serve
engram init
engram init --force
engram doctor
engram doctor --json
engram serve
engram stop
engram setup autostart --install
engram setup autostart --status
engram setup autostart --uninstall
engram setup client claude --protocol
engram setup client codex --print
engram setup client gemini --force
engram migrate
engram preflight
engram reindex
engram list --status quarantined
engram list --unclassified
engram classify ENTRY_ID --claim-key "topic/claim"
engram attest "Statement relu" fact user --subject-key "topic/key" --claim-key "topic/claim"
engram supersede --old OLD_ID --new NEW_ID
engram eval --mode both --out local/eval
engram consolidate --plan --out local/consolidation/plan.json
engram consolidate --apply local/consolidation/plan.json
engram consolidate --check-freshness
```

`--config <chemin>` est une option globale et se place **avant** la sous-commande.

| Commande | À quoi elle sert |
| --- | --- |
| `engram init` | Écrit le `engram.toml` de départ depuis la copie empaquetée dans la distribution — pas de checkout, pas de syntaxe de shell, pas de différence entre plateformes. Refuse d'écraser ; `--force` remplace délibérément. |
| `engram doctor` | Le diagnostic unique à lancer avant tout le reste, et celui à envoyer à quiconque n'arrive pas à connecter son client. Rapporte l'interpréteur, le plancher SQLite, la configuration résolue et si elle se charge, la base et sa version de schéma, le verrou de propriété, l'endpoint et le fichier de log, chacun avec sa réparation. Sort en `0` sauf échec ; `--json` pour les scripts. |
| `engram stop` | Demande au démon propriétaire de cette base de la fermer et de sortir, attend sur le verrou et rapporte s'il s'est arrêté. Seule façon d'arrêter proprement une tâche d'ouverture de session sans fenêtre ou un service supervisé. |
| `engram setup autostart` | **Windows uniquement.** Enregistre, inspecte ou supprime la tâche d'ouverture de session qui lance le démon sans console. Sort en `2` sur toute autre plateforme et ne change rien ; utilisez [systemd ou launchd](docs/fr/installation-unix.md) là-bas. Sans elle, Engram s'arrête à la prochaine fermeture de session. |
| `engram setup client` | Écrit `.mcp.json` (Claude Code, répertoire courant), `~/.codex/config.toml` (Codex) ou `~/.gemini/settings.json` (Gemini) avec l'endpoint de la configuration chargée. Fusionne au lieu d'écraser : les autres serveurs, les clés et les commentaires TOML survivent. `--protocol` ajoute le protocole de session à `CLAUDE.md` / `AGENTS.md` / `GEMINI.md` ; `--print` n'écrit rien ; `--force` remplace une entrée nommant un autre endpoint. |

Le bloc Codex écrit par cette commande omet délibérément la clé `required` : OpenAI la définit
comme faisant échouer le démarrage de Codex quand le serveur ne peut pas s'initialiser, si bien
qu'un courtier de mémoire simplement arrêté emporterait tout l'assistant avec lui.

`consolidate --plan` reste read-only pour Datacron, mais ancre les propositions immuables dans la
base Engram. Éditez uniquement chaque `decision` du JSON (`approve` ou `reject`) avant `--apply`.
Le plan est à usage unique : toute modification d'un autre champ ou toute relecture après apply est
refusée et exige un nouveau plan. Un hash Datacron divergent produit `stale`, conserve le rapport et
renvoie le code 6 ; il n'est jamais forcé. Actuellement, un résultat `update` reste visible avec sa
cible et son diff dans le rapport, mais produit toujours `skip` : Engram ne patche aucune section
tant que Datacron ne fournit pas un ancrage d'identité durable indépendamment vérifié.
Chaque création cible un seul chemin canonique contenant l'ID candidat. Après une réponse
d'écriture ambiguë, un nouveau plan ne réconcilie que le contenu canonique complet identique de ce
chemin au lieu de créer un doublon.
Arrêter le démon avec `engram stop` avant `migrate`, `classify`, `attest`, `supersede`, `reindex`
ou `consolidate`, puis le redémarrer avant recall. Ces commandes prennent le même verrou OS que le
démon et échouent clairement tant qu'il est actif ; `list` reste disponible via une connexion
SQLite read-only. Pour une base existante, effectuer d'abord une sauvegarde SQLite cohérente,
arrêter le démon, puis lancer `engram preflight`. Il garde le verrou writer offline, laisse la base
source en lecture seule, copie son snapshot vers un stockage temporaire et y prouve la
migration/reconstruction complète avant d'annoncer la compatibilité. Lancer ensuite `engram migrate`
et inventorier `engram list --unclassified`. Relire chaque préférence, décision ou fait historique
et lui attribuer explicitement sa famille avec
`engram classify ENTRY_ID --claim-key "topic/claim"` ; ne jamais inférer ces clés en masse. Les
commandes de confiance utilisent `[attestation].default_actor`, sauf si `--actor` est fourni.
R3 ne tronque jamais les données qui dépassent ses nouveaux plafonds fixes : un preflight en échec
nomme la première ligne à relire avec 2026.0730.01 avant de retenter. Si le preflight renvoie
`vector_rebuild_required: true` et que le mode hybride est actif, lancer `engram reindex` après la
migration. SQLite charge d'abord le schéma sous un plafond temporaire de 256 Kio, puis conserve un
plafond de 8 Mio par valeur/ligne ; les snapshots de consolidation sont limités explicitement à
4 Mio UTF-8. Le preflight refuse les données historiques incompatibles sans les tronquer.

Les erreurs CLI connues n'affichent aucun traceback par défaut. Le code `2` signale l'usage ou la
configuration, `3` une ressource locale indisponible (port, verrou de processus, base ou runtime
SQLite), `4` une dépendance externe injoignable (Datacron ou endpoint d'embeddings), `5` une
contention transitoire du store et `6` un rapport apply contenant des propositions `failed` ou
`stale`. Utiliser le flag global `--debug` avant la commande, ou `ENGRAM_DEBUG=1`, uniquement pour
obtenir un traceback.

## Limites actuelles

- Engram ne voit pas passivement les conversations : chaque client doit appeler `recall` et
  `remember` selon le protocole documenté.
- Le transport est HTTP local. Le connecteur distant Claude Desktop exige une URL HTTPS publique ;
  Claude Code se connecte directement à localhost.
- Le mode hybride est expérimental et dépend d'un endpoint compatible OpenAI local. Il se dégrade
  explicitement vers FTS si le provider est indisponible ou renvoie un vecteur invalide, ou si le
  scan exact dépasse les plafonds fixes de candidats, dimensions ou octets. Une couverture
  vectorielle incomplète marque le rappel incomplet.
- La publication PyPI et la soumission au MCP Registry sont différées. Le manifeste est prêt pour
  le paquet et son endpoint HTTP local.
- Le FTS reste lexical : ses fallbacks bornés gèrent le bruit, l'ordre des termes et les préfixes,
  mais pas les paraphrases sans vocabulaire commun. Le rapport d'évaluation mesure séparément ces
  paraphrases pour éviter de confondre rappel lexical et rappel sémantique.

## Documentation

| Démarrer | Utiliser | Exploiter en confiance |
| --- | --- | --- |
| [Parcours 5 minutes](docs/fr/quick-start.md) | [Guide utilisateur](docs/fr/user-guide.md) | [Guide opérateur](docs/fr/operator-guide.md) |
| [Installation](docs/fr/setup.md) | [Engram, Datacron et Cortex](docs/fr/datacron-cortex.md) | [Sécurité](docs/fr/security.md) |
| [Protocole client](docs/fr/client-protocol.md) | [Architecture](docs/fr/architecture.md) | [FAQ et hub](docs/fr/index.md) |
| [Windows et SQLite](docs/fr/installation-windows.md) | [Contrat de données](docs/fr/spec.md) | [Installation en service macOS et Linux](docs/fr/installation-unix.md) |

## Développement

```text
uv sync --extra dev --python 3.14.6
uv run --python 3.14.6 ruff check .
uv run --python 3.14.6 ruff format --check .
uv run --python 3.14.6 mypy
uv run --python 3.14.6 pytest
uv build --python 3.14.6
```

## Contribuer

Les portillons, les conventions de commit et la règle de miroir FR/EN de la documentation sont
décrits dans [CONTRIBUTING.md](CONTRIBUTING.md). Une faille se signale en privé, jamais dans une
issue publique : voir [SECURITY.md](SECURITY.md), qui décrit aussi le modèle de menace — endpoint
en loopback seul, absence d'authentification sur le port, et confiance accordée uniquement par un
geste humain.

## Licence

Apache License 2.0. Copyright 2026 Julien Bombled. Voir [LICENSE](LICENSE) et les
[notices tiers](THIRD_PARTY_NOTICES.md).
