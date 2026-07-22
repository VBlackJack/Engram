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
- `sources` donne les identifiants cites ; `notes` explique la selection et les omissions budget.

Traiter `own_pending` comme non confirme. Un candidat ne doit pas guider une action irreversible.

## Attester la memoire de confiance

Definir l'identite d'audit par defaut dans `engram.toml` :

```toml
[attestation]
default_actor = "local-operator"
```

Arreter `engram serve` avant une mutation de confiance afin qu'un seul processus ecrive dans la
base. Inspecter les candidats, puis attester le contenu relu :

```powershell
engram list --status quarantined
engram attest "Le service ecoute sur le port 8377." fact user `
  --subject-key "engram/server-port" `
  --evidence "review=change-42"
```

Si le kind, le scope et le statement canonique correspondent a un candidat, l'attestation promeut
ce meme identifiant en `active`/`approved`. Pour une correction, fournir explicitement
l'identifiant remplace :

```powershell
engram attest "Le service ecoute sur le port 9000." fact user `
  --subject-key "engram/server-port" `
  --supersedes 01AAAAAAAAAAAAAAAAAAAAAAAA
```

Pour relier deux entrees existantes, utiliser `engram supersede --old OLD_ID --new NEW_ID`. Les
commandes de confiance emettent du JSON exploitable. Redemarrer le daemon avant de rappeler la
nouvelle memoire active. `engram attest --help` documente provenance, confiance, validite,
observation, evidence et acteur.

## Reindexer

FTS5 et les vecteurs sont derives :

```powershell
uv run --python 3.14.3 engram reindex
```

En mode `fts`, seuls les index FTS sont reconstruits. En mode `hybrid`, le modele configure est
appele pour recreer les vecteurs. La table `entries` et l'audit ne sont pas modifies.

## Evaluer le retrieval

```powershell
uv run --python 3.14.3 engram eval --mode fts --out local/eval
uv run --python 3.14.3 engram eval --mode both --out local/eval
```

Le corpus seede charge 72 entrees et note 64 requetes avec des graders deterministes. `both`
mesure aussi l'hybride si l'endpoint repond. Les artefacts `metrics.json` et `rapport-eval.md`
restent sous `local/` et ne sont pas publies.

## Consolider vers Datacron

Configurer `[datacron]` avec un vault, des chemins de lecture et des chemins d'ecriture explicites.
Une allowlist vide interdit l'ecriture.

### 1. Generer le plan

```powershell
uv run --python 3.14.3 engram consolidate --plan --out local/consolidation/plan.json
```

Cette commande est read-only pour Datacron et Engram. Lire le fichier Markdown compagnon, puis
editer le JSON. Chaque proposition reste `pending` par defaut.

### 2. Valider humainement

Pour chaque proposition :

- choisir `decision: "approve"` ou `"reject"` ;
- verifier `classification`, `proposed_action`, `rel_path`, `heading`, `new_content` ;
- conserver `expected_hash` : il porte la protection CAS.

### 3. Appliquer

```powershell
uv run --python 3.14.3 engram consolidate --apply local/consolidation/plan.json
```

Un resultat peut etre `applied`, `skipped`, `stale` ou `failed`. Ne pas editer un hash pour
contourner `stale` : regenerer le plan depuis la note courante.

### 4. Controler la fraicheur

```powershell
uv run --python 3.14.3 engram consolidate --check-freshness
```

Le controle compare les hashes promus. Il marque les divergences stale dans Engram, sans reecrire
Datacron. Une promotion stale est retiree du rappel courant jusqu'a nouvelle revue.
