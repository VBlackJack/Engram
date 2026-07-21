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
