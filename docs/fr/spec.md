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
a `expired`.

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

Seul le chemin interne atteste accepte `human` ou `tool_verified`. Une entree ne devient eligible
a la consolidation que si elle est `active`, `approved`, non stale et attestee par une de ces deux
provenances.

## Cycle de vie

1. Un appel `remember` cree ou retrouve idempotemment un candidat `quarantined`.
2. Une attestation explicite produit une entree `active` et `approved`.
3. Une nouvelle version peut rendre les anciennes `superseded` sans effacer l'historique.
4. Le TTL rend une entree `expired`; la purge physique est une operation distincte et auditee.
5. Une consolidation revue passe l'etat a `promoted` seulement apres ecriture CAS et relecture.

Les conflits actifs qui partagent des `subject_keys` sont symetriques : aucune version n'est
placee arbitrairement dans `current`. Ils apparaissent dans `conflicts` uniquement si le client le
demande.

## Idempotence

La cle est calculee sur le contenu canonique pertinent. Un retry identique retourne la meme entree
avec `idempotent=true` et cree un evenement `idempotent_noop`; il ne duplique pas la memoire.

## Fraicheur Datacron

Apres promotion, Engram conserve `datacron_ref`, `datacron_hash` et `synced_at`. Le controle de
fraicheur relit la note. Si son hash differe, l'entree devient `stale` et est masquee de `current`
jusqu'a revue. L'historique n'est pas supprime et Datacron n'est pas reecrit par ce controle.
