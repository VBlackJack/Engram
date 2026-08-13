# Architecture

[Français](architecture.md) | [English](../en/architecture.md)

> **Document de référence :** inutile pour démarrer. Utilisez le
> [guide rapide](quick-start.md) ou le
> [schéma Engram-Datacron-Cortex](datacron-cortex.md) pour un parcours court.

En cinq points : Engram est un serveur HTTP local stateful, SQLite est canonique, un seul processus
écrit, les index de recherche sont dérivés, et toute consolidation Datacron reste revue. Cortex
n'est pas un composant interne d'Engram.

## Vue d'ensemble

```text
Claude Code / Codex / Gemini
             |
             | MCP Streamable HTTP
             v
     Engram (writer unique)
       |        |        |
     SQLite   Retrieval  Capsule
       |                   |
       +---- plan revu ----+----> Datacron MCP ----> vault Markdown
```

Engram est un processus stateful local. Plusieurs clients MCP peuvent l'utiliser, mais une seule
instance Engram doit écrire la base. Le serveur sérialise les mutations et renvoie `server busy,
retry` si le verrou ne peut pas être acquis dans le délai configuré.

Un fichier de coordination placé à côté de la base porte un verrou OS exclusif. Le démon le garde
pendant sa durée de vie ; chaque writer offline le garde pendant toute sa commande. Windows utilise
un octet verrouillé via le CRT, tandis que POSIX utilise `flock`. Le PID et la commande ne sont que
des métadonnées de diagnostic : un fichier laissé par un processus mort est repris dès que le verrou
kernel a disparu. Le fichier n'est pas unlink à la libération afin d'éviter les races de remplacement
d'inode. La classe publique `EngramStore` garde le même lease pendant sa durée de vie ; plusieurs
connexions de ce seul processus le partagent, tandis qu'un autre processus writer est refusé. Les
lectures pures de `list` utilisent SQLite `mode=ro` sans ce verrou writer.

Le même chemin de base porte aussi la sentinelle d'arrêt `<base>.stop`, que `engram stop` dépose et
que le démon efface après avoir pris le verrou. C'est le seul canal d'arrêt d'un processus lancé
sans console, et il ne passe par aucun port.

## Stockage SQLite

SQLite est ouvert en WAL, avec foreign keys, busy timeout et migrations transactionnelles. Le
plancher 3.51.3 évite le bug WAL-reset. La table `entries` est canonique ; les tables FTS et
vectorielles sont dérivées et reconstructibles par `engram reindex`. Au démarrage, Engram compare
l'index FTS external-content aux lignes canoniques et le reconstruit s'il manque ou diverge.
`consolidation_plans` ancre les snapshots immuables des plans et leur état d'usage unique hors de
l'artefact de revue éditable. Chaque connexion charge d'abord `sqlite_schema` sous un plafond
temporaire de 256 Kio, puis applique un plafond permanent de 8 Mio aux valeurs et lignes SQLite.
Un snapshot de consolidation est refusé avant mutation au-delà de 4 Mio UTF-8.
Le preflight d'upgrade garde le lease writer, lit un snapshot source unique et prouve la migration
complète sur une copie disque jetable. Les définitions des tables canoniques sont contrôlées
exactement ; un objet dérivé peut manquer ou être une table reconstruisible, mais un index, trigger
ou view homonyme est refusé.

`audit_log` est append-only. Il conserve l'acteur, l'action, l'identifiant d'entrée et une empreinte
de détail, jamais le statement ni un payload de conversation.

Le log configuré tourne à 10 Mio avec cinq backups. Chaque processus Engram ferme le fichier entre
deux records et sérialise écriture/rotation par un verrou OS séparé, y compris sous Windows où un
handle ouvert ferait échouer la rotation fondée sur rename.

## Serveur MCP HTTP

FastMCP expose un endpoint Streamable HTTP (`127.0.0.1:8377/mcp` par défaut) et deux outils aux
schémas stricts. `remember` passe par la file d'écriture. `recall` effectue recherche et assemblage
dans un worker, sans mutation de confiance.

Le MCP ne reçoit que les appels d'outils. Il n'observe pas la conversation du client, raison pour
laquelle le [protocole client](client-protocol.md) fait partie du produit.

## Retrieval

Le mode `fts` dérive des termes sans opérateurs depuis une entrée NFKC bornée. Il classe d'abord la
phrase exacte et la conjonction de tous les termes, puis remplit chaque place restante du top-K
avec les rankings disjonctif et préfixe fusionnés équitablement. Les hits stricts gardent leur
priorité sans masquer les correspondances morphologiques. Chaque étage applique les filtres de
visibilité et un top-K dur dans SQL, avec BM25 puis récence et identifiant pour départager.
Une deadline monotone absolue `fts_query_timeout_ms` couvre l'attente du verrou et tous les étages
SQLite progressifs. Le progress handler SQLite interrompt un scan hors budget ; seuls les étages
terminés existent dans le ranking interne, mais le résultat public est vide et marqué incomplet
pour éviter une revalidation partielle après expiration. Le mode `hybrid`, derrière
configuration, combine FTS et embeddings par reciprocal rank fusion (`rrf_k`).
Il calcule un top-K sémantique exact uniquement si tous les vecteurs visibles tiennent sous
`hybrid_max_candidates` et les budgets fixes de dimensions/octets. Le scan ne renvoie que les IDs et
vecteurs ; au plus les payloads du top-K fusionné sont matérialisés puis revalidés. Un dépassement,
un résultat provider mal formé ou une indisponibilité dégrade le rappel vers FTS et marque la
capsule incomplète. Les rebuilds vectoriels utilisent des pages bornées et un stage temporaire,
préservent l'index live jusqu'au swap atomique et comparent `data_version` SQLite avant ce swap pour
refuser un commit intervenu entre-temps.

## Capsule recall

La politique D6/D7 sépare le ranking de la confiance. La capsule est remplie dans cet ordre :

1. `current` : préférences, décisions et faits actifs et dignes de confiance ;
2. `next_action` : états projet courants ;
3. `relevant` : épisodes pertinents ;
4. `conflicts` : versions non résolues, symétriques et sur demande ;
5. `own_pending` : candidats quarantainés du client appelant uniquement ;
6. `sources` et `notes` : identifiants, raison de sélection, `recall_complete` et codes
   d'avertissement bornés en cas d'omission fail-closed.

Cette répartition par `kind` est stricte : `current` n'accueille que `preference`, `decision` et
`fact`, un `project_state` va dans `next_action` et un `episode` dans `relevant`.

Le budget est borné par `[capsule]` sur le payload fallback et structuré sérialisé. Le nombre
d'octets UTF-8 sérialisés sert de plafond conservateur à un octet par token et de limite absolue de
taille du payload. Les éléments de priorité la plus faible sont omis en premier avec une note
explicite, et un scope surdimensionné est représenté par une empreinte bornée ; une entrée stale,
superseded, expired ou quarantined d'un autre client ne peut pas se glisser dans `current`.

## Consolidation Datacron

Le gateway parle au serveur Datacron en MCP stdio et applique les allowlists configurées. Le flux
est volontairement en deux temps :

Le sous-processus Datacron vit dans un thread propriétaire non-daemon. Les budgets
`startup_timeout_ms`, `request_timeout_ms` et `shutdown_timeout_ms` bornent chaque frontière. À la
fermeture, le transport coupe stdin puis termine l'arbre de processus avec un Job Object sous
Windows ou un groupe de processus sous POSIX ; un timeout empoisonne la session et interdit son
rejeu implicite.

1. `--plan` relit le chemin canonique, recherche plusieurs variantes de sections voisines, classe
   create/link/skip, ancre un snapshot SQLite immuable et produit JSON + Markdown ;
2. un humain modifie uniquement chaque décision approve/reject ;
3. `--apply` vérifie l'artefact contre le snapshot et consomme le plan ; `new` crée une seule note
   canonique puis la relit, `redundant` revérifie et lie la note sans écriture, tandis que `update`
   et `contradictory` restent `skip` ;
4. Engram marque `promoted` seulement si la création exacte ou la liaison exacte est revérifiée ;
5. `--check-freshness` compare ensuite les hashes sans réécrire le vault.

Une erreur sur une proposition n'autorise pas le forçage d'une autre. Un rapport apply contenant
`failed` ou `stale` sort avec le code 6. Le plan consommé ne peut pas être rejoué ; les propositions
non résolues doivent être replanifiées depuis l'état Datacron courant.

Seuls `plan_id` et les décisions franchissent la frontière de revue comme entrées. Chemin, heading,
niveau, contenu, hashes, voisins, classification et action doivent correspondre exactement au
snapshot de confiance ; le reciblage manuel est refusé. Apply régénère toujours les voisins courants
et recontrôle la cible live avant création ou liaison. Les chemins emploient des slashs canoniques
et les headings tiennent sur une ligne. Un chemin de création contient l'ID candidat et n'admet
aucune variante. Après une réponse ambiguë, seule une note dont le contenu canonique complet est
identique, hors normalisation des fins de ligne et du newline final, devient `redundant`. Tout
contenu supplémentaire reste `update/skip`.
En fin de lot, apply relit chaque chemin potentiellement écrit et marque stale toute promotion dont
le hash de note complète diverge.
