# Contrat de données

[Français](spec.md) | [English](../en/spec.md)

> **Document de référence :** utile pour implémenter ou auditer un client. Pour l'usage courant,
> lire le [guide utilisateur](user-guide.md).

Ce document décrit le contrat persistant d'Engram. Les champs de confiance et de provenance sont
des décisions du serveur, pas des affirmations libres du client.

## Kinds

| Kind | Usage | TTL par défaut |
| --- | --- | --- |
| `preference` | Préférence durable explicite | Sans expiration |
| `decision` | Décision prise et raison utile | Sans expiration |
| `project_state` | État courant et prochaine action | 30 jours |
| `fact` | Fait stable vérifié | Sans expiration |
| `episode` | Événement de session utile à court terme | 7 jours |

Les TTL sont configurables dans `[ttl_days]`. La valeur `0` désactive l'expiration d'une entrée
**fiable**. Un candidat non attesté est en plus borné par `candidate_max_days`, 90 par défaut :
un `kind` réglé à `0` n'accorde donc jamais à la supposition non relue d'un modèle la durée de vie
d'un fait vérifié par un humain. Le plafond ne fait que raccourcir, et attester un candidat le lève. L'expiration d'un candidat le
retire de la recall sans rien supprimer : `engram list --status expired` montre toujours l'énoncé,
et l'attester le ramène en fiable sous la forme d'une nouvelle entrée. Recall exclut
immédiatement les entrées à ou après `expires_at`, puis le démon passe périodiquement leur status
à `expired`. La validité métier est inclusive : une entrée ne peut être rappelée ou consolidée que
si la date UTC du store respecte `valid_from <= aujourd'hui <= valid_until` ; une borne
absente reste ouverte.

## Schéma d'une entrée

| Champ | Type | Règle |
| --- | --- | --- |
| `id` | string | ULID serveur canonique |
| `kind` | enum | Un des cinq kinds |
| `scope` | string | Espace logique normalisé, `user` par défaut |
| `statement` | string | Contenu borné par `max_statement_chars` |
| `subject_keys` | liste de strings | Clés de recherche/sujet bornées et normalisées |
| `status` | enum | `active`, `superseded`, `quarantined`, `expired` |
| `promotion_state` | enum | `candidate`, `approved`, `rejected`, `promoted` |
| `source_type` | enum | `human`, `tool_verified`, `model_inferred`, `session_summary` |
| `writer_model` | string ou null | Identité MCP du client écrivain |
| `confidence` | enum | `high`, `medium`, `low` |
| `observed_at` | datetime ou null | Moment observé fourni si connu |
| `recorded_at` | datetime | Horodatage serveur UTC |
| `valid_from` | date ou null | Début de validité métier |
| `valid_until` | date ou null | Fin de validité métier |
| `expires_at` | datetime ou null | Expiration calculée par kind |
| `canonical_key` | string | Identité SHA-256 du kind, scope et statement normalisés |
| `idempotency_key` | string | Empreinte SHA-256 de la clé canonique et de l'ULID |
| `claim_key` | string ou null | Famille sémantique normalisée des affirmations fiables |
| `supersedes` | liste d'identifiants | Versions remplacées |
| `evidence` | liste `{type, ref}` | Références opaques, jamais le payload source |
| `stale` | booléen | Promotion Datacron dont la fraîcheur a divergé |
| `datacron_ref` | string ou null | Chemin et section de la cible promue |
| `datacron_hash` | string ou null | Hash relu après écriture |
| `synced_at` | datetime ou null | Dernière synchronisation confirmée |

Le champ `type` d'une evidence est fermé : seuls `tool_result`, `datacron_note`, `human_message` et
`review` sont acceptés.

## Provenance et confiance

Pour un contenu nouveau ou renouvelé, `remember` crée une entrée `model_inferred`, `quarantined`,
`candidate`. Un retry exact du même writer retourne cette génération ; des métadonnées
matériellement différentes de ce writer sont conservées comme observation corroborante. Un contenu
canonique identique déjà actif et fiable est retourné sans créer de candidat. L'outcome explicite
est `created`, `retry`, `corroborated`, `existing_trusted` ou `renewed`. L'identité du writer vient
de l'initialisation MCP et non d'un argument. Une confiance `high` demandée à ce niveau est stockée
`medium` et l'événement de plafonnement est audité.

Chaque génération candidate possède au moins une ligne dans `entry_observations`. Les observations
supplémentaires conservent writer, confiance, dates, subject keys et preuves sans fusionner
silencieusement des writers différents dans une même affirmation de provenance.

Seul le chemin CLI local attesté accepte `human` ou `tool_verified`. Une entrée ne devient éligible
à la consolidation que si elle est `active`, `approved`, non stale, dans sa fenêtre de validité
métier et attestée par une de ces deux provenances. Apply contrôle la fenêtre avant une écriture
Datacron, puis le store la contrôle encore dans la transaction de promotion : une entrée devenue
invalide ne peut pas être marquée `promoted`.

## Cycle de vie

1. Un appel `remember` crée, renouvelle, retrouve ou corrobore un candidat `quarantined`, ou
   retourne un contenu fiable actif canoniquement identique.
2. Une attestation explicite produit une entrée `active` et `approved`. Le contenu canonique
   identique d'un candidat est promu sur place et conserve son identifiant.
3. Une nouvelle version peut rendre les anciennes `superseded` sans effacer l'historique.
4. Le TTL rend une entrée `expired` ; la purge physique est une opération distincte et auditée.
5. Une consolidation revue passe l'état à `promoted` seulement après création exactement relue ou
   liaison `redundant` exactement revérifiée.

Les conflits fiables actifs sont regroupés uniquement par le tuple exact
`(kind, scope, claim_key)`. Toutes les versions de cette famille sont retournées symétriquement
dans `conflicts` ; aucune n'est placée arbitrairement dans `current`. Les `subject_keys` améliorent
la recherche par sujet, mais ne définissent jamais l'identité sémantique d'un conflit. Les anciennes
entrées fiables sans `claim_key` restent lisibles dans l'inventaire explicite des non classées,
mais sont masquées de `current` jusqu'à leur classification par un opérateur.

## Propriété du processus

Le démon et chaque commande capable de modifier la base configurée prennent le même verrou OS
exclusif avant d'ouvrir le store. Une contention échoue immédiatement avec le diagnostic de l'owner.
Un fichier de coordination non verrouillé ne constitue pas une propriété : des métadonnées de PID
périmées ne peuvent donc pas bloquer la reprise. Le listing par statut utilise une base existante et
migrée en mode SQLite read-only ; il reste disponible pendant le fonctionnement du démon.

L'arrêt propre du démon passe par la sentinelle `<base>.stop` déposée à côté de la base, que
`engram stop` écrit et dont il attend l'effet en surveillant le verrou de propriété. Le démon efface
la demande qu'il trouve après avoir pris ce verrou, de sorte qu'une sentinelle oubliée ne bloque
jamais un démarrage ultérieur.

Le contrat d'erreur CLI réserve le code `2` à l'usage/configuration, `3` aux ressources locales
indisponibles, `4` aux dépendances externes indisponibles (transport Datacron ou endpoint
d'embeddings), `5` à la contention transitoire du store, `6` à un rapport apply contenant des
propositions failed ou stale et `130` à une interruption opérateur propagée à la CLI. Les erreurs
connues omettent les tracebacks, sauf si le flag global `--debug` ou `ENGRAM_DEBUG=1` est actif. La
disponibilité du port est vérifiée avant l'ouverture de SQLite ou du verrou de processus.

## Idempotence

`canonical_key` identifie le contenu normalisé exact. `idempotency_key` identifie une génération
stable en hachant cette clé canonique avec son ULID ; elle survit donc à l'attestation sur place.
La détection d'un retry utilise l'identité canonique, le writer et l'observation conservée : un
retry exact retourne `retry` et audite `idempotent_noop`, tandis qu'une nouvelle observation
retourne `corroborated`.

## Fraîcheur Datacron

Après promotion, Engram conserve `datacron_ref`, `datacron_hash` et `synced_at`. Le contrôle de
fraîcheur relit la note. Si son hash diffère, l'entrée devient `stale` et est masquée de `current`
jusqu'à revue. L'historique n'est pas supprimé et Datacron n'est pas réécrit par ce contrôle.

La recherche Datacron ne fournit pas de subject keys durables. Le gateway conserve donc ces clés
vides au lieu de recopier celles du candidat. La recherche combine la requête AND complète et des
variantes par terme. Le travail reste borné à trois requêtes complètes plus au plus huit variantes
unitaires, et `neighbor_limit` reste compris entre 1 et 64. Tout hit sans chemin, illisible, vide ou
sans sélecteur de section unique fait échouer le plan ; il ne peut jamais provoquer une
classification `new` suivie d'une création.
`get_note(full)` valide le `rel_path`, retire uniquement l'enveloppe sandbox Datacron canonique et
refuse tout contenu tronqué. Le `content_hash` serveur, calculé sur le fichier, reste intact pour la
fraîcheur et la vérification ; le contrat `freshness-contract-v1` est obligatoire.
Une proposition `redundant` cible toujours le voisin dont le statement normalisé correspond
exactement au candidat. Une proposition `update` montre la cible classée et son diff dans le
rapport, avec son niveau `H1` à `H6`, mais son action actuelle est `skip` : aucun patch de section
n'est autorisé sans ancrage d'identité durable indépendamment vérifié.

Le chemin d'une nouvelle note est déterministe, borne son slug ASCII à 64 caractères et contient
toujours l'ID candidat. Avant chaque plan, Engram relit ce chemin canonique. Une note dont le
contenu complet correspond exactement au
rendu attendu devient `redundant`, y compris après une réponse de création perdue ; seules les fins
de ligne et la présence du newline final sont normalisées. Toute autre note présente devient
`update/skip`. Aucune variante de chemin ne peut créer un doublon.
La planification persiste un snapshot canonique et immuable des propositions sous un `plan_id`
généré. L'artefact de revue ne
peut modifier que la décision de chaque proposition. Apply refuse toute autre divergence ou
décision encore `pending` sans consommer le plan. Une fois toutes les décisions approve/reject,
apply consomme le plan avant les écritures externes et interdit sa relecture. Une passe finale
réconcilie le hash de note complète de chaque promotion sur un chemin potentiellement écrit avant
qu'elle puisse être rappelée. Tout résultat apply `failed` ou `stale` produit le code de sortie 6
après écriture du rapport.
