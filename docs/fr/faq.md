# FAQ et dépannage

[Français](faq.md) | [English](../en/faq.md)

## Commencez ici, quel que soit le symptôme

```text
uv run --python 3.14.6 engram doctor
```

Il mesure en une passe : l'interpréteur, la version de SQLite face au plancher, **quel** fichier de
configuration a été résolu et s'il se charge, la base et sa version de schéma, ce qui détient le
verrou de la base, si l'endpoint accepte les connexions, et si le fichier de log peut être écrit.
Chaque ligne en échec affiche la commande qui la répare. Il sort en `0` sauf si un contrôle a
échoué ; un avertissement n'est pas un échec. `--json` produit le même rapport en un seul document
pour un script.

Lisez tout le rapport avant de changer quoi que ce soit : le diagnostic ne s'arrête pas au premier
problème, parce que le second est souvent celui qui explique le premier.

Accès rapide :

- [Le client ne se connecte pas](#le-client-ne-se-connecte-pas)
- [Le démon refuse de s'arrêter](#le-demon-refuse-de-sarreter)
- [Le candidat reste dans own_pending](#le-candidat-est-dans-own_pending-pas-dans-current)
- [La consolidation dit stale](#la-consolidation-dit-stale)
- [Cortex ne voit pas une note récente](#cortex-ne-voit-pas-une-note-datacron-recente)

## `Configuration file does not exist`

Lancer `engram init`, qui écrit une configuration de départ là où le chargeur la cherche, ou définir
`ENGRAM_CONFIG` avec un chemin absolu. Les chemins relatifs dans TOML sont résolus depuis le dossier
du fichier choisi, et `engram doctor` affiche le chemin exact qu'il a essayé.

`engram init` écrit la copie empaquetée dans la distribution : elle n'a besoin d'aucun checkout à
côté d'elle. Elle refuse de remplacer un fichier existant ; `engram init --force` le remplace
délibérément.

## `SQLite 3.51.3 or newer is required`

Le `sqlite3` du Python actif est trop ancien, même si une commande `sqlite3` récente est dans le
PATH. Le message nomme une URL de documentation et `engram doctor`, plutôt qu'un fichier situé dans
un checkout ; lancez ce dernier, qui affiche la version trouvée, le plancher à franchir et
l'interpréteur d'où elle vient :

```text
uv run --python 3.14.6 engram doctor
```

Réparez en lançant Engram sur un interpréteur dont le `sqlite3` lie une bibliothèque plus récente —
par exemple celui que fournit `uv python install 3.14.6` — ou suivez
[Windows et SQLite](installation-windows.md) pour remplacer la DLL d'un runtime que vous devez
conserver.

## Le client ne se connecte pas

Lancez d'abord `engram doctor`. Ses lignes `daemon` et `endpoint` séparent trois causes qui, vues
du client, se ressemblent :

- `daemon` avertit que rien ne détient la base : Engram ne tourne pas. Lancez-le, ou installez
  l'intégration de démarrage ([Windows](setup.md#2-configurer-et-lancer),
  [macOS et Linux](installation-unix.md)).
- `endpoint` échoue alors que `daemon` indique `serving` : le démon s'est lié à une autre adresse
  que celle visée par le client. Comparez `[server].host` et `[server].port` avec le log.
- `endpoint` avertit que l'URL accepte alors qu'aucun démon ne détient cette base : **un autre
  processus écoute là**, et le client l'atteint au lieu de votre mémoire. Donnez à cette
  installation son propre port, ou arrêtez l'autre.

Vérifiez sinon que l'URL finit par `/mcp` et correspond au contenu du fichier du client.
`engram setup client claude --print` — ou `codex`, ou `gemini` — affiche le bloc construit depuis
votre propre configuration : c'est aussi le moyen le plus rapide de voir l'endpoint auquel Engram
croit. Un navigateur n'est pas un test MCP. Pour Claude Desktop, localhost n'est pas joignable par
le connecteur distant ; utiliser Claude Code ou un proxy HTTPS authentifié.

<a id="le-demon-refuse-de-sarreter"></a>

## Le démon refuse de s'arrêter

```text
uv run --python 3.14.6 engram stop
```

Cela fonctionne pour toutes les installations, y compris la tâche d'ouverture de session Windows et
un service systemd ou launchd, dont aucun ne possède de console à interrompre. La commande attend
sur le verrou de propriété et rapporte si le démon s'est réellement arrêté, au lieu de le supposer.

Ne créez pas la sentinelle `<base>.stop` à la main. La commande résout ce chemin depuis la
configuration que le démon lui-même a chargée ; un chemin tapé de mémoire qui ne correspond à aucune
configuration livrée écrit la demande là où personne ne regarde, et donne l'illusion d'avoir
fonctionné.

Si `engram stop` échoue, il nomme le pid qui détient encore la base et laisse la demande en place.
Lisez le log avant de terminer ce processus : tuer un démon en pleine écriture est ce qui laisse un
journal d'écriture anticipée derrière lui.

## Mon candidat n'apparaît pas dans `own_pending`

`own_pending` est isolé par l'identité MCP `clientInfo.name/clientInfo.version`. Une nouvelle version
du client ou un autre client constitue un autre writer. Vérifier aussi le `scope`, les `kinds`, la
requête, le TTL et le budget.

## Le candidat est dans `own_pending`, pas dans `current`

C'est le comportement de sécurité normal pour cette entrée : tout élément visible dans
`own_pending` est un candidat non confirmé en quarantaine. Il faut une attestation explicite avant
qu'il puisse devenir actif et partagé. Un contenu canonique déjà actif et fiable serait retourné
avec l'outcome `existing_trusted` et ne créerait pas ce candidat.

## J'ai attesté une entrée et elle n'apparaît pas dans `current`

`current` ne contient que les `preference`, `decision` et `fact`. Un `project_state` attesté
apparaît dans `next_action`, et un `episode` dans `relevant`. Le tableau complet est dans
[Attester un candidat](operator-guide.md#attester-un-candidat).

Une entrée dépourvue de `claim_key` est par ailleurs omise entièrement, avec l'avertissement
`unclassified_claim_omitted` : `--claim-key` est obligatoire pour `preference`, `decision` et
`fact`.

## `server busy, retry`

Un write est déjà en cours ou plusieurs instances utilisent la même base. Vérifier qu'un seul
processus Engram est writer, puis retenter avec backoff. Augmenter `write_wait_timeout_ms` seulement
après diagnostic.

## L'endpoint hybride est injoignable

Engram journalise la dégradation et utilise FTS. Vérifier `embeddings_endpoint`, le nom exact de
`embeddings_model`, le timeout et la disponibilité du serveur. Revenir à `mode = "fts"` pour une
exploitation sans embeddings.

## La recherche FTS rate une variante morphologique

Essayer des termes du statement ou des `subject_keys`. Engram applique des préfixes contrôlés après
les étages phrase exacte, tous les termes et au moins un terme, mais ce n'est ni un stemmer ni un
correcteur orthographique flou. Utiliser le mode hybride pour les paraphrases sans vocabulaire
commun.

## La consolidation dit `stale`

La note Datacron a changé après le plan. Ne pas forcer ni remplacer le hash. Régénérer `--plan`,
relire la nouvelle proposition, l'approuver, puis relancer `--apply`.

## La consolidation refuse un chemin

Le chemin est hors `read_paths`/`write_paths`, l'allowlist d'écriture est vide, ou le nouveau dossier
n'est pas sous `_memory/`. Corriger `engram.toml` ; ne pas contourner la validation.

## Une promotion disparaît de `current`

`--check-freshness` a pu détecter un hash Datacron divergent et marquer l'entrée stale. Consulter le
rapport JSON/Markdown sous `local/consolidation`, puis refaire une revue.

## La capsule omet des résultats

Lire `notes.why_returned`. Si une note indique des omissions budget, demander un `token_budget` plus
grand, dans les bornes `[capsule]`, ou préciser `scope`, `kinds` et `query`.

## Engram a disparu après un redémarrage ou une déconnexion

Aucune intégration de démarrage n'a été installée. `engram serve` dans un terminal dure exactement
aussi longtemps que ce terminal.

- **Windows :** `uv run --python 3.14.6 engram setup autostart --install` enregistre une tâche
  d'ouverture de session qui lance le démon sans fenêtre de console. Contrôlez-la avec `--status`.
- **macOS / Linux :** `engram setup autostart` est réservé à Windows et sort en `2` ailleurs.
  Installez l'unité systemd ou l'agent launchd de
  [Installer en service sous macOS et Linux](installation-unix.md).

<a id="cortex-ne-voit-pas-une-note-datacron-recente"></a>

## Cortex ne voit pas une note Datacron récente

Cortex n'a pas de watcher et Datacron ne l'appelle pas. Lancez :

```text
cortex sync
```

Puis vérifiez `cortex_freshness` depuis le client MCP. La note Datacron reste canonique pendant que
l'index Cortex est en retard. Voir le [guide de la trilogie](datacron-cortex.md).

## La CLI renvoie le code `4`

Une dépendance externe est indisponible : Datacron pendant une consolidation, ou l'endpoint
d'embeddings pendant une opération hybride. Relancez avec le flag global `--debug` uniquement
après avoir vérifié la configuration et la disponibilité de la dépendance concernée.

## La CLI renvoie le code `2` et je ne sais pas quel réglage est faux

Le code `2` signale l'usage ou la configuration. `engram doctor` nomme le fichier de configuration
résolu et la première clé qui l'empêche de se charger :

```text
uv run --python 3.14.6 engram doctor
```

`engram setup autostart` sort aussi en `2` sous macOS et Linux, par construction : la commande est
réservée à Windows. Utilisez
[Installer en service sous macOS et Linux](installation-unix.md) là-bas.
