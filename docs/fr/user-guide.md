# Guide utilisateur

[Français](user-guide.md) | [English](../en/user-guide.md)

> **Objectif :** utiliser Engram au quotidien sans administrer la base.<br>
> **Temps :** moins d'une minute au début et à la fin d'une tâche.<br>
> **Résultat :** la session retrouve le contexte utile et laisse une prochaine action claire.

Pour l'installation, commencez par le [démarrage en 5 minutes](quick-start.md). Les opérations
avancées sont séparées dans le [guide opérateur](operator-guide.md).

## La routine en trois moments

### 1. Au début : rappeler

Demandez au client d'appeler `recall` avant le travail substantiel :

```text
query = "Engram audit documentation Datacron Cortex"
scope = "project/engram"
```

Une bonne requête nomme :

- le projet ;
- la tâche actuelle ;
- deux ou trois sujets utiles.

**Lisez d'abord :** `current`, `next_action` et `notes.recall_complete`.

### 2. Pendant : travailler, puis mémoriser seulement le durable

Appelez `remember` après une information explicite qui aidera une prochaine session :

- une préférence confirmée ;
- une décision et sa raison utile ;
- un fait vérifié ;
- une correction importante ;
- un changement d'état du projet.

Exemple :

```text
statement = "La documentation utilisateur separe maintenant usage quotidien et operations."
kind = "project_state"
scope = "project/engram"
subject_keys = ["engram:documentation"]
```

N'enregistrez pas les secrets, transcripts, hypothèses, raisonnements intermédiaires, grandes
sorties d'outils ou détails éphémères.

### 3. À la fin : laisser le prochain pas

Si l'état a changé, enregistrez un seul `project_state` concis :

```text
statement = "Le guide utilisateur est termine. Etat: liens a valider. Prochaine action: lancer les tests documentaires."
kind = "project_state"
scope = "project/engram"
subject_keys = ["engram:documentation", "engram:release"]
```

Il doit dire :

1. ce qui est terminé ;
2. l'état actuel ;
3. le blocage confirmé éventuel ;
4. la prochaine action concrète.

## Choisir le type (`kind`)

| Situation | Type |
| --- | --- |
| « Toujours utiliser des chemins absolus dans ce projet » | `preference` |
| « SQLite a été retenu pour ces raisons » | `decision` |
| « La migration est finie ; prochaine action : déployer » | `project_state` |
| « Le service écoute sur le port 8377 » | `fact` |
| « Le test a échoué une fois après un timeout » | `episode` |

Utilisez peu de `subject_keys`, stables et descriptives, par exemple `engram:storage` ou
`project:release`.

## Lire une capsule sans se perdre

Lisez dans cet ordre :

| Zone | Question à se poser |
| --- | --- |
| `current` | Quelle information fiable puis-je utiliser maintenant ? |
| `next_action` | Quel état de projet ou prochain pas est encore utile ? |
| `conflicts` | Plusieurs versions fiables sont-elles non résolues ? |
| `own_pending` | Qu'ai-je proposé depuis ce même client sans validation ? |
| `relevant` | Quel épisode récent peut aider ? |
| `notes` | Le rappel est-il complet et pourquoi ces éléments sont-ils là ? |
| `sources` | Quels identifiants soutiennent la capsule ? |

Chaque `kind` a sa section : `current` regroupe les `preference`, `decision` et `fact` ; un
`project_state` apparaît dans `next_action` et un `episode` dans `relevant`. Une information
attendue qui manque dans `current` est peut-être simplement dans la section de son type.

### Deux règles de sécurité

1. `own_pending` signifie **candidat non confirmé**. Ne l'utilisez jamais pour justifier une action
   irréversible.
2. Si `notes.recall_complete` vaut `false`, l'absence d'un souvenir ne prouve rien. Lisez
   `notes.warnings`, puis resserrez la requête ou demandez une action opérateur.

Si `conflicts` contient plusieurs versions, présentez-les de manière symétrique. Ne choisissez pas
silencieusement celle qui semble la plus pratique.

## Comprendre le résultat de `remember`

| Outcome | Sens |
| --- | --- |
| `created` | Nouveau candidat en quarantaine |
| `retry` | Même observation renvoyée sans nouvelle génération |
| `corroborated` | Nouvelle observation d'un contenu candidat déjà présent |
| `existing_trusted` | Un contenu canonique identique est déjà fiable |
| `renewed` | Nouvelle génération d'un souvenir expiré, encore en quarantaine |

Un contenu nouveau ou renouvelé n'entre pas automatiquement dans `current`.

## Quand demander l'aide d'un opérateur

Passez au [guide opérateur](operator-guide.md) pour :

- attester ou corriger une mémoire ;
- migrer une base ;
- reconstruire FTS ou les vecteurs ;
- consolider vers Datacron ;
- diagnostiquer un code de sortie CLI.

Pour une erreur visible dans le client, lancez `engram doctor`, puis commencez par la
[FAQ](faq.md).

## Quel outil après Engram ?

- Besoin d'une note canonique ou d'une écriture durable : **Datacron**.
- Besoin de rechercher une idée dans beaucoup de documents : **Cortex**.
- Besoin de retrouver le contexte de la session : **Engram**.

Le flux complet est dans [Engram, Datacron et Cortex](datacron-cortex.md).
