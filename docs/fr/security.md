# Securite et vie privee

[Francais](security.md) | [English](../en/security.md)

## Frontiere de confiance

Le client MCP est non fiable pour la provenance. `remember` n'accepte ni `source_type`, ni
`writer_model`, ni un statut privilegie. Le serveur derive l'identite de la session MCP, impose
`model_inferred`, plafonne la confiance a `medium` et place l'entree en quarantaine.

Les provenances `human` et `tool_verified` ne sont accessibles que par la commande locale
`engram attest`. L'acteur d'audit vient de la configuration ou d'un flag operateur explicite. Une
reference d'evidence reste opaque ; elle ne transforme pas seule un candidat en fait verifie.

## Quarantaine anti-poisoning

Un candidat quarantined :

- n'entre pas dans `current`, `next_action` ou `relevant` ;
- n'est visible que dans `own_pending` du meme couple nom/version de client MCP ;
- est etiquete `unconfirmed candidate` ;
- ne peut pas etre consolide vers Datacron.

Cette politique limite la propagation entre clients d'une instruction injectee ou d'une inference
fausse. La revue et l'attestation restent explicites.

## Writer unique et SQLite

Executer une seule instance Engram par fichier de base. Le verrou applicatif serialise les
mutations, SQLite utilise WAL et `BEGIN IMMEDIATE`, et un timeout court demande au client de retry.
Le guard refuse SQLite < 3.51.3. Ne pas placer le fichier sur un partage reseau dont les verrous
SQLite ne sont pas garantis.

Le daemon conserve un verrou OS exclusif derive du chemin de la base. Les writers offline
(`migrate`, `classify`, `attest`, `supersede`, `reindex` et `consolidate`) gardent ce meme verrou
pendant toute leur operation et echouent avant d'ouvrir SQLite si un autre owner existe. Le fichier
de coordination
persiste, mais ses metadonnees ne donnent jamais la propriete : le verrou de plage Windows ou
`flock` sous POSIX fait autorite et est libere automatiquement a la mort du processus. `list`
utilise le mode SQLite read-only et ne prend aucun verrou writer. Arreter le daemon avant une
ecriture offline, puis le redemarrer ensuite.

## Reseau

Le defaut `127.0.0.1` est une frontiere de securite. Engram n'implemente ni compte, ni jeton, ni
TLS. Ne pas utiliser `0.0.0.0` directement. Pour un client distant, placer un proxy HTTPS
authentifie devant Engram, restreindre les origines et le reseau, et surveiller les acces.

## Confinement Datacron

Les lectures et ecritures passent par Datacron MCP. `vault_root`, `read_paths` et `write_paths`
limitent les cibles ; `new_note_directory` doit rester sous `_memory/`. Une liste d'ecriture vide
ferme le chemin : une valeur `DATACRON_WRITE_PATHS` du processus parent est explicitement effacee
au lieu d'etre heritee. Une mutation exige le hash CAS attendu, puis une relecture avant de
marquer la promotion.

`contradiction_scan` est un signal read-only : il ne donne pas d'autorisation d'ecrire.

## Donnees et appels externes

- Pas de telemetrie.
- Pas d'appel LLM cloud implemente.
- FTS5 reste entierement local.
- Le mode hybride envoie les statements a l'endpoint d'embeddings configure. Le garder sur
  loopback pour une garantie locale ; une URL distante constitue une exportation de donnees choisie
  par l'operateur.
- Les logs ne contiennent pas volontairement le contenu des souvenirs ; proteger neanmoins le
  dossier `logs/` et la base comme des donnees utilisateur.

## Capsule bornee

`token_budget` est contraint par un minimum et un maximum serveur. Le builder retire les sections
de plus faible priorite avant de depasser le budget et signale les omissions. Cette borne limite
l'exfiltration accidentelle et l'envahissement du contexte, sans remplacer le controle d'acces.

## Sauvegarde et incident

Sauvegarder ensemble le fichier principal et, si Engram tourne, utiliser une sauvegarde SQLite
coherente plutot qu'une simple copie des fichiers WAL/SHM. En cas de soupcon de corruption : arreter
le writer, conserver les fichiers, travailler sur une copie et ne pas lancer de purge.
