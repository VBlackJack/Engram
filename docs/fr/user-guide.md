# Guide utilisateur

[Francais](user-guide.md) | [English](../en/user-guide.md)

> **Objectif :** utiliser Engram au quotidien sans administrer la base.<br>
> **Temps :** moins d'une minute au debut et a la fin d'une tache.<br>
> **Resultat :** la session retrouve le contexte utile et laisse une prochaine action claire.

Pour l'installation, commencez par le [demarrage en 5 minutes](quick-start.md). Les operations
avancees sont separees dans le [guide operateur](operator-guide.md).

## La routine en trois moments

### 1. Au debut : rappeler

Demandez au client d'appeler `recall` avant le travail substantiel :

```text
query = "Engram audit documentation Datacron Cortex"
scope = "project/engram"
```

Une bonne requete nomme :

- le projet ;
- la tache actuelle ;
- deux ou trois sujets utiles.

**Lisez d'abord :** `current`, `next_action` et `notes.recall_complete`.

### 2. Pendant : travailler, puis memoriser seulement le durable

Appelez `remember` apres une information explicite qui aidera une prochaine session :

- une preference confirmee ;
- une decision et sa raison utile ;
- un fait verifie ;
- une correction importante ;
- un changement d'etat du projet.

Exemple :

```text
statement = "La documentation utilisateur separe maintenant usage quotidien et operations."
kind = "project_state"
scope = "project/engram"
subject_keys = ["engram:documentation"]
```

N'enregistrez pas les secrets, transcripts, hypotheses, raisonnements intermediaires, grandes
sorties d'outils ou details ephemeres.

### 3. A la fin : laisser le prochain pas

Si l'etat a change, enregistrez un seul `project_state` concis :

```text
statement = "Le guide utilisateur est termine. Etat: liens a valider. Prochaine action: lancer les tests documentaires."
kind = "project_state"
scope = "project/engram"
subject_keys = ["engram:documentation", "engram:release"]
```

Il doit dire :

1. ce qui est termine ;
2. l'etat actuel ;
3. le blocage confirme eventuel ;
4. la prochaine action concrete.

## Choisir le type (`kind`)

| Situation | Type |
| --- | --- |
| "Toujours utiliser des chemins absolus dans ce projet" | `preference` |
| "SQLite a ete retenu pour ces raisons" | `decision` |
| "La migration est finie ; prochaine action : deployer" | `project_state` |
| "Le service ecoute sur le port 8377" | `fact` |
| "Le test a echoue une fois apres un timeout" | `episode` |

Utilisez peu de `subject_keys`, stables et descriptives, par exemple `engram:storage` ou
`project:release`.

## Lire une capsule sans se perdre

Lisez dans cet ordre :

| Zone | Question a se poser |
| --- | --- |
| `current` | Quelle information fiable puis-je utiliser maintenant ? |
| `next_action` | Quel etat de projet ou prochain pas est encore utile ? |
| `conflicts` | Plusieurs versions fiables sont-elles non resolues ? |
| `own_pending` | Qu'ai-je propose depuis ce meme client sans validation ? |
| `relevant` | Quel episode recent peut aider ? |
| `notes` | Le rappel est-il complet et pourquoi ces elements sont-ils la ? |
| `sources` | Quels identifiants soutiennent la capsule ? |

### Deux regles de securite

1. `own_pending` signifie **candidat non confirme**. Ne l'utilisez jamais pour justifier une action
   irreversible.
2. Si `notes.recall_complete` vaut `false`, l'absence d'un souvenir ne prouve rien. Lisez
   `notes.warnings`, puis resserrez la requete ou demandez une action operateur.

Si `conflicts` contient plusieurs versions, presentez-les de maniere symetrique. Ne choisissez pas
silencieusement celle qui semble la plus pratique.

## Comprendre le resultat de `remember`

| Outcome | Sens |
| --- | --- |
| `created` | Nouveau candidat en quarantaine |
| `retry` | Meme observation renvoyee sans nouvelle generation |
| `corroborated` | Nouvelle observation d'un contenu candidat deja present |
| `existing_trusted` | Un contenu canonique identique est deja fiable |
| `renewed` | Nouvelle generation d'un souvenir expire, encore en quarantaine |

Un contenu nouveau ou renouvele n'entre pas automatiquement dans `current`.

## Quand demander l'aide d'un operateur

Passez au [guide operateur](operator-guide.md) pour :

- attester ou corriger une memoire ;
- migrer une base ;
- reconstruire FTS ou les vecteurs ;
- consolider vers Datacron ;
- diagnostiquer un code de sortie CLI.

Pour une erreur visible dans le client, commencez par la [FAQ](faq.md).

## Quel outil apres Engram ?

- Besoin d'une note canonique ou d'une ecriture durable : **Datacron**.
- Besoin de rechercher une idee dans beaucoup de documents : **Cortex**.
- Besoin de retrouver le contexte de la session : **Engram**.

Le flux complet est dans [Engram, Datacron et Cortex](datacron-cortex.md).
