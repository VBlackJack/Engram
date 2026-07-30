# Guide operateur

[Francais](operator-guide.md) | [English](../en/operator-guide.md)

> **Objectif :** administrer Engram sans melanger ces actions avec l'usage quotidien.<br>
> **Public :** personne qui gere la base, la confiance ou Datacron.<br>
> **Risque :** moyen a eleve ; sauvegarde et arret du daemon obligatoires quand ils sont indiques.<br>
> **Version :** Engram `2026.0730.02`.

Vous cherchez seulement a rappeler ou proposer un souvenir ? Revenez au
[guide utilisateur](user-guide.md).

## Choisir une procedure

| Je veux... | Procedure | Daemon |
| --- | --- | --- |
| Voir les candidats | `uv run --python 3.14.3 engram list --status quarantined` | Peut rester actif |
| Faire confiance a un candidat relu | [Attester](#attester-un-candidat) | Arrete |
| Mettre a niveau une base | [Migrer](#migrer-une-base-existante) | Arrete |
| Reconstruire les index Engram | [Reindexer](#reindexer-engram) | Arrete |
| Mesurer le retrieval | [Evaluer](#evaluer-le-retrieval) | Peut rester actif |
| Promouvoir vers Datacron | [Consolider](#consolider-vers-datacron) | Arrete |

`migrate`, `classify`, `attest`, `supersede`, `reindex` et tous les modes de `consolidate`
prennent le meme verrou writer que le daemon. Ils refusent de demarrer s'il tourne.

## Avant toute mutation

### 1. Arreter le daemon

Interrompez proprement le terminal qui execute `uv run --python 3.14.3 engram serve`.

**Vous devez voir :** le processus se termine. Si une commande affiche encore un PID proprietaire,
n'effacez pas le fichier de verrou ; identifiez d'abord ce processus.

### 2. Creer une sauvegarde SQLite coherente

Remplacez le premier chemin par la valeur effective de `[database].path`, resolue depuis le dossier
de `engram.toml`. `ENGRAM_CONFIG` peut selectionner un autre fichier. La commande refuse une source
absente et une destination deja presente :

```powershell
$engramDbPath = (Resolve-Path "G:/CHEMIN/ABSOLU/engram.db").Path
$engramBackupDir = Join-Path (Split-Path -Parent $engramDbPath) "backups"
New-Item -ItemType Directory -Force -Path $engramBackupDir | Out-Null
$engramBackupPath = Join-Path $engramBackupDir ("engram-" + (Get-Date -Format "yyyyMMdd-HHmmss") + ".db")
if (Test-Path -LiteralPath $engramBackupPath) { throw "Backup destination already exists" }
$env:ENGRAM_BACKUP_SOURCE = $engramDbPath
$env:ENGRAM_BACKUP_DESTINATION = $engramBackupPath
uv run --python 3.14.3 python -c "from os import environ; from pathlib import Path; import sqlite3; source=Path(environ['ENGRAM_BACKUP_SOURCE']); destination=Path(environ['ENGRAM_BACKUP_DESTINATION']); assert source.is_file(), f'source missing: {source}'; assert not destination.exists(), f'destination exists: {destination}'; source_db=sqlite3.connect(source.resolve().as_uri() + '?mode=ro', uri=True); assert source_db.execute('PRAGMA quick_check').fetchone()[0] == 'ok'; backup_db=sqlite3.connect(destination); source_db.backup(backup_db); assert backup_db.execute('PRAGMA quick_check').fetchone()[0] == 'ok'; backup_db.close(); source_db.close(); print(destination)"
Remove-Item Env:ENGRAM_BACKUP_SOURCE
Remove-Item Env:ENGRAM_BACKUP_DESTINATION
```

**Vous devez voir :** un chemin de sauvegarde horodate et aucun echec de `quick_check`. Conservez
une copie hors du dossier de travail pour une operation critique.

## Attester un candidat

### 1. Inventorier

```powershell
uv run --python 3.14.3 engram list --status quarantined
```

Relisez le statement, le type (`kind`), la portee (`scope`), les sujets et les preuves. Ne copiez
pas automatiquement un lot dans la zone de confiance.

### 2. Attester le contenu exact

```powershell
uv run --python 3.14.3 engram attest "Le service ecoute sur le port 8377." fact user `
  --subject-key "engram/server-port" `
  --claim-key "engram/server-port" `
  --evidence "review=change-42"
```

**Vous devez voir :** un resultat JSON `active` / `approved`. Si le type, la portee et le contenu
canonique correspondent au candidat, Engram promeut son identifiant existant.

Pour corriger une entree, passez l'identifiant remplace :

```powershell
uv run --python 3.14.3 engram attest "Le service ecoute sur le port 9000." fact user `
  --subject-key "engram/server-port" `
  --claim-key "engram/server-port" `
  --supersedes 01AAAAAAAAAAAAAAAAAAAAAAAA
```

Pour relier deux entrees deja presentes :

```powershell
uv run --python 3.14.3 engram supersede --old OLD_ID --new NEW_ID
```

### 3. Redemarrer

```powershell
uv run --python 3.14.3 engram serve
```

Rappelez le sujet et verifiez la presence dans `current`. Une attestation humaine ne dispense pas
de relire un conflit retourne par Engram.

## Migrer une base existante

### 1. Inventorier les identites R2

Avant de remplacer l'environnement `2026.0730.01`, conservez-le et executez :

```powershell
uv run --python 3.14.3 engram list --status quarantined
```

Relisez ou exportez les candidats dont l'identite client MCP etait absente/vide, contenait `%`,
`/`, des espaces externes, plus de 128 caracteres, des controles ou bidi, des separateurs de ligne
ou des surrogates Unicode. R3 conserve les owners ordinaires `name/version`, utilise un namespace
`mcp-v2:` domain-separated par SHA-256 pour les separateurs reserves et refuse les composantes
invalides.

Le preflight ne peut pas distinguer tous les owners generiques crees par la Store API des owners
MCP. Ne devinez pas leur identite : relisez-les humainement ou laissez leur ancienne politique TTL
s'appliquer.

### 2. Adapter la configuration R3

```toml
[capsule]
default_token_budget = 4800
min_token_budget = 1200
max_token_budget = 6000
```

### 3. Prouver la migration sans toucher la source

Apres sauvegarde et arret du daemon :

```powershell
uv run --python 3.14.3 engram preflight
```

`preflight` ouvre la source en lecture seule, fige un snapshot et teste la migration complete sur
une copie temporaire. Les schemas 3 a 5 sont pris en charge. Engram ne tronque jamais une ancienne
valeur qui depasse une nouvelle limite. Prevoyez au moins la taille de la base, plus une marge de
travail, sur le volume temporaire.

**Continuez uniquement si :** le rapport annonce la compatibilite. Si une ligne est nommee,
relisez-la ou exportez-la avec la version indiquee par le diagnostic.

Interpretez aussi les index derives :

- `fts_rebuild_required: true` : le schema FTS doit etre recree ;
- `fts_rebuild_required: null` : le schema correspond ; le contenu external-content sera tout de
  meme valide au demarrage ;
- `vector_rebuild_required: true` : les anciens vecteurs doivent etre reconstruits apres migration
  si le mode hybride est actif.

### 4. Migrer et classer

```powershell
uv run --python 3.14.3 engram migrate
uv run --python 3.14.3 engram list --unclassified
```

Pour chaque `preference`, `decision` ou `fact` historique, choisissez manuellement une famille
semantique :

```powershell
uv run --python 3.14.3 engram classify ENTRY_ID --claim-key "engram/server-port"
uv run --python 3.14.3 engram list --unclassified
```

Ne derivez pas `claim_key` en masse depuis `subject_keys` : les sujets aident la recherche, tandis
que la claim key definit l'identite d'un conflit.

### 5. Reconstruire si le rapport le demande

Si `vector_rebuild_required` vaut `true` et que le mode hybride est actif :

```powershell
uv run --python 3.14.3 engram reindex
```

Redemarrez ensuite le daemon et testez un rappel connu.

## Reindexer Engram

Arretez le daemon, puis :

```powershell
uv run --python 3.14.3 engram reindex
```

- En mode `fts`, Engram reconstruit uniquement FTS.
- En mode `hybrid`, il recree aussi les vecteurs avec l'endpoint configure.
- `entries` et `audit_log` ne sont pas modifies.
- L'index live n'est remplace qu'apres la reussite du rebuild.

Redemarrez `uv run --python 3.14.3 engram serve`, puis rappelez une requete connue.

## Evaluer le retrieval

```powershell
uv run --python 3.14.3 engram eval --mode fts --out local/eval
uv run --python 3.14.3 engram eval --mode both --out local/eval
```

**Vous devez voir :** `metrics.json` et `rapport-eval.md` sous `local/eval`. Le corpus seede et les
graders sont deterministes ; ils n'accedent pas au vault Datacron.

## Consolider vers Datacron

Avant de commencer :

- configurez le gateway comme indique dans
  [Mise en place avec un vault commun](datacron-cortex.md#mise-en-place-avec-un-vault-commun) ;
- arretez le daemon ;
- ne ciblez jamais le vault durable depuis un test automatise.

### 1. Generer un plan

```powershell
uv run --python 3.14.3 engram consolidate --plan --out local/consolidation/plan.json
```

**Vous devez voir :** un JSON et un rapport Markdown. Cette etape lit Datacron mais n'y ecrit pas.
Engram ancre aussi un snapshot immuable du plan dans SQLite.

### 2. Relire

Pour chaque proposition, verifiez :

- la classification et l'action ;
- `rel_path`, le heading et son niveau ;
- le nouveau contenu, le hash attendu et les voisins ;
- le diff lorsqu'il existe.

Modifiez uniquement `decision` avec `"approve"` ou `"reject"`. Toute proposition restee
`"pending"` bloque l'apply. Ne modifiez ni la cible, ni le contenu genere, ni les hashes.

### 3. Appliquer une fois

```powershell
uv run --python 3.14.3 engram consolidate --apply local/consolidation/plan.json
```

Le plan est consomme avant les ecritures et ne peut pas etre rejoue. Un resultat peut etre
`applied`, `skipped`, `stale` ou `failed`.

- `stale` : Datacron a change ; regenerez un plan et relisez-le.
- `failed` : conservez le rapport et corrigez la dependance.
- code de sortie `6` : au moins une proposition est `stale` ou `failed`.
- une proposition `update` reste actuellement en lecture seule et produit `skip`.

### 4. Controler la fraicheur

```powershell
uv run --python 3.14.3 engram consolidate --check-freshness
```

Une divergence retire la promotion du rappel courant. Engram ne reecrit pas Datacron pour masquer
le probleme.

### 5. Redemarrer et synchroniser Cortex

```powershell
uv run --python 3.14.3 engram serve
```

Dans un autre terminal, si Cortex indexe ce vault :

```powershell
cortex sync
```

Une consolidation Datacron ne synchronise jamais Cortex automatiquement.

## Codes de sortie

| Code | Signification | Premiere action |
| --- | --- | --- |
| `2` | Usage ou configuration invalide | Corriger la commande ou `engram.toml` |
| `3` | Ressource locale indisponible | Verifier port, verrou, base et runtime SQLite |
| `4` | Dependance externe indisponible | Verifier Datacron ou l'endpoint d'embeddings |
| `5` | Contention transitoire du store | Attendre la fin de l'ecriture et retenter |
| `6` | Apply avec resultat `failed` ou `stale` | Lire le rapport et generer un nouveau plan |
| `130` | Interruption operateur | Verifier l'etat, puis reprendre explicitement |

Les erreurs connues n'affichent pas de traceback. Pour un diagnostic ponctuel :

```powershell
uv run --python 3.14.3 engram --debug COMMAND
```

ou definissez `ENGRAM_DEBUG=1`.

## Reprise apres incident

1. Ne lancez pas plusieurs writers pour "debloquer" la base.
2. Conservez la base, les fichiers WAL/SHM eventuels, les logs et le rapport de commande.
3. Travaillez sur une copie et executez `uv run --python 3.14.3 engram preflight`.
4. Ne restaurez une sauvegarde qu'apres avoir arrete tous les processus Engram et identifie les
   changements qui seraient perdus.
5. Utilisez la [FAQ](faq.md) pour le symptome exact.
