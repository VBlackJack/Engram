# Contrat de donnees

[Francais](spec.md) | [English](../en/spec.md)

Ce document decrit le contrat persistant d'Engram. Les champs de confiance et de provenance sont
des decisions du serveur, pas des affirmations libres du client.

## Kinds

| Kind | Usage | TTL par defaut |
| --- | --- | --- |
| `preference` | Preference durable explicite | Sans expiration |
| `decision` | Decision prise et raison utile | Sans expiration |
| `project_state` | Etat courant et prochaine action | 30 jours |
| `fact` | Fait stable verifie | Sans expiration |
| `episode` | Evenement de session utile a court terme | 7 jours |

Les TTL sont configurables dans `[ttl_days]`. La valeur `0` desactive l'expiration. Recall exclut
immediatement les entrees a ou apres `expires_at`, puis le daemon passe periodiquement leur status
a `expired`. La validite metier est inclusive : une entree ne peut etre rappelee ou consolidee que
si la date UTC du store respecte `valid_from <= aujourd'hui <= valid_until` ; une borne
absente reste ouverte.

## Schema d'une entree

| Champ | Type | Regle |
| --- | --- | --- |
| `id` | string | ULID serveur canonique |
| `kind` | enum | Un des cinq kinds |
| `scope` | string | Espace logique normalise, `user` par defaut |
| `statement` | string | Contenu borne par `max_statement_chars` |
| `subject_keys` | liste de strings | Cles de recherche/sujet bornees et normalisees |
| `status` | enum | `active`, `superseded`, `quarantined`, `expired` |
| `promotion_state` | enum | `candidate`, `approved`, `rejected`, `promoted` |
| `source_type` | enum | `human`, `tool_verified`, `model_inferred`, `session_summary` |
| `writer_model` | string ou null | Identite MCP du client ecrivain |
| `confidence` | enum | `high`, `medium`, `low` |
| `observed_at` | datetime ou null | Moment observe fourni si connu |
| `recorded_at` | datetime | Horodatage serveur UTC |
| `valid_from` | date ou null | Debut de validite metier |
| `valid_until` | date ou null | Fin de validite metier |
| `expires_at` | datetime ou null | Expiration calculee par kind |
| `canonical_key` | string | Identite SHA-256 du kind, scope et statement normalises |
| `idempotency_key` | string | Empreinte SHA-256 de la cle canonique et de l'ULID |
| `claim_key` | string ou null | Famille semantique normalisee des affirmations fiables |
| `supersedes` | liste d'identifiants | Versions remplacees |
| `evidence` | liste `{type, ref}` | References opaques, jamais le payload source |
| `stale` | booleen | Promotion Datacron dont la fraicheur a diverge |
| `datacron_ref` | string ou null | Chemin et section de la cible promue |
| `datacron_hash` | string ou null | Hash relu apres ecriture |
| `synced_at` | datetime ou null | Derniere synchronisation confirmee |

## Provenance et confiance

Pour un contenu nouveau ou renouvele, `remember` cree une entree `model_inferred`, `quarantined`,
`candidate`. Un retry exact du meme writer retourne cette generation ; des metadonnees
materiellement differentes de ce writer sont conservees comme observation corroborante. Un contenu
canonique identique deja actif et fiable est retourne sans creer de candidat. L'outcome explicite
est `created`, `retry`, `corroborated`, `existing_trusted` ou `renewed`. L'identite du writer vient
de l'initialisation MCP et non d'un argument. Une confiance `high` demandee a ce niveau est stockee
`medium` et l'evenement de plafonnement est audite.

Chaque generation candidate possede au moins une ligne dans `entry_observations`. Les observations
supplementaires conservent writer, confiance, dates, subject keys et preuves sans fusionner
silencieusement des writers differents dans une meme affirmation de provenance.

Seul le chemin CLI local atteste accepte `human` ou `tool_verified`. Une entree ne devient eligible
a la consolidation que si elle est `active`, `approved`, non stale, dans sa fenetre de validite
metier et attestee par une de ces deux provenances. Apply controle la fenetre avant une ecriture
Datacron, puis le store la controle encore dans la transaction de promotion : une entree devenue
invalide ne peut pas etre marquee `promoted`.

## Cycle de vie

1. Un appel `remember` cree, renouvelle, retrouve ou corrobore un candidat `quarantined`, ou
   retourne un contenu fiable actif canoniquement identique.
2. Une attestation explicite produit une entree `active` et `approved`. Le contenu canonique
   identique d'un candidat est promu sur place et conserve son identifiant.
3. Une nouvelle version peut rendre les anciennes `superseded` sans effacer l'historique.
4. Le TTL rend une entree `expired`; la purge physique est une operation distincte et auditee.
5. Une consolidation revue passe l'etat a `promoted` seulement apres creation exactement relue ou
   liaison `redundant` exactement reverifiee.

Les conflits fiables actifs sont regroupes uniquement par le tuple exact
`(kind, scope, claim_key)`. Toutes les versions de cette famille sont retournees symetriquement
dans `conflicts` ; aucune n'est placee arbitrairement dans `current`. Les `subject_keys` ameliorent
la recherche par sujet, mais ne definissent jamais l'identite semantique d'un conflit. Les anciennes
entrees fiables sans `claim_key` restent lisibles dans l'inventaire explicite des non classees,
mais sont masquees de `current` jusqu'a leur classification par un operateur.

## Propriete du processus

Le daemon et chaque commande capable de modifier la base configuree prennent le meme verrou OS
exclusif avant d'ouvrir le store. Une contention echoue immediatement avec le diagnostic de l'owner.
Un fichier de coordination non verrouille ne constitue pas une propriete : des metadonnees de PID
perimees ne peuvent donc pas bloquer la reprise. Le listing par statut utilise une base existante et
migree en mode SQLite read-only ; il reste disponible pendant le fonctionnement du daemon.

Le contrat d'erreur CLI reserve le code `2` a l'usage/configuration, `3` aux ressources locales
indisponibles, `4` aux pannes de transport Datacron, `5` a la contention transitoire du store, `6` a
un rapport apply contenant des propositions failed ou stale et `130` a une interruption operateur
propagee a la CLI. Les erreurs connues omettent les tracebacks, sauf si le flag global `--debug` ou
`ENGRAM_DEBUG=1` est actif. La disponibilite du port est verifiee avant l'ouverture de SQLite ou du
verrou de processus.

## Idempotence

`canonical_key` identifie le contenu normalise exact. `idempotency_key` identifie une generation
stable en hachant cette cle canonique avec son ULID ; elle survit donc a l'attestation sur place.
La detection d'un retry utilise l'identite canonique, le writer et l'observation conservee : un
retry exact retourne `retry` et audite `idempotent_noop`, tandis qu'une nouvelle observation
retourne `corroborated`.

## Fraicheur Datacron

Apres promotion, Engram conserve `datacron_ref`, `datacron_hash` et `synced_at`. Le controle de
fraicheur relit la note. Si son hash differe, l'entree devient `stale` et est masquee de `current`
jusqu'a revue. L'historique n'est pas supprime et Datacron n'est pas reecrit par ce controle.

La recherche Datacron ne fournit pas de subject keys durables. Le gateway conserve donc ces cles
vides au lieu de recopier celles du candidat. La recherche combine la requete AND complete et des
variantes par terme. Le travail reste borne a trois requetes completes plus au plus huit variantes
unitaires, et `neighbor_limit` reste compris entre 1 et 64. Tout hit sans chemin, illisible, vide ou
sans selecteur de section unique fait echouer le plan ; il ne peut jamais provoquer une
classification `new` suivie d'une creation.
`get_note(full)` valide le `rel_path`, retire uniquement l'enveloppe sandbox Datacron canonique et
refuse tout contenu tronque. Le `content_hash` serveur, calcule sur le fichier, reste intact pour la
fraicheur et la verification ; le contrat `freshness-contract-v1` est obligatoire.
Une proposition `redundant` cible toujours le voisin dont le statement normalise correspond
exactement au candidat. Une proposition `update` montre la cible classee et son diff dans le
rapport, avec son niveau `H1` a `H6`, mais son action actuelle est `skip` : aucun patch de section
n'est autorise sans ancrage d'identite durable independamment verifie.

Le chemin d'une nouvelle note est deterministe, borne son slug ASCII a 64 caracteres et contient
toujours l'ID candidat. Avant chaque plan, Engram relit ce chemin canonique. Une note dont le
contenu complet correspond exactement au
rendu attendu devient `redundant`, y compris apres une reponse de creation perdue ; seules les fins
de ligne et la presence du newline final sont normalisees. Toute autre note presente devient
`update/skip`. Aucune variante de chemin ne peut creer un doublon.
La planification persiste un snapshot canonique et immuable des propositions sous un `plan_id`
genere. L'artefact de revue ne
peut modifier que la decision de chaque proposition. Apply refuse toute autre divergence ou
decision encore `pending` sans consommer le plan. Une fois toutes les decisions approve/reject,
apply consomme le plan avant les ecritures externes et interdit sa relecture. Une passe finale
reconcilie le hash de note complete de chaque promotion sur un chemin potentiellement ecrit avant
qu'elle puisse etre rappelee. Tout resultat apply `failed` ou `stale` produit le code de sortie 6
apres ecriture du rapport.
