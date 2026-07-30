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
conflict identity.
```

Le texte est volontairement en anglais pour etre reutilisable tel quel par les trois clients. Il ne
donne aucune autorisation d'attester ou de consolider : ces actions restent humaines et separees.

## Installation par client

- **Claude Code** : instructions utilisateur ou `CLAUDE.md` local non commite.
- **Codex** : instructions utilisateur ou `AGENTS.md` local selon la portee voulue.
- **Gemini CLI / Code Assist** : `GEMINI.md` utilisateur ou de projet.
- **Claude Desktop** : instructions de projet/personnalisees associees a l'usage du connecteur.

Voir [setup.md](setup.md) pour les fichiers de configuration MCP et les limites de transport.
