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
| `id` | string | Identifiant serveur stable |
| `kind` | enum | Un des cinq kinds |
| `scope` | string | Espace logique normalise, `user` par defaut |
| `statement` | string | Contenu borne par `max_statement_chars` |
| `subject_keys` | liste de strings | Identites de sujet, bornees et normalisees |
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
| `idempotency_key` | string | Empreinte deterministe unique |
| `supersedes` | liste d'identifiants | Versions remplacees |
| `evidence` | liste `{type, ref}` | References opaques, jamais le payload source |
| `stale` | booleen | Promotion Datacron dont la fraicheur a diverge |
| `datacron_ref` | string ou null | Chemin et section de la cible promue |
| `datacron_hash` | string ou null | Hash relu apres ecriture |
| `synced_at` | datetime ou null | Derniere synchronisation confirmee |

## Provenance et confiance

`remember` cree toujours une entree `model_inferred`, `quarantined`, `candidate`. L'identite du
writer vient de l'initialisation MCP et non d'un argument. Une confiance `high` demandee a ce
niveau est stockee `medium` et l'evenement de plafonnement est audite.

Seul le chemin CLI local atteste accepte `human` ou `tool_verified`. Une entree ne devient eligible
a la consolidation que si elle est `active`, `approved`, non stale, dans sa fenetre de validite
metier et attestee par une de ces deux provenances. Apply controle la fenetre avant une ecriture
Datacron, puis le store la controle encore dans la transaction de promotion : une entree devenue
invalide ne peut pas etre marquee `promoted`.

## Cycle de vie

1. Un appel `remember` cree ou retrouve idempotemment un candidat `quarantined`.
2. Une attestation explicite produit une entree `active` et `approved`. Le contenu canonique
   identique d'un candidat est promu sur place et conserve son identifiant.
3. Une nouvelle version peut rendre les anciennes `superseded` sans effacer l'historique.
4. Le TTL rend une entree `expired`; la purge physique est une operation distincte et auditee.
5. Une consolidation revue passe l'etat a `promoted` seulement apres ecriture CAS et relecture.

Les conflits actifs qui partagent des `subject_keys` sont symetriques : aucune version n'est
placee arbitrairement dans `current`. Ils apparaissent dans `conflicts` uniquement si le client le
demande.

## Propriete du processus

Le daemon et chaque commande capable de modifier la base configuree prennent le meme verrou OS
exclusif avant d'ouvrir le store. Une contention echoue immediatement avec le diagnostic de l'owner.
Un fichier de coordination non verrouille ne constitue pas une propriete : des metadonnees de PID
perimees ne peuvent donc pas bloquer la reprise. Le listing par statut utilise une base existante et
migree en mode SQLite read-only ; il reste disponible pendant le fonctionnement du daemon.

Le contrat d'erreur CLI reserve le code `2` a l'usage/configuration, `3` aux ressources locales
indisponibles, `4` aux pannes de transport Datacron, `5` a la contention transitoire du store et
`130` a une interruption operateur propagee a la CLI. Les erreurs connues omettent les tracebacks,
sauf si le flag global `--debug` ou `ENGRAM_DEBUG=1` est actif. La disponibilite du port est
verifiee avant l'ouverture de SQLite ou du verrou de processus.

## Idempotence

La cle est calculee sur le contenu canonique pertinent. Un retry identique retourne la meme entree
avec `idempotent=true` et cree un evenement `idempotent_noop`; il ne duplique pas la memoire.

## Fraicheur Datacron

Apres promotion, Engram conserve `datacron_ref`, `datacron_hash` et `synced_at`. Le controle de
fraicheur relit la note. Si son hash differe, l'entree devient `stale` et est masquee de `current`
jusqu'a revue. L'historique n'est pas supprime et Datacron n'est pas reecrit par ce controle.

La consolidation conserve le rang de recherche deterministe renvoye par Datacron pour choisir une
cible de patch. Une proposition `redundant` cible toujours le voisin dont le statement normalise
correspond exactement au candidat, meme si un resultat plus large arrive avant. Apply regenere les
voisins courants avant ecriture. Une cible de patch corrigee par l'humain n'est acceptee que si son
chemin, son heading, son niveau et son hash attendu identifient exactement un voisin du plan revu et
de la recherche courante. Les chemins et headings NEW ne sont pas editables. Le gateway transmet le
niveau a Datacron et relit la section exacte avant de marquer l'entree `promoted`. Une passe finale
reconcilie le hash de note complete de chaque promotion sur un chemin potentiellement ecrit avant
qu'elle puisse etre rappelee.
