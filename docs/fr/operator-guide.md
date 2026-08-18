# Guide opérateur

[Français](operator-guide.md) | [English](../en/operator-guide.md)

> **Objectif :** administrer Engram sans mélanger ces actions avec l'usage quotidien.<br>
> **Public :** personne qui gère la base, la confiance ou Datacron.<br>
> **Risque :** moyen à élevé ; sauvegarde et arrêt du démon obligatoires quand ils sont indiqués.<br>
> **Version :** Engram `2026.0730.02`, relu le 2026-08-13.

Vous cherchez seulement à rappeler ou proposer un souvenir ? Revenez au
[guide utilisateur](user-guide.md).

Avant toute procédure de cette page, `uv run --python 3.14.6 engram doctor` indique quel fichier de
configuration est en vigueur, à quelle base il aboutit, sa version de schéma et ce qui la détient
actuellement. Chaque étape ci-dessous suppose que ce sont bien celles que vous visiez.

## Choisir une procédure

| Je veux... | Procédure | Démon |
| --- | --- | --- |
| Voir les candidats | `uv run --python 3.14.6 engram list --status quarantined` | Peut rester actif |
| Faire confiance à un candidat relu | [Attester](#attester-un-candidat) | Arrêté |
| Mettre à niveau une base | [Migrer](#migrer-une-base-existante) | Arrêté |
| Reconstruire les index Engram | [Réindexer](#reindexer-engram) | Arrêté |
| Mesurer le retrieval | [Évaluer](#evaluer-le-retrieval) | Peut rester actif |
| Promouvoir vers Datacron | [Consolider](#consolider-vers-datacron) | Arrêté |

`migrate`, `classify`, `attest`, `supersede`, `reindex` et tous les modes de `consolidate`
prennent le même verrou writer que le démon. Ils refusent de démarrer s'il tourne.

## Avant toute mutation

<a id="1-arreter-le-daemon"></a>

### 1. Arrêter le démon

Une seule commande fonctionne pour toutes les installations, quel que soit ce qui a lancé le démon :

```text
uv run --python 3.14.6 engram stop
```

**Vous devez voir :** un JSON avec `"stopped": true`, et `engram.db-wal` comme `engram.db-shm`
disparus. `engram stop` demande au démon propriétaire de la base configurée de la fermer et de
sortir, puis attend sur le verrou de propriété et rapporte s'il s'est réellement arrêté. Si rien ne
détient la base, il répond `"requested": false, "stopped": true`.

C'est décisif pour les installations qui n'ont aucun terminal à interrompre :

| Comment Engram a été lancé | Comment l'arrêter |
| --- | --- |
| Tâche d'ouverture de session Windows (`engram setup autostart --install`) | `engram stop` — il n'y a aucune console à laquelle envoyer `Ctrl+C` |
| Unité utilisateur systemd | `engram stop`, ou `systemctl --user stop engram.service`, qui le lance |
| LaunchAgent launchd | `engram stop` |
| Un terminal qui exécute `engram serve` | `engram stop` depuis un autre terminal, ou `Ctrl+C` dans celui-ci |

**Si `engram stop` échoue :** il nomme le pid qui détient encore la base et laisse la demande en
place. N'effacez pas le fichier de verrou et ne tuez pas le processus avant d'avoir lu le log ;
terminer un démon en pleine écriture est précisément ce qui laisse un journal d'écriture anticipée
derrière lui. `uv run --python 3.14.6 engram doctor` indique le propriétaire et s'il s'agit d'un
démon ou d'un writer hors ligne.

<a id="2-creer-une-sauvegarde-sqlite-coherente"></a>

### 2. Créer une sauvegarde SQLite cohérente

Remplacez le chemin par la valeur effective de `[database].path`, que `engram doctor` affiche.
`ENGRAM_CONFIG` peut sélectionner un autre fichier. Les deux variantes refusent une source absente
et une destination déjà présente.

#### Windows (PowerShell)

```powershell
$engramDbPath = (Resolve-Path "G:/CHEMIN/ABSOLU/engram.db").Path
$engramBackupDir = Join-Path (Split-Path -Parent $engramDbPath) "backups"
New-Item -ItemType Directory -Force -Path $engramBackupDir | Out-Null
$engramBackupPath = Join-Path $engramBackupDir ("engram-" + (Get-Date -Format "yyyyMMdd-HHmmss") + ".db")
if (Test-Path -LiteralPath $engramBackupPath) { throw "Backup destination already exists" }
$env:ENGRAM_BACKUP_SOURCE = $engramDbPath
$env:ENGRAM_BACKUP_DESTINATION = $engramBackupPath
uv run --python 3.14.6 python -c "from os import environ; from pathlib import Path; import sqlite3; source=Path(environ['ENGRAM_BACKUP_SOURCE']); destination=Path(environ['ENGRAM_BACKUP_DESTINATION']); assert source.is_file(), f'source missing: {source}'; assert not destination.exists(), f'destination exists: {destination}'; source_db=sqlite3.connect(source.resolve().as_uri() + '?mode=ro', uri=True); assert source_db.execute('PRAGMA quick_check').fetchone()[0] == 'ok'; backup_db=sqlite3.connect(destination); source_db.backup(backup_db); assert backup_db.execute('PRAGMA quick_check').fetchone()[0] == 'ok'; backup_db.close(); source_db.close(); print(destination)"
Remove-Item Env:ENGRAM_BACKUP_SOURCE
Remove-Item Env:ENGRAM_BACKUP_DESTINATION
```

#### macOS / Linux

```bash
ENGRAM_BACKUP_SOURCE="$(cd "$(dirname /chemin/absolu/engram.db)" && pwd)/$(basename /chemin/absolu/engram.db)"
ENGRAM_BACKUP_DIR="$(dirname "$ENGRAM_BACKUP_SOURCE")/backups"
mkdir -p "$ENGRAM_BACKUP_DIR"
ENGRAM_BACKUP_DESTINATION="$ENGRAM_BACKUP_DIR/engram-$(date +%Y%m%d-%H%M%S).db"
export ENGRAM_BACKUP_SOURCE ENGRAM_BACKUP_DESTINATION
uv run --python 3.14.6 python -c "from os import environ; from pathlib import Path; import sqlite3; source=Path(environ['ENGRAM_BACKUP_SOURCE']); destination=Path(environ['ENGRAM_BACKUP_DESTINATION']); assert source.is_file(), f'source missing: {source}'; assert not destination.exists(), f'destination exists: {destination}'; source_db=sqlite3.connect(source.resolve().as_uri() + '?mode=ro', uri=True); assert source_db.execute('PRAGMA quick_check').fetchone()[0] == 'ok'; backup_db=sqlite3.connect(destination); source_db.backup(backup_db); assert backup_db.execute('PRAGMA quick_check').fetchone()[0] == 'ok'; backup_db.close(); source_db.close(); print(destination)"
unset ENGRAM_BACKUP_SOURCE ENGRAM_BACKUP_DESTINATION
```

**Vous devez voir :** un chemin de sauvegarde horodaté et aucun échec de `quick_check`. Conservez
une copie hors du dossier de travail pour une opération critique.

<a id="3-savoir-comment-redemarrer"></a>

### 3. Savoir comment redémarrer

Chaque procédure ci-dessous se termine par un redémarrage. Utilisez celui qui correspond au mode
d'installation :

| Installation | Redémarrage |
| --- | --- |
| Tâche d'ouverture de session Windows | `uv run --python 3.14.6 engram setup autostart --status` montre qu'elle est enregistrée ; démarrez-la avec `uv run --python 3.14.6 engram setup autostart --install`, qui converge et lance le démon quand la base est libre |
| Unité utilisateur systemd | `systemctl --user start engram.service` |
| LaunchAgent launchd | `launchctl kickstart -k gui/$(id -u)/com.github.vblackjack.engram` |
| Premier plan | `uv run --python 3.14.6 engram serve` |

Les fichiers d'unité et les tableaux de commandes complets sont dans
[Installer en service sous macOS et Linux](installation-unix.md).

Confirmez avec `uv run --python 3.14.6 engram doctor` : `daemon` doit indiquer `serving` et
`endpoint` doit indiquer que l'URL accepte les connexions.

## Attester un candidat

### 1. Inventorier

```text
uv run --python 3.14.6 engram list --status quarantined
```

Relisez le statement, le type (`kind`), la portée (`scope`), les sujets et les preuves. Ne copiez
pas automatiquement un lot dans la zone de confiance.

### 2. Attester le contenu exact

`attest` s'apparie sur le contenu canonique, pas sur un identifiant. Retaper un statement avec une
formulation différente crée une **nouvelle** entrée au lieu de promouvoir celle que vous visiez, et
sort quand même en `0`. Copiez le statement depuis l'inventaire plutôt que de le retaper.

#### Windows (PowerShell)

```powershell
uv run --python 3.14.6 engram attest "Le service ecoute sur le port 8377." fact user `
  --subject-key "engram/server-port" `
  --claim-key "engram/server-port" `
  --evidence "review=change-42"
```

#### macOS / Linux

```bash
uv run --python 3.14.6 engram attest "Le service ecoute sur le port 8377." fact user \
  --subject-key "engram/server-port" \
  --claim-key "engram/server-port" \
  --evidence "review=change-42"
```

**Vous devez voir :** un résultat JSON `active` / `approved`. Si le type, la portée et le contenu
canonique correspondent au candidat, Engram promeut son identifiant existant.

`--claim-key` est **obligatoire** pour `preference`, `decision` et `fact` : c'est l'identité de la
famille de conflit, et une entrée qui en est dépourvue est omise du rappel en fail-closed.
`--subject-key` est un indice de découverte et ne s'y substitue pas.

### Options qui pilotent la confiance et la durée de vie

| Option | Valeurs acceptées | Défaut | Ce qu'elle change |
| --- | --- | --- | --- |
| `--source-type` | `human`, `tool_verified` | `human` | Provenance enregistrée pour l'entrée. `tool_verified` s'applique à un énoncé qu'un outil a prouvé, pas à un énoncé qu'une personne a jugé. Un client ne peut affirmer ni l'un ni l'autre via MCP ; seule cette commande le peut. |
| `--confidence` | `high`, `medium`, `low` | `high` | Confiance stockée avec l'entrée. Abaissez-la délibérément pour un contenu relu mais incertain ; à claim égale, une entrée `high` prime sur une entrée `low`. |
| `--valid-from` | `AAAA-MM-JJ` | non défini | Premier jour où l'énoncé tient. Avant cette date, l'entrée existe mais n'est pas courante. Utile pour attester une décision qui prend effet plus tard. |
| `--valid-until` | `AAAA-MM-JJ` | non défini | Dernier jour où l'énoncé tient. Après cette date, l'entrée cesse d'être courante. C'est la façon honnête d'enregistrer ce que vous savez déjà périssable. |
| `--observed-at` | ISO-8601 avec décalage UTC, par exemple `2026-08-13T09:00:00+02:00` | maintenant | Moment de l'observation. Antidatez-la quand vous attestez aujourd'hui un fait constaté plus tôt : les départages par récence lisent cette valeur, pas l'instant où vous avez tapé la commande. |

`--valid-from` et `--valid-until` sont des jours calendaires ; `--observed-at` est un instant et
exige un décalage explicite. `--valid-until` borne la durée de vie d'un énoncé et n'a rien à voir
avec la politique `[ttl_days]`, qui s'applique par `kind` — et, pour un candidat que personne n'a
attesté, avec le plafond `candidate_max_days` qui le borne quoi que dise son `kind`.

### Types de preuve acceptés

`--evidence` prend `TYPE=REF` et se répète. Seuls quatre types sont acceptés ; tout autre est
refusé :

| Type | À utiliser pour |
| --- | --- |
| `tool_result` | L'identifiant ou la référence d'une exécution d'outil ayant produit l'énoncé |
| `datacron_note` | La note Datacron qui porte la version durable |
| `human_message` | Le message dans lequel une personne l'a énoncé ou confirmé |
| `review` | La revue, le changement ou le ticket dans lequel il a été validé |

La référence elle-même est opaque pour Engram : elle est stockée et restituée, jamais résolue ni
téléchargée.

### Corriger une entrée

Pour corriger une entrée, passez l'identifiant remplacé :

```text
uv run --python 3.14.6 engram attest "Le service ecoute sur le port 9000." fact user --subject-key "engram/server-port" --claim-key "engram/server-port" --supersedes 01AAAAAAAAAAAAAAAAAAAAAAAA
```

Pour relier deux entrées déjà présentes :

```text
uv run --python 3.14.6 engram supersede --old OLD_ID --new NEW_ID
```

### 3. Redémarrer et vérifier

Redémarrez le démon selon son mode d'installation — voir
[Savoir comment redémarrer](#3-savoir-comment-redemarrer) — puis rappelez le sujet.

**L'endroit où regarder dépend du `kind` attesté.** La capsule ne place pas tout dans `current` :

| Kind attesté | Section de capsule où il apparaît |
| --- | --- |
| `preference` | `current` |
| `decision` | `current` |
| `fact` | `current` |
| `project_state` | `next_action` |
| `episode` | `relevant` |

Un `project_state` ou un `episode` qui n'apparaît jamais dans `current` se comporte correctement ;
chercher l'un des deux dans `current` est la façon habituelle de prendre une attestation réussie
pour un échec. Une entrée engagée dans une famille de conflit non résolue apparaît sous
`conflicts`, quel que soit son `kind`, et une entrée encore dépourvue de `claim_key` est omise
entièrement avec l'avertissement `unclassified_claim_omitted`.

Une attestation humaine ne dispense pas de relire un conflit retourné par Engram.

## Migrer une base existante

### 1. Inventorier les identités R2

Avant de remplacer l'environnement `2026.0730.01`, conservez-le et exécutez :

```text
uv run --python 3.14.6 engram list --status quarantined
```

Relisez ou exportez les candidats dont l'identité client MCP était absente ou vide, contenait `%`,
`/`, des espaces externes, plus de 128 caractères, des contrôles ou du bidi, des séparateurs de
ligne ou des surrogates Unicode. R3 conserve les owners ordinaires `name/version`, utilise un
namespace `mcp-v2:` domain-separated par SHA-256 pour les séparateurs réservés et refuse les
composantes invalides.

Le preflight ne peut pas distinguer tous les owners génériques créés par la Store API des owners
MCP. Ne devinez pas leur identité : relisez-les humainement ou laissez leur ancienne politique TTL
s'appliquer.

### 2. Adapter la configuration R3

```toml
[capsule]
default_token_budget = 4800
min_token_budget = 1200
max_token_budget = 32768
```

`max_token_budget` est le plafond qu'un client a le droit de demander, pas ce que coûte un rappel :
l'élever n'agrandit aucun rappel, puisque c'est `default_token_budget` qui décide de cela. Il valait
`6000` jusqu'à 2026.813.1, et une famille de conflits à six versions enregistrées dépasse 6000
octets sérialisés — ces familles étaient donc inatteignables par les outils MCP, quel que soit le
budget qu'un client pouvait demander.

### 3. Prouver la migration sans toucher la source

Après sauvegarde et arrêt du démon :

```text
uv run --python 3.14.6 engram preflight
```

`preflight` ouvre la source en lecture seule, fige un snapshot et teste la migration complète sur
une copie temporaire. Les schémas 3 à 5 sont pris en charge. Engram ne tronque jamais une ancienne
valeur qui dépasse une nouvelle limite. Prévoyez au moins la taille de la base, plus une marge de
travail, sur le volume temporaire.

**Continuez uniquement si :** le rapport annonce la compatibilité. Si une ligne est nommée,
relisez-la ou exportez-la avec la version indiquée par le diagnostic.

Interprétez aussi les index dérivés :

- `fts_rebuild_required: true` : le schéma FTS doit être recréé ;
- `fts_rebuild_required: null` : le schéma correspond ; le contenu external-content sera tout de
  même validé au démarrage ;
- `vector_rebuild_required: true` : les anciens vecteurs doivent être reconstruits après migration
  si le mode hybride est actif.

### 4. Migrer et classer

```text
uv run --python 3.14.6 engram migrate
uv run --python 3.14.6 engram list --unclassified
```

Pour chaque `preference`, `decision` ou `fact` historique, choisissez manuellement une famille
sémantique :

```text
uv run --python 3.14.6 engram classify ENTRY_ID --claim-key "engram/server-port"
uv run --python 3.14.6 engram list --unclassified
```

Ne dérivez pas `claim_key` en masse depuis `subject_keys` : les sujets aident la recherche, tandis
que la claim key définit l'identité d'un conflit.

### 5. Reconstruire si le rapport le demande

Si `vector_rebuild_required` vaut `true` et que le mode hybride est actif :

```text
uv run --python 3.14.6 engram reindex
```

Redémarrez ensuite le démon selon son mode d'installation — voir
[Savoir comment redémarrer](#3-savoir-comment-redemarrer) — et testez un rappel connu.

### 6. Revenir à la version précédente

Une migration ne se remonte pas. Un Engram plus ancien refuse une base dont le schéma est plus
récent que celui qu'il connaît — `Database schema version 6 is newer than supported version 5` —
donc revenir à la version précédente est une **restauration**, pas un simple changement de version.
Réinstaller l'ancienne version seule laisse un démon qui n'ouvre plus la base du tout.

1. Arrêtez le démon : `uv run --python 3.14.6 engram stop`.
2. Sauvegardez la base actuelle, déjà migrée, comme dans
   [Créer une sauvegarde SQLite cohérente](#2-creer-une-sauvegarde-sqlite-coherente). Conservez-la :
   c'est la seule copie de tout ce qui a été écrit depuis la migration.
3. Remettez en place la sauvegarde d'avant migration, et supprimez les éventuels `-wal` et `-shm`
   à côté d'elle. Un journal d'écriture anticipée appartient à la base pour laquelle il a été
   écrit ; en laisser un auprès d'un fichier restauré est la façon dont un retour arrière perd des
   données qu'il semblait conserver.
4. Installez la version précédente.
5. Redémarrez, puis vérifiez avec `engram doctor`, `PRAGMA integrity_check` et le nombre d'entrées
   relevé avant la migration.

**Tout ce qui a été écrit après la migration est perdu par cette procédure**, puisque la sauvegarde
restaurée à l'étape 3 lui est antérieure. Le point de reprise est l'instant où cette sauvegarde a
été prise : prenez-la juste avant de migrer, et traitez l'étape 2 comme le relevé de ce qu'un retour
arrière jetterait.

<a id="reindexer-engram"></a>

## Réindexer Engram

Arrêtez le démon avec `uv run --python 3.14.6 engram stop`, puis :

```text
uv run --python 3.14.6 engram reindex
```

- En mode `fts`, Engram reconstruit uniquement FTS.
- En mode `hybrid`, il recrée aussi les vecteurs avec l'endpoint configuré.
- `entries` et `audit_log` ne sont pas modifiés.
- L'index live n'est remplacé qu'après la réussite du rebuild.

Redémarrez le démon selon son mode d'installation — voir
[Savoir comment redémarrer](#3-savoir-comment-redemarrer) — puis rappelez une requête connue.

<a id="evaluer-le-retrieval"></a>

## Évaluer le retrieval

```text
uv run --python 3.14.6 engram eval --mode fts --out local/eval
uv run --python 3.14.6 engram eval --mode both --out local/eval
```

**Vous devez voir :** `metrics.json` et `rapport-eval.md` sous `local/eval`. Le corpus seedé et les
graders sont déterministes ; ils n'accèdent pas au vault Datacron.

## Consolider vers Datacron

Avant de commencer :

- configurez le gateway comme indiqué dans
  [Mise en place avec un vault commun](datacron-cortex.md#mise-en-place-avec-un-vault-commun) ;
- arrêtez le démon avec `uv run --python 3.14.6 engram stop` ;
- ne ciblez jamais le vault durable depuis un test automatisé.

### 1. Générer un plan

```text
uv run --python 3.14.6 engram consolidate --plan --out local/consolidation/plan.json
```

**Vous devez voir :** un JSON et un rapport Markdown. Cette étape lit Datacron mais n'y écrit pas.
Engram ancre aussi un snapshot immuable du plan dans SQLite.

### 2. Relire

Pour chaque proposition, vérifiez :

- la classification et l'action ;
- `rel_path`, le heading et son niveau ;
- le nouveau contenu, le hash attendu et les voisins ;
- le diff lorsqu'il existe.

Modifiez uniquement `decision` avec `"approve"` ou `"reject"`. Toute proposition restée
`"pending"` bloque l'apply. Ne modifiez ni la cible, ni le contenu généré, ni les hashes.

### 3. Appliquer une fois

```text
uv run --python 3.14.6 engram consolidate --apply local/consolidation/plan.json
```

Le plan est consommé avant les écritures et ne peut pas être rejoué. Un résultat peut être
`applied`, `skipped`, `stale` ou `failed`.

- `stale` : Datacron a changé ; régénérez un plan et relisez-le.
- `failed` : conservez le rapport et corrigez la dépendance.
- code de sortie `6` : au moins une proposition est `stale` ou `failed`.
- une proposition `update` reste actuellement en lecture seule et produit `skip`.

### 4. Contrôler la fraîcheur

```text
uv run --python 3.14.6 engram consolidate --check-freshness
```

Une divergence retire la promotion du rappel courant. Engram ne réécrit pas Datacron pour masquer
le problème.

### 5. Redémarrer et synchroniser Cortex

Redémarrez le démon selon son mode d'installation — voir
[Savoir comment redémarrer](#3-savoir-comment-redemarrer). Pour un lancement au premier plan :

```text
uv run --python 3.14.6 engram serve
```

Dans un autre terminal, si Cortex indexe ce vault :

```text
cortex sync
```

Une consolidation Datacron ne synchronise jamais Cortex automatiquement.

## Codes de sortie

| Code | Signification | Première action |
| --- | --- | --- |
| `2` | Usage ou configuration invalide | Corriger la commande ou `engram.toml` ; `engram doctor` nomme la clé fautive |
| `3` | Ressource locale indisponible | Vérifier port, verrou, base et runtime SQLite ; `engram doctor` les mesure tous |
| `4` | Dépendance externe indisponible | Vérifier Datacron ou l'endpoint d'embeddings |
| `5` | Contention transitoire du store | Attendre la fin de l'écriture et retenter |
| `6` | Apply avec résultat `failed` ou `stale` | Lire le rapport et générer un nouveau plan |
| `130` | Interruption opérateur | Vérifier l'état, puis reprendre explicitement |

`engram setup autostart` sort aussi en `2` sous macOS et Linux, par construction : la commande est
réservée à Windows. Voir [Installer en service sous macOS et Linux](installation-unix.md).

Les erreurs connues n'affichent pas de traceback. Pour un diagnostic ponctuel :

```text
uv run --python 3.14.6 engram --debug COMMAND
```

ou définissez `ENGRAM_DEBUG=1`.

## Reprise après incident

1. Lancez d'abord `uv run --python 3.14.6 engram doctor`. Il rapporte l'interpréteur, la version de
   SQLite, le fichier de configuration réellement résolu, la base et son schéma, le propriétaire du
   verrou, l'endpoint et le fichier de log — et nomme une réparation pour chaque échec.
2. Ne lancez pas plusieurs writers pour « débloquer » la base.
3. Conservez la base, les fichiers WAL/SHM éventuels, les logs et le rapport de commande.
4. Travaillez sur une copie et exécutez `uv run --python 3.14.6 engram preflight`.
5. Ne restaurez une sauvegarde qu'après avoir arrêté tous les processus Engram avec
   `uv run --python 3.14.6 engram stop` et identifié les changements qui seraient perdus.
6. Utilisez la [FAQ](faq.md) pour le symptôme exact.
