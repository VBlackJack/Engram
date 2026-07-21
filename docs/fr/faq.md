# FAQ et depannage

[Francais](faq.md) | [English](../en/faq.md)

## `Configuration file does not exist`

Copier `engram.example.toml` vers `engram.toml`, ou definir `ENGRAM_CONFIG` avec un chemin absolu.
Les chemins relatifs dans TOML sont resolus depuis le dossier du fichier choisi.

## `SQLite 3.51.3 or newer is required`

Le `sqlite3` du Python actif est trop ancien, meme si l'executable `sqlite3.exe` du PATH est recent.
Verifier avec :

```powershell
uv run --python 3.14.3 python -c "import sys, sqlite3; print(sys.executable); print(sqlite3.sqlite_version)"
```

Utiliser le Python gere par `uv` ou suivre [installation-windows.md](installation-windows.md).

## Le client ne se connecte pas

Verifier qu'Engram tourne, que l'URL finit par `/mcp`, et qu'aucun autre processus n'occupe le port.
Un navigateur n'est pas un test MCP. Pour Claude Desktop, localhost n'est pas joignable par le
connecteur distant ; utiliser Claude Code ou un proxy HTTPS authentifie.

## Mon candidat n'apparait pas dans `own_pending`

`own_pending` est isole par l'identite MCP `clientInfo.name/clientInfo.version`. Une nouvelle version
du client ou un autre client constitue un autre writer. Verifier aussi le `scope`, les `kinds`, la
requete, le TTL et le budget.

## Le candidat est dans `own_pending`, pas dans `current`

C'est le comportement de securite normal : `remember` produit un candidat non confirme en
quarantaine. Il faut une attestation explicite avant qu'il puisse devenir actif et partage.

## `server busy, retry`

Un write est deja en cours ou plusieurs instances utilisent la meme base. Verifier qu'un seul
processus Engram est writer, puis retenter avec backoff. Augmenter `write_wait_timeout_ms` seulement
apres diagnostic.

## L'endpoint hybride est injoignable

Engram journalise la degradation et utilise FTS. Verifier `embeddings_endpoint`, le nom exact de
`embeddings_model`, le timeout et la disponibilite du serveur. Revenir a `mode = "fts"` pour une
exploitation sans embeddings.

## La recherche FTS rate une variante morphologique

Essayer des termes du statement ou des `subject_keys`. Le filet substring ne remplace pas un
stemmer. Porter et les prefixes sont differes jusqu'a preuve d'un besoin reel ; le mode hybride est
la voie d'extension actuelle.

## La consolidation dit `stale`

La note Datacron a change apres le plan. Ne pas forcer ni remplacer le hash. Regenerer `--plan`,
relire la nouvelle proposition, l'approuver, puis relancer `--apply`.

## La consolidation refuse un chemin

Le chemin est hors `read_paths`/`write_paths`, l'allowlist d'ecriture est vide, ou le nouveau dossier
n'est pas sous `_memory/`. Corriger `engram.toml`; ne pas contourner la validation.

## Une promotion disparait de `current`

`--check-freshness` a pu detecter un hash Datacron divergent et marquer l'entree stale. Consulter le
rapport JSON/Markdown sous `local/consolidation`, puis refaire une revue.

## La capsule omet des resultats

Lire `notes.why_returned`. Si une note indique des omissions budget, demander un `token_budget` plus
grand, dans les bornes `[capsule]`, ou preciser `scope`, `kinds` et `query`.
