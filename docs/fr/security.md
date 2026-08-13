# Sécurité et vie privée

[Français](security.md) | [English](../en/security.md)

> **Document de référence :** pour démarrer, retenez seulement que le serveur reste local, que
> `own_pending` n'est pas fiable et qu'une revue humaine précède toute promotion. Voir le
> [guide utilisateur](user-guide.md).

## Frontière de confiance

Le client MCP est non fiable pour la provenance. `remember` n'accepte ni `source_type`, ni
`writer_model`, ni un statut privilégié. Le serveur dérive l'identité de la session MCP, impose
`model_inferred`, plafonne la confiance à `medium` et place l'entrée en quarantaine.

Les provenances `human` et `tool_verified` ne sont accessibles que par la commande locale
`engram attest`. L'acteur d'audit vient de la configuration ou d'un flag opérateur explicite. Une
référence d'evidence reste opaque ; elle ne transforme pas à elle seule un candidat en fait vérifié.
Seuls quatre types d'evidence sont acceptés : `tool_result`, `datacron_note`, `human_message` et
`review`.

## Quarantaine anti-poisoning

Un candidat quarantined :

- n'entre pas dans `current`, `next_action` ou `relevant` ;
- n'est visible que dans `own_pending` du même couple nom/version de client MCP ;
- est étiqueté `unconfirmed candidate` ;
- ne peut pas être consolidé vers Datacron.

Cette politique limite la propagation entre clients d'une instruction injectée ou d'une inférence
fausse. La revue et l'attestation restent explicites. Le couple nom/version MCP est déclaré par le
client lui-même. Il sert seulement d'espace de noms pratique pour les observations en attente :
ce n'est ni une authentification, ni une autorisation, ni une frontière de confidentialité. Tout
processus capable de joindre l'endpoint peut déclarer le même couple.

## Writer unique et SQLite

Exécuter une seule instance Engram par fichier de base. Le verrou applicatif sérialise les
mutations, SQLite utilise WAL et `BEGIN IMMEDIATE`, et un timeout court demande au client de retry.
Le guard refuse SQLite < 3.51.3. Ne pas placer le fichier sur un partage réseau dont les verrous
SQLite ne sont pas garantis.

Le démon conserve un verrou OS exclusif dérivé du chemin de la base. Les writers offline
(`migrate`, `classify`, `attest`, `supersede`, `reindex` et `consolidate`) gardent ce même verrou
pendant toute leur opération et échouent avant d'ouvrir SQLite si un autre owner existe. Le fichier
de coordination
persiste, mais ses métadonnées ne donnent jamais la propriété : le verrou de plage Windows ou
`flock` sous POSIX fait autorité et est libéré automatiquement à la mort du processus. `list`
utilise le mode SQLite read-only et ne prend aucun verrou writer. Arrêter le démon avec
`engram stop` avant une écriture offline, puis le redémarrer ensuite.

La demande d'arrêt est un fichier sentinelle `<base>.stop` déposé à côté de la base, et non un
message reçu sur le réseau : le droit d'arrêter Engram est exactement le droit d'écrire dans le
répertoire de sa base, qui est déjà le droit de la corrompre. Aucun port n'expose cette capacité.

## Réseau

L'adresse d'écoute est une frontière de sécurité. Engram accepte uniquement des literals IP
loopback non ambigus comme `127.0.0.1` ou `::1` ; les hostnames, wildcards, adresses LAN et adresses
publiques échouent à la configuration. Engram n'implémente ni compte, ni jeton, ni TLS. Pour un
client distant, placer un proxy HTTPS authentifié devant l'endpoint loopback, restreindre les
origines et le réseau, et surveiller les accès.

## Confinement Datacron

Les lectures et écritures passent par Datacron MCP. `vault_root`, `read_paths` et `write_paths`
limitent les cibles ; `new_note_directory` doit rester sous `_memory/`. Une liste d'écriture vide
ferme le chemin : une valeur `DATACRON_WRITE_PATHS` du processus parent est explicitement effacée
au lieu d'être héritée. Une mutation exige le hash CAS attendu, puis une relecture avant de
marquer la promotion.

`contradiction_scan` est un signal read-only : il ne donne pas d'autorisation d'écrire.

## Données et appels externes

- Pas de télémétrie.
- Pas d'appel LLM cloud implémenté.
- FTS5 reste entièrement local.
- Le mode hybride envoie les statements à l'endpoint d'embeddings configuré. Le garder sur
  loopback pour une garantie locale ; une URL distante constitue une exportation de données choisie
  par l'opérateur.
- Les logs ne contiennent pas volontairement le contenu des souvenirs ; protéger néanmoins le
  dossier `logs/` et la base comme des données utilisateur.

## Capsule bornée

`token_budget` est contraint par un minimum et un maximum serveur, publiés comme `minimum` et
`maximum` dans le schéma de l'outil `recall` afin qu'un client voie la borne avant d'appeler.
Engram l'interprète de façon
conservatrice comme le nombre maximal d'octets UTF-8 du résultat d'outil complet sérialisé : un
octet par token subword byte-level possible. C'est une limite absolue de taille du payload, pas une
promesse de tokenisation exacte pour chaque modèle. Le builder mesure ensemble fallback et contenu
structuré, remplace un scope surdimensionné par une empreinte bornée, retire les sections de plus
faible priorité avant la limite et expose les omissions fail-closed via
`notes.recall_complete` et des codes d'avertissement bornés. Cette borne limite l'exfiltration
accidentelle et l'envahissement du contexte, sans remplacer le contrôle d'accès.

## Sauvegarde et incident

Sauvegarder ensemble le fichier principal et, si Engram tourne, utiliser une sauvegarde SQLite
cohérente plutôt qu'une simple copie des fichiers WAL/SHM. En cas de soupçon de corruption :
arrêter le writer avec `engram stop`, conserver les fichiers, travailler sur une copie et ne pas
lancer de purge.
