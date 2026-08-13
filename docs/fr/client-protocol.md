# Protocole client Engram

[Français](client-protocol.md) | [English](../en/client-protocol.md)

> **Objectif :** apprendre au client quand appeler Engram.<br>
> **À faire :** copier le [bloc prêt à coller](#texte-dinstruction-pret-a-coller), puis revenir au
> [guide utilisateur](user-guide.md).<br>
> **Temps :** 2 minutes.

## Pourquoi ce protocole existe

MCP transmet des appels d'outils ; Engram ne voit pas passivement la conversation, les fichiers
ouverts ou la fin d'une session. Chaque client doit donc décider quand rappeler et quand proposer
un souvenir. Ce contrat rend ce comportement identique dans Claude, Codex et Gemini.

Exemple minimal de début de tâche :

```text
recall
query = "nom-du-projet tache sujets-utiles"
scope = "project/nom-du-projet"
```

## Les trois moments

### 1. Au démarrage

Appeler `recall` avant le travail substantiel avec une requête courte qui nomme le projet, la tâche
et les sujets utiles. Utiliser le scope le plus précis connu. Lire `current` et `next_action` avant
d'agir ; traiter `conflicts` et `own_pending` comme non résolus.

### 2. Pendant la session

Appeler `remember` seulement après une information durable et explicite : décision, correction,
préférence confirmée, fait vérifié ou changement d'état. Choisir le kind et des `subject_keys`
stables. Ne pas enregistrer les raisonnements intermédiaires, suppositions, secrets, transcripts,
sorties volumineuses ou faits déjà présents.

## Référence : rappel incomplet

Vous pouvez ignorer cette table tant que `notes.recall_complete` vaut `true`.

Toujours inspecter `notes.recall_complete`. S'il vaut `false`, ne pas conclure qu'un souvenir absent
n'existe pas. `notes.warnings` contient des codes stables et bornés :

| Code | Sens | Action client |
| --- | --- | --- |
| `query_too_long` | La requête dépasse le plafond de caractères et n'a pas été cherchée. | Raccourcir puis retenter. |
| `query_too_many_terms` | La requête dépasse le plafond de termes et n'a pas été cherchée. | Garder un ensemble plus ciblé de sujets puis retenter. |
| `query_has_no_search_terms` | La normalisation ne produit aucun terme lexical cherchable. | Fournir au moins un mot ou un nombre. |
| `fts_query_timeout` | La deadline absolue du plan lexical a expiré ; le résultat public est vide pour éviter toute revalidation non bornée ou partielle. | Ne pas conclure à une absence. Retenter avec moins de termes lexicaux, plus spécifiques ; contacter l'opérateur si cela persiste. |
| `capsule_budget_overflow` | Des entrées entières ont été omises pour respecter le plafond du payload sérialisé. | Retenter avec un budget autorisé plus grand ou une requête plus ciblée ; ne pas conclure à une absence. |
| `unclassified_claim_omitted` | Un claim legacy de confiance n'a pas d'identité de proposition explicite et a été omis fail-closed. | Demander à l'opérateur de classifier l'entrée legacy avant de se fier à la complétude. |
| `conflicts_hidden_by_request` | Des versions conflictuelles correspondent mais `include_conflicts` vaut false. | Retenter avec `include_conflicts=true` et traiter chaque version symétriquement. |
| `conflict_family_overflow` | Une famille de conflit complète dépasse le plafond et a été omise. | Resserrer la requête ou demander à l'opérateur de revoir `fts_top_k` ; ne jamais choisir implicitement une version. |
| `project_state_overflow` | L'historique d'état projet du scope dépasse le plafond et a été omis. | Ne pas conclure à l'absence de prochaine action ; resserrer le scope ou utiliser un inventaire opérateur. |
| `hybrid_provider_unavailable` | Les embeddings ont échoué et seul le FTS lexical a été utilisé. | Considérer le rappel sémantique incomplet ; retenter plus tard ou continuer avec des limites lexicales explicites. |
| `hybrid_provider_invalid_vector` | Le provider renvoie un mauvais nombre de vecteurs ou un vecteur vide, non numérique, non fini, hors float32, surdimensionné ou de norme nulle. | Considérer le rappel sémantique indisponible et réparer ou remplacer le provider. |
| `hybrid_candidate_overflow` | Le scan vectoriel exact dépasse `hybrid_max_candidates` et seul le FTS a été utilisé. | Resserrer scope/kinds ou faire revoir le plafond hybride borné par l'opérateur. |
| `hybrid_vector_budget_exceeded` | Les dimensions ou octets vectoriels visibles dépassent le budget mémoire fixe ; seul le FTS a été utilisé. | Resserrer scope/kinds ou rester en mode FTS ; ne pas relever le budget de sécurité fixe. |
| `hybrid_vector_coverage_incomplete` | Au moins une entrée visible n'a aucun vecteur, ou une dimension compatible, pour le modèle configuré. | Réindexer les vecteurs ou considérer l'absence sémantique comme inconnue. |

### 3. À la fin

Quand l'état a changé de manière utile pour la prochaine session, appeler `remember` avec un
`project_state` concis : ce qui est terminé, l'état actuel, le blocage confirmé éventuel et la
prochaine action concrète. Ajouter un `episode` uniquement si l'événement lui-même aura une valeur
à court terme.

Exemple minimal :

```text
remember
statement = "Termine: X. Etat: Y. Blocage: aucun. Prochaine action: Z."
kind = "project_state"
scope = "project/nom-du-projet"
subject_keys = ["project:nom-du-projet"]
```

<a id="texte-dinstruction-pret-a-coller"></a>

## Texte d'instruction prêt à coller

```text
Engram session protocol

Engram is an MCP memory broker. It does not observe the conversation unless you call its tools.

At the start of a substantive task, call recall with a concise query naming the project, task,
and relevant subjects. Use the narrowest known scope. Read current and next_action before acting.
Treat conflicts and own_pending as unresolved, never as verified truth.

Scopes are one of global, user, project/<id>, or session/<id>, where <id> is a short slug.
An unrecognised scope is refused, so prefer project/<repository-name> when in doubt.

During the session, call remember only for durable, explicit information: a confirmed preference,
a decision and useful rationale, a verified fact, an important correction, or a meaningful project
state change. Choose one of preference, decision, fact, project_state, or episode. Use a small set
of stable subject_keys. Never store secrets, credentials, private transcripts, speculation,
intermediate reasoning, large tool output, or information already present.

Before ending a session whose state materially changed, call remember with one concise
project_state containing the completed work, current state, any confirmed blocker, and the next
concrete action. Store an episode only when the event itself has short-term future value.

The remember tool reports created, retry, corroborated, existing_trusted, or renewed. New and
renewed generations are unconfirmed quarantined candidates; existing_trusted is already trusted
content, while own_pending remains unresolved and must never justify an irreversible action.
If recall returns a conflict, surface every version in the exact kind/scope/claim_key family
symmetrically and ask for resolution when it matters. subject_keys are discovery hints, not
conflict identity. If notes.recall_complete is false, never infer absence from omitted results;
inspect notes.warnings and follow the documented retry or operator action for each code.
```

Le texte est volontairement en anglais pour être réutilisable tel quel par les trois clients. Il ne
donne aucune autorisation d'attester ou de consolider : ces actions restent humaines et séparées.

`engram setup client <claude|codex|gemini> --protocol` ajoute ce même texte au fichier
d'instructions du client, une seule fois et sans écraser ce qui s'y trouve déjà.

## Installation par client

- **Claude Code** : instructions utilisateur ou `CLAUDE.md` local non commité.
- **Codex** : instructions utilisateur ou `AGENTS.md` local selon la portée voulue.
- **Gemini CLI / Code Assist** : `GEMINI.md` utilisateur ou de projet.
- **Claude Desktop** : instructions de projet/personnalisées associées à l'usage du connecteur.

Voir [setup.md](setup.md) pour les fichiers de configuration MCP et les limites de transport.
