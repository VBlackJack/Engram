# Guide utilisateur

[Francais](user-guide.md) | [English](../en/user-guide.md)

## Usage quotidien

Engram devient utile quand chaque client suit trois moments simples :

1. **Debut** : appeler `recall` avec le projet, la tache et les mots-cles utiles.
2. **Session** : appeler `remember` apres une decision, une correction explicite, une preference
   confirmee ou un changement d'etat durable.
3. **Fin** : enregistrer un `project_state` qui indique l'etat de sortie et la prochaine action.

Le texte pret a installer est dans [client-protocol.md](client-protocol.md).

### Choisir le kind

| Situation | Kind conseille |
| --- | --- |
| "Toujours utiliser des chemins absolus dans ce projet" | `preference` |
| "La base retenue est SQLite pour ces raisons" | `decision` |
| "La migration est finie ; prochaine action : deployer" | `project_state` |
| "Le service ecoute sur le port 8377" | `fact` |
| "Le test a echoue une fois apres un timeout" | `episode` |

Utiliser des `subject_keys` stables et peu nombreux, par exemple `engram:storage` ou
`project:release`. Ne pas enregistrer de secret, transcript, hypothese, doublon ou detail ephemere.

### Lire une capsule

- `current` contient le contexte stable utilisable maintenant.
- `next_action` indique les etats projet et suites utiles.
- `relevant` contient les episodes recents.
- `conflicts` reste vide sauf si `include_conflicts=true`.
- `own_pending` contient uniquement les candidats quarantaines ecrits par ce client MCP.
- `sources` donne les identifiants cites ; `notes` explique la selection, les omissions budget et
  si le rappel est complet. Ne jamais conclure a une absence si `notes.recall_complete` vaut false.

Traiter `own_pending` comme non confirme. Un candidat ne doit pas guider une action irreversible.

## Attester la memoire de confiance

Definir l'identite d'audit par defaut dans `engram.toml` :

```toml
[attestation]
default_actor = "local-operator"
```

Arreter `engram serve` avant une mutation de confiance afin qu'un seul processus ecrive dans la
base. La CLI applique cette frontiere : `migrate`, `classify`, `attest`, `supersede`, `reindex` et
tous les modes de `consolidate` echouent avec le PID du daemon et l'action corrective tant qu'il
reste actif. `list` reste disponible en read-only. Inspecter les candidats, puis attester le contenu
relu :

```powershell
engram list --status quarantined
engram attest "Le service ecoute sur le port 8377." fact user `
  --subject-key "engram/server-port" `
  --claim-key "engram/server-port" `
  --evidence "review=change-42"
```

Si le kind, le scope et le statement canonique correspondent a un candidat, l'attestation promeut
ce meme identifiant en `active`/`approved`. Pour une correction, fournir explicitement
l'identifiant remplace :

```powershell
engram attest "Le service ecoute sur le port 9000." fact user `
  --subject-key "engram/server-port" `
  --claim-key "engram/server-port" `
  --supersedes 01AAAAAAAAAAAAAAAAAAAAAAAA
```

Pour relier deux entrees existantes, utiliser `engram supersede --old OLD_ID --new NEW_ID`. Les
commandes de confiance emettent du JSON exploitable. Redemarrer le daemon avant de rappeler la
nouvelle memoire active. `engram attest --help` documente provenance, confiance, validite,
observation, evidence et acteur.

## Migrer et classer une base existante

Arreter le daemon et creer une sauvegarde SQLite coherente avant toute migration. Travailler
d'abord sur une copie lors d'une reprise ou d'un incident, puis executer :

```powershell
engram preflight
engram migrate
engram list --unclassified
```

`preflight` exige l'arret du daemon et garde le verrou OS du writer offline. Il ouvre la base source
en lecture seule, fige un snapshot SQLite, copie ce snapshot vers une base temporaire sur disque,
puis execute la migration complete, la reconstruction des index derives et les controles
d'integrite sur cette copie jetable. Les octets de la source ne changent pas. Prevoir assez d'espace
temporaire pour une copie de la base. Les schemas 3 a 5 sont pris en charge ; un schema plus ancien
exige l'upgrade intermediaire 2026.0721.04 indique par le diagnostic.

La version 2026.0730.02 ajoute des plafonds fixes pour les statements, cles sujet, identites
client/audit, preuves et references durables. Elle ne tronque jamais le contenu historique. Si une
ligne incompatible est signalee, conserver la sauvegarde verifiee et utiliser 2026.0730.01 pour
relire ou exporter cette ligne avant de retenter. Dans le rapport JSON,
`vector_rebuild_required: true` signifie que l'ancienne table vectorielle derivee sera remplacee ;
lancer `engram reindex` apres migration lorsque le mode hybride est actif.
`fts_rebuild_required: true` signifie que le schema FTS doit etre recree ; `null` signifie que son
schema correspond et que le contenu external sera tout de meme valide au demarrage.

Inventorier les candidats pending avant upgrade si l'identite d'un client MCP R2 manquait ou etait
vide, ou si son nom/version contenait `%`, `/`, des espaces externes, plus de 128 caracteres, des
controles/bidi, des separateurs de ligne ou des surrogates Unicode. R3 preserve les owners legacy
ordinaires `name/version`, mais les separateurs reserves utilisent une identite `mcp-v2:`
domain-separated par SHA-256 et les composantes invalides sont refusees. Avec la version precedente,
utiliser `engram list --status quarantined` pour relire ou exporter les candidats concernes et
n'attester qu'apres verification humaine ; sinon laisser leur politique TTL anterieure s'appliquer.
Preflight ne peut pas distinguer les noms generiques de la Store API des owners MCP.

`migrate` applique toutes les etapes de schema pending dans une transaction et refuse une ligne historique mal formee sans
laisser de migration partielle. Les `project_state` recoivent leur famille reservee ; les episodes
n'en utilisent pas. Pour chaque preference, decision ou fait retourne par l'inventaire, relire le
contenu et attribuer manuellement une famille semantique stable :

```powershell
engram classify ENTRY_ID --claim-key "engram/server-port"
engram list --unclassified
```

Repeter jusqu'a ce que l'inventaire soit vide. Ne pas deriver automatiquement `claim_key` depuis
`subject_keys` : ces dernieres servent a la recherche, pas a l'identite d'un conflit. Redemarrer
ensuite le daemon.

## Diagnostiquer les erreurs CLI

Les erreurs locales attendues utilisent des codes de sortie stables et un message stderr
actionnable :

| Code | Signification | Action corrective |
| --- | --- | --- |
| `2` | Usage ou configuration invalide | Corriger la commande ou la valeur dans `engram.toml` |
| `3` | Ressource locale indisponible | Liberer port/verrou, reparer la base ou mettre SQLite a jour |
| `4` | Provider externe indisponible | Reparer Datacron ou l'endpoint d'embedding configure |
| `5` | Contention transitoire du store | Reessayer apres la fin de l'ecriture courante |
| `6` | Rapport apply avec proposition failed ou stale | Lire le rapport et generer un nouveau plan |
| `130` | Interruption operateur recue par la CLI | Aucune reprise necessaire |

Ces erreurs n'affichent pas de traceback. Pour diagnostiquer, placer le flag global avant la
commande (`engram --debug serve`) ou definir `ENGRAM_DEBUG=1`. Le guard du runtime SQLite renvoie
directement vers [installation-windows.md](installation-windows.md) si la version chargee est
anterieure a `3.51.3`.

## Reindexer

FTS5 et les vecteurs sont derives :

```powershell
uv run --python 3.14.3 engram reindex
```

En mode `fts`, seuls les index FTS sont reconstruits. En mode `hybrid`, le modele configure est
appele pour recreer les vecteurs. L'index vectoriel live n'est remplace qu'apres la reussite de
chaque batch borne et si aucune autre connexion SQLite n'a commite pendant le rebuild. La table
`entries` et l'audit ne sont pas modifies.

## Evaluer le retrieval

```powershell
uv run --python 3.14.3 engram eval --mode fts --out local/eval
uv run --python 3.14.3 engram eval --mode both --out local/eval
```

Le corpus seede charge 72 entrees et note 88 requetes avec des graders deterministes. Le contrat
de release FTS utilise toujours ses reglages de retrieval et son budget capsule versionnes de 4800
octets UTF-8 conservateurs, independamment des valeurs par defaut du runtime. `both` mesure aussi
l'hybride si l'endpoint repond. Les artefacts `metrics.json` et `rapport-eval.md` restent sous
`local/` et ne sont pas publies.

## Consolider vers Datacron

Configurer `[datacron]` avec un vault, des chemins de lecture et des chemins d'ecriture explicites.
Une allowlist vide interdit l'ecriture, meme si le processus parent definit deja
`DATACRON_WRITE_PATHS`. Le transport CLI par defaut utilise `command = "datacron"` avec
`args = ["mcp", "serve"]`. `startup_timeout_ms`, `request_timeout_ms` et
`shutdown_timeout_ms` bornent chaque frontiere du sous-processus. Un timeout empoisonne la session :
le plan ne doit jamais etre rejoue. Le runtime pince `mcp==1.28.1` : son context manager stdio
ferme stdin puis termine l'arbre de processus (Job Object sous Windows, groupe de processus sous
POSIX) dans deux attentes bornees de 2 s. Le timeout d'arret par defaut de 5 s couvre ce cleanup ;
le thread proprietaire non-daemon empeche la sortie du processus avant son `finally`.

La gate end-to-end automatisee doit utiliser un vault initialise et jetable ainsi qu'une
configuration Engram reservee au test. Elle ne doit jamais cibler le vault durable. Un
`consolidate --plan` contre un vault reel reste une action operateur manuelle et explicite ; aucun
smoke ne contient cette etape.

### 1. Generer le plan

```powershell
uv run --python 3.14.3 engram consolidate --plan --out local/consolidation/plan.json
```

Cette commande est read-only pour Datacron. Elle conserve un snapshot de plan immuable dans la base
SQLite Engram, puis produit les artefacts de revue. Lire le fichier Markdown compagnon, puis editer
le JSON. Chaque proposition reste `pending` par defaut. Le snapshot canonique complet doit tenir
dans 4 Mio UTF-8 ; au-dela, reduire le lot avant de regenerer le plan.

### 2. Valider humainement

Pour chaque proposition :

- verifier `classification`, `proposed_action`, `rel_path`, `heading`, `heading_level`, `new_content`,
  `expected_hash` et les voisins ;
- pour `update`, lire le diff avant/apres ; l'action actuelle est `skip` et aucune section existante
  n'est modifiee sans ancrage d'identite durable independamment verifie ;
- modifier uniquement `decision`, avec `"approve"` ou `"reject"`.

Ne pas recibler une proposition ni modifier un champ genere. Engram compare tous les champs
immuables a son snapshot SQLite et refuse un plan modifie. Apply refuse aussi toute decision restee
`pending` sans consommer le plan.

### 3. Appliquer

```powershell
uv run --python 3.14.3 engram consolidate --apply local/consolidation/plan.json
```

Un resultat peut etre `applied`, `skipped`, `stale` ou `failed`. Le plan est consomme avant toute
tentative d'ecriture externe et ne peut pas etre rejoue. Si un resultat vaut `stale` ou `failed`, le
rapport est conserve et la commande sort avec le code 6. Regenerer un plan depuis la note courante.
Une creation utilise un chemin canonique qui contient l'ID candidat. Si la reponse d'ecriture se
perd apres la creation, le plan suivant relit ce meme chemin et ne classe `redundant` que son
contenu canonique complet identique (fins de ligne et newline final exceptes), au lieu de creer un
doublon.

### 4. Controler la fraicheur

```powershell
uv run --python 3.14.3 engram consolidate --check-freshness
```

Le controle compare les hashes promus. Il marque les divergences stale dans Engram, sans reecrire
Datacron. Une promotion stale est retiree du rappel courant jusqu'a nouvelle revue.
