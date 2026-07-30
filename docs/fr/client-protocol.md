# Protocole client Engram

[Francais](client-protocol.md) | [English](../en/client-protocol.md)

## Pourquoi ce protocole existe

MCP transmet des appels d'outils ; Engram ne voit pas passivement la conversation, les fichiers
ouverts ou la fin d'une session. Chaque client doit donc decider quand rappeler et quand proposer
un souvenir. Ce contrat rend ce comportement identique dans Claude, Codex et Gemini.

## Les trois moments

### 1. Au demarrage

Appeler `recall` avant le travail substantiel avec une requete courte qui nomme le projet, la tache
et les sujets utiles. Utiliser le scope le plus precis connu. Lire `current` et `next_action` avant
d'agir ; traiter `conflicts` et `own_pending` comme non resolus.

### 2. Pendant la session

Appeler `remember` seulement apres une information durable et explicite : decision, correction,
preference confirmee, fait verifie ou changement d'etat. Choisir le kind et des `subject_keys`
stables. Ne pas enregistrer les raisonnements intermediaires, suppositions, secrets, transcripts,
sorties volumineuses ou faits deja presents.

## Rappel incomplet

Toujours inspecter `notes.recall_complete`. S'il vaut `false`, ne pas conclure qu'un souvenir absent
n'existe pas. `notes.warnings` contient des codes stables et bornes :

| Code | Sens | Action client |
| --- | --- | --- |
| `query_too_long` | La requete depasse le plafond de caracteres et n'a pas ete cherchee. | Raccourcir puis retenter. |
| `query_too_many_terms` | La requete depasse le plafond de termes et n'a pas ete cherchee. | Garder un ensemble plus cible de sujets puis retenter. |
| `query_has_no_search_terms` | La normalisation ne produit aucun terme lexical cherchable. | Fournir au moins un mot ou un nombre. |
| `fts_query_timeout` | La deadline absolue du plan lexical a expire ; le resultat public est vide pour eviter toute revalidation non bornee ou partielle. | Ne pas conclure a une absence. Retenter avec moins de termes lexicaux, plus specifiques ; contacter l'operateur si cela persiste. |
| `capsule_budget_overflow` | Des entrees entieres ont ete omises pour respecter le plafond du payload serialise. | Retenter avec un budget autorise plus grand ou une requete plus ciblee ; ne pas conclure a une absence. |
| `unclassified_claim_omitted` | Un claim legacy de confiance n'a pas d'identite de proposition explicite et a ete omis fail-closed. | Demander a l'operateur de classifier l'entree legacy avant de se fier a la completude. |
| `conflicts_hidden_by_request` | Des versions conflictuelles correspondent mais `include_conflicts` vaut false. | Retenter avec `include_conflicts=true` et traiter chaque version symetriquement. |
| `conflict_family_overflow` | Une famille de conflit complete depasse le plafond et a ete omise. | Resserrer la requete ou demander a l'operateur de revoir `fts_top_k` ; ne jamais choisir implicitement une version. |
| `project_state_overflow` | L'historique d'etat projet du scope depasse le plafond et a ete omis. | Ne pas conclure a l'absence de prochaine action ; resserrer le scope ou utiliser un inventaire operateur. |
| `hybrid_provider_unavailable` | Les embeddings ont echoue et seul le FTS lexical a ete utilise. | Considerer le rappel semantique incomplet ; retenter plus tard ou continuer avec des limites lexicales explicites. |
| `hybrid_provider_invalid_vector` | Le provider renvoie un mauvais nombre de vecteurs ou un vecteur vide, non numerique, non fini, hors float32, surdimensionne ou de norme nulle. | Considerer le rappel semantique indisponible et reparer ou remplacer le provider. |
| `hybrid_candidate_overflow` | Le scan vectoriel exact depasse `hybrid_max_candidates` et seul le FTS a ete utilise. | Resserrer scope/kinds ou faire revoir le plafond hybride borne par l'operateur. |
| `hybrid_vector_budget_exceeded` | Les dimensions ou octets vectoriels visibles depassent le budget memoire fixe ; seul le FTS a ete utilise. | Resserrer scope/kinds ou rester en mode FTS ; ne pas relever le budget de securite fixe. |
| `hybrid_vector_coverage_incomplete` | Au moins une entree visible n'a aucun vecteur, ou une dimension compatible, pour le modele configure. | Reindexer les vecteurs ou considerer l'absence semantique comme inconnue. |

### 3. A la fin

Quand l'etat a change de maniere utile pour la prochaine session, appeler `remember` avec un
`project_state` concis : ce qui est termine, l'etat actuel, le blocage confirme eventuel et la
prochaine action concrete. Ajouter un `episode` uniquement si l'evenement lui-meme aura une valeur
a court terme.

## Texte d'instruction pret a coller

```text
Engram session protocol

Engram is an MCP memory broker. It does not observe the conversation unless you call its tools.

At the start of a substantive task, call recall with a concise query naming the project, task,
and relevant subjects. Use the narrowest known scope. Read current and next_action before acting.
Treat conflicts and own_pending as unresolved, never as verified truth.

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

Le texte est volontairement en anglais pour etre reutilisable tel quel par les trois clients. Il ne
donne aucune autorisation d'attester ou de consolider : ces actions restent humaines et separees.

## Installation par client

- **Claude Code** : instructions utilisateur ou `CLAUDE.md` local non commite.
- **Codex** : instructions utilisateur ou `AGENTS.md` local selon la portee voulue.
- **Gemini CLI / Code Assist** : `GEMINI.md` utilisateur ou de projet.
- **Claude Desktop** : instructions de projet/personnalisees associees a l'usage du connecteur.

Voir [setup.md](setup.md) pour les fichiers de configuration MCP et les limites de transport.
