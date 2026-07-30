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
d'inode. La classe publique `EngramStore` garde le meme lease pendant sa duree de vie ; plusieurs
connexions de ce seul processus le partagent, tandis qu'un autre processus writer est refuse. Les
lectures pures de `list` utilisent SQLite `mode=ro` sans ce verrou writer.

## Stockage SQLite

SQLite est ouvert en WAL, avec foreign keys, busy timeout et migrations transactionnelles. Le
plancher 3.51.3 evite le bug WAL-reset. La table `entries` est canonique ; les tables FTS et
vectorielles sont derivees et reconstructibles par `engram reindex`. Au demarrage, Engram compare
l'index FTS external-content aux lignes canoniques et le reconstruit s'il manque ou diverge.
`consolidation_plans` ancre les snapshots immuables des plans et leur etat d'usage unique hors de
l'artefact de revue editable. Chaque connexion charge d'abord `sqlite_schema` sous un plafond
temporaire de 256 Kio, puis applique un plafond permanent de 8 Mio aux valeurs et lignes SQLite.
Un snapshot de consolidation est refuse avant mutation au-dela de 4 Mio UTF-8.
Le preflight d'upgrade garde le lease writer, lit un snapshot source unique et prouve la migration
complete sur une copie disque jetable. Les definitions des tables canoniques sont controlees
exactement ; un objet derive peut manquer ou etre une table reconstruisible, mais un index, trigger
ou view homonyme est refuse.

`audit_log` est append-only. Il conserve l'acteur, l'action, l'identifiant d'entree et une empreinte
de detail, jamais le statement ni un payload de conversation.

Le log configure tourne a 10 Mio avec cinq backups. Chaque processus Engram ferme le fichier entre
deux records et serialise ecriture/rotation par un verrou OS separe, y compris sous Windows ou un
handle ouvert ferait echouer la rotation fondee sur rename.

## Serveur MCP HTTP

FastMCP expose un endpoint Streamable HTTP (`127.0.0.1:8377/mcp` par defaut) et deux outils aux
schemas stricts. `remember` passe par la file d'ecriture. `recall` effectue recherche et assemblage
dans un worker, sans mutation de confiance.

Le MCP ne recoit que les appels d'outils. Il n'observe pas la conversation du client, raison pour
laquelle le [protocole client](client-protocol.md) fait partie du produit.

## Retrieval

Le mode `fts` derive des termes sans operateurs depuis une entree NFKC bornee. Il classe d'abord la
phrase exacte et la conjonction de tous les termes, puis remplit chaque place restante du top-K
avec les rankings disjonctif et prefixe fusionnes equitablement. Les hits stricts gardent leur
priorite sans masquer les correspondances morphologiques. Chaque etage applique les filtres de
visibilite et un top-K dur dans SQL, avec BM25 puis recence et identifiant pour departager.
Une deadline monotone absolue `fts_query_timeout_ms` couvre l'attente du verrou et tous les etages
SQLite progressifs. Le progress handler SQLite interrompt un scan hors budget ; seuls les etages
termines existent dans le ranking interne, mais le resultat public est vide et marque incomplet
pour eviter une revalidation partielle apres expiration. Le mode `hybrid`, derriere
configuration, combine FTS et embeddings par reciprocal rank fusion (`rrf_k`).
Il calcule un top-K semantique exact uniquement si tous les vecteurs visibles tiennent sous
`hybrid_max_candidates` et les budgets fixes de dimensions/octets. Le scan ne renvoie que les IDs et
vecteurs ; au plus les payloads du top-K fusionne sont materialises puis revalides. Un depassement,
un resultat provider mal forme ou une indisponibilite degrade le rappel vers FTS et marque la
capsule incomplete. Les rebuilds vectoriels utilisent des pages bornees et un stage temporaire,
preservent l'index live jusqu'au swap atomique et comparent `data_version` SQLite avant ce swap pour
refuser un commit intervenu entre-temps.

## Capsule recall

La politique D6/D7 separe le ranking de la confiance. La capsule est remplie dans cet ordre :

1. `current` : preferences, decisions et faits actifs et dignes de confiance ;
2. `next_action` : etats projet courants ;
3. `relevant` : episodes pertinents ;
4. `conflicts` : versions non resolues, symetriques et sur demande ;
5. `own_pending` : candidats quarantaines du client appelant uniquement ;
6. `sources` et `notes` : identifiants, raison de selection, `recall_complete` et codes
   d'avertissement bornes en cas d'omission fail-closed.

Le budget est borne par `[capsule]` sur le payload fallback et structure serialise. Le nombre
d'octets UTF-8 serialises sert de plafond conservateur a un octet par token et de limite absolue de
taille du payload. Les elements de priorite la plus faible sont omis en premier avec une note
explicite, et un scope surdimensionne est represente par une empreinte bornee ; une entree stale,
superseded, expired ou quarantined d'un autre client ne peut pas se glisser dans `current`.

## Consolidation Datacron

Le gateway parle au serveur Datacron en MCP stdio et applique les allowlists configurees. Le flux
est volontairement en deux temps :

1. `--plan` relit le chemin canonique, recherche plusieurs variantes de sections voisines, classe
   create/link/skip, ancre un snapshot SQLite immuable et produit JSON + Markdown ;
2. un humain modifie uniquement chaque decision approve/reject ;
3. `--apply` verifie l'artefact contre le snapshot et consomme le plan ; `new` cree une seule note
   canonique puis la relit, `redundant` reverifie et lie la note sans ecriture, tandis que `update`
   et `contradictory` restent `skip` ;
4. Engram marque `promoted` seulement si la creation exacte ou la liaison exacte est reverifiee ;
5. `--check-freshness` compare ensuite les hashes sans reecrire le vault.

Une erreur sur une proposition n'autorise pas le forcement d'une autre. Un rapport apply contenant
`failed` ou `stale` sort avec le code 6. Le plan consomme ne peut pas etre rejoue ; les propositions
non resolues doivent etre replannifiees depuis l'etat Datacron courant.

Seuls `plan_id` et les decisions franchissent la frontiere de revue comme entrees. Chemin, heading,
niveau, contenu, hashes, voisins, classification et action doivent correspondre exactement au
snapshot de confiance ; le reciblage manuel est refuse. Apply regenere toujours les voisins courants
et recontrole la cible live avant creation ou liaison. Les chemins emploient des slashs canoniques
et les headings tiennent sur une ligne. Un chemin de creation contient l'ID candidat et n'admet
aucune variante. Apres une reponse ambigue, seule une note dont le contenu canonique complet est
identique, hors normalisation des fins de ligne et du newline final, devient `redundant`. Tout
contenu supplementaire reste `update/skip`.
En fin de lot, apply relit chaque chemin potentiellement ecrit et marque stale toute promotion dont
le hash de note complete diverge.
