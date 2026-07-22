# Architecture

[Francais](architecture.md) | [English](../en/architecture.md)

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
instance Engram doit ecrire la base. Le serveur serialise les mutations et renvoie `server busy,
retry` si le verrou ne peut pas etre acquis dans le delai configure.

Un fichier de coordination place a cote de la base porte un verrou OS exclusif. Le daemon le garde
pendant sa duree de vie ; chaque writer offline le garde pendant toute sa commande. Windows utilise
un octet verrouille via le CRT, tandis que POSIX utilise `flock`. Le PID et la commande ne sont que
des metadonnees de diagnostic : un fichier laisse par un processus mort est repris des que le verrou
kernel a disparu. Le fichier n'est pas unlink a la liberation afin d'eviter les races de remplacement
d'inode. Les lectures pures de `list` utilisent SQLite `mode=ro` sans ce verrou writer.

## Stockage SQLite

SQLite est ouvert en WAL, avec foreign keys, busy timeout et migrations transactionnelles. Le
plancher 3.51.3 evite le bug WAL-reset. La table `entries` est canonique ; les tables FTS et
vectorielles sont derivees et reconstructibles par `engram reindex`.

`audit_log` est append-only. Il conserve l'acteur, l'action, l'identifiant d'entree et une empreinte
de detail, jamais le statement ni un payload de conversation.

## Serveur MCP HTTP

FastMCP expose un endpoint Streamable HTTP (`127.0.0.1:8377/mcp` par defaut) et deux outils aux
schemas stricts. `remember` passe par la file d'ecriture. `recall` effectue recherche et assemblage
dans un worker, sans mutation de confiance.

Le MCP ne recoit que les appels d'outils. Il n'observe pas la conversation du client, raison pour
laquelle le [protocole client](client-protocol.md) fait partie du produit.

## Retrieval

Le mode `fts` interroge FTS5 avec BM25, puis departage par recence. Une recherche substring bornee
sert de filet si la requete FTS ne renvoie rien. Le mode `hybrid`, derriere configuration, combine
FTS et embeddings par reciprocal rank fusion (`rrf_k`). L'endpoint d'embeddings est local et
compatible avec l'API OpenAI ; une indisponibilite degrade explicitement le rappel vers FTS.

## Capsule recall

La politique D6/D7 separe le ranking de la confiance. La capsule est remplie dans cet ordre :

1. `current` : preferences, decisions et faits actifs et dignes de confiance ;
2. `next_action` : etats projet courants ;
3. `relevant` : episodes pertinents ;
4. `conflicts` : versions non resolues, symetriques et sur demande ;
5. `own_pending` : candidats quarantaines du client appelant uniquement ;
6. `sources` et `notes` : identifiants et raison de selection.

Le budget est borne par `[capsule]`. Les elements de priorite la plus faible sont omis en premier,
avec une note explicite ; une entree stale, superseded, expired ou quarantined d'un autre client ne
peut pas se glisser dans `current`.

## Consolidation Datacron

Le gateway parle au serveur Datacron en MCP stdio et applique les allowlists configurees. Le flux
est volontairement en deux temps :

1. `--plan` recherche les sections voisines, classe create/patch/skip et produit JSON + Markdown ;
2. un humain choisit approve/reject et peut corriger la cible ;
3. `--apply` relit la note, compare le hash CAS, ecrit via MCP, puis relit ;
4. Engram marque `promoted` seulement si la relecture confirme la mutation ;
5. `--check-freshness` compare ensuite les hashes sans reecrire le vault.

Une erreur sur une proposition n'autorise pas le forcement d'une autre. Les propositions `stale`
doivent etre replannifiees depuis l'etat Datacron courant.
