# Mise en place

[Français](setup.md) | [English](../en/setup.md)

> **Objectif :** installer Engram et connecter un seul client MCP.<br>
> **Temps :** 10 à 20 minutes.<br>
> **Résultat :** le client affiche `recall` et `remember`.<br>
> **Vérifié avec :** Engram `2026.0730.02`, le 2026-08-13.

Toutes les commandes des sections 1 et 3 sont identiques sous Windows, macOS et Linux. La section 2
est le seul endroit où les systèmes diffèrent, et il est signalé.

## 1. Installer Engram

Engram exige Git, `uv` 0.12.1 ou plus récent, Python 3.13+ et SQLite 3.51.3+ dans le module
`sqlite3` de ce Python.

```text
git --version
uv --version
```

**Résultat attendu :** les deux commandes affichent une version. Sinon, installez
[Git](https://git-scm.com/downloads) ou
[`uv`](https://docs.astral.sh/uv/getting-started/installation/). `uv` 0.12.1 est la première version
qui connaît le build `3.14.6` ; les versions antérieures ne connaissent que des builds jusqu'à
`3.14.3`.

```text
git clone https://github.com/VBlackJack/Engram.git
cd Engram
uv sync --python 3.14.6
uv run --python 3.14.6 engram init
```

**Résultat attendu :** `engram init` affiche le fichier écrit, le chemin de base auquel ce fichier
aboutit, l'endpoint, puis `Next: engram doctor`.

`engram init` écrit la configuration de départ depuis la copie empaquetée dans la distribution.
Elle n'a besoin ni d'un checkout, ni d'une syntaxe propre à un shell, ni d'un `engram.example.toml`
à côté d'elle : elle se comporte donc de la même façon depuis une installation par wheel et sur
tous les systèmes. Elle refuse de remplacer un `engram.toml` existant ; `--force` le remplace
délibérément.

Vérifiez ensuite l'installation avant de lancer quoi que ce soit :

```text
uv run --python 3.14.6 engram doctor
```

**Résultat attendu :** `[ ok ]` pour `python`, `sqlite` et `configuration`. `database` et `daemon`
restent en avertissement tant que le démon n'a pas tourné une première fois. Chaque ligne en échec
affiche la commande qui la répare, et `engram doctor` ne sort en non-zéro que si un contrôle a
échoué.

Si SQLite est trop ancien, arrêtez-vous et suivez
[Windows et SQLite](installation-windows.md). Une commande `sqlite3` récente dans le PATH ne
remplace pas la bibliothèque chargée par Python.

## 2. Configurer et lancer

Avec une nouvelle installation, les valeurs écrites par `engram init` suffisent :

- endpoint IP loopback `127.0.0.1:8377/mcp` ;
- base locale `engram.db` ;
- retrieval FTS ;
- écritures Datacron désactivées.

> **STOP reprise :** si vous mettez à niveau ou réutilisez une base antérieure, ne lancez pas la
> commande suivante. Le `engram.toml` neuf créé à l'étape 1 est normal. Pour une ancienne base,
> sauvegardez puis suivez
> [Migrer une base existante](operator-guide.md#migrer-une-base-existante).

<a id="windows-la-tache-douverture-de-session"></a>

### Windows : la tâche d'ouverture de session

Pour une nouvelle base uniquement, installez le démarrage automatique :

```text
uv run --python 3.14.6 engram setup autostart --install
```

**Résultat attendu :** la commande sort en 0 et affiche un JSON dont `started` vaut `true`. Engram
tourne dès maintenant et redémarrera à chaque ouverture de session. **Aucun terminal ne doit rester
ouvert :** la tâche lance l'interpréteur sans console, donc il n'y a pas de fenêtre à fermer par
inadvertance.

Vérifiez :

```text
uv run --python 3.14.6 engram setup autostart --status
uv run --python 3.14.6 engram doctor
```

**Résultat attendu :** `installed` vaut `true` et `daemon_running` vaut `true` ; `engram doctor`
affiche `[ ok ] daemon: serving, pid ...` et `[ ok ] endpoint: http://127.0.0.1:8377/mcp accepts`.

Ce que la commande fait, et ce qu'elle ne fait pas :

| Action | Effet | Code de sortie |
|---|---|---|
| `--install` | Enregistre ou met à jour **une seule** tâche pour ce `engram.toml`, puis démarre le démon si la base est libre. Rejouer la commande converge, sans doublon. | `0` |
| `--status` | N'écrit rien. Répond dans le JSON, jamais par le code de sortie. Le champ `interpreter_present` dit si l'interpréteur **enregistré dans la tâche** existe toujours : `installed: true` avec `interpreter_present: false` décrit une tâche qui ne démarrera plus. | `0` dans tous les cas |
| `--uninstall` | Supprime la tâche. Sur une tâche déjà absente, `removed` vaut `false`. | `0` |

#### Reprendre une installation antérieure

Si Engram était déjà lancé au démarrage par un autre mécanisme — une tâche planifiée posée à la
main, un script de lancement — `--install` **refuse** et nomme la tâche concernée :

```text
uv run --python 3.14.6 engram setup autostart --install
```

**Résultat attendu :** code de sortie non nul, et un message de la forme
`Another registered task would open this database: 'Engram Local Daemon' ...`.

La détection compare **la base de données visée**, pas le nom de la tâche. Une tâche qui passe par
un script intermédiaire n'annonce pas sa configuration : dans ce cas la commande ne conclut pas à
l'absence de conflit, elle signale l'indétermination et refuse quand même. Une réponse inconnue
n'est pas une réponse négative.

Pour reprendre l'installation :

```text
uv run --python 3.14.6 engram setup autostart --install --replace
```

**Résultat attendu :** code 0, et un JSON dont `disabled` nomme la tâche reprise.

```text
uv run --python 3.14.6 engram setup autostart --status
```

**Résultat attendu :** `installed` vaut `true`, `daemon_running` vaut `true`, et `conflicts` est
vide.

> **La tâche reprise est désactivée, pas supprimée.** Sa définition reste intacte dans le
> planificateur. Pour revenir en arrière, réactivez-la depuis PowerShell puis désactivez celle
> d'Engram :
>
> ```powershell
> Enable-ScheduledTask -TaskName "<nom rapporte dans disabled>"
> ```
>
> La suppression définitive reste votre geste, jamais celui de la commande.

`--replace` arrête proprement le démon issu de la tâche reprise et **attend la libération du
verrou de la base**, pas un délai fixe. Rejouer `--install --replace` sur un système déjà convergé
sort en 0 sans rien changer.

En dernier recours, `--force` installe malgré un conflit ou une indétermination. À n'utiliser que
si vous savez que l'autre tâche n'ouvrira pas cette base : deux démons sur la même base, c'est le
second qui meurt sur le verrou.

Points à connaître :

- la tâche est nommée d'après le chemin de votre `engram.toml`. Deux installations distinctes ont
  deux tâches distinctes, et aucune ne remplace l'autre en silence ;
- `--install` ne démarre pas un second démon si la base est déjà détenue. Il l'écrit dans
  `start_skipped_reason` au lieu de faire semblant d'avoir réussi ;
- hors Windows, la commande échoue explicitement en code `2` et ne fait rien. Sur un autre système,
  utilisez [Installer en service sous macOS et Linux](installation-unix.md) ;
- pour cibler un fichier de configuration précis, ajoutez `--config <chemin>` avant la
  sous-commande. La tâche enregistre ce chemin en clair, elle n'hérite d'aucune variable
  d'environnement.

### macOS et Linux : systemd ou launchd

`engram setup autostart` construit une tâche planifiée Windows et rien d'autre ; sur toute autre
plateforme elle refuse avec le code `2` plutôt que de faire croire à une installation. Les fichiers
d'unité équivalents, prêts à remplir, sont dans
[Installer en service sous macOS et Linux](installation-unix.md) : une unité **utilisateur** systemd
pour Linux et un **LaunchAgent** launchd pour macOS, tous deux lançant `engram serve` avec un
chemin `--config` absolu.

En attendant d'en installer un, lancez le démon dans un terminal :

```text
uv run --python 3.14.6 engram serve
```

### Arrêter le démon proprement

Quel que soit ce qui l'a lancé — tâche d'ouverture de session, systemd, launchd ou un terminal —
une seule commande demande au démon propriétaire de cette base de la fermer et de sortir, puis
attend et rapporte ce qui s'est réellement passé :

```text
uv run --python 3.14.6 engram stop
```

**Résultat attendu :** un JSON avec `"stopped": true`, et **`engram.db-wal` comme `engram.db-shm`
disparaissent**. C'est la preuve observable d'une fermeture propre : SQLite ne supprime ces deux
fichiers qu'à la fermeture de la dernière connexion. Si aucun processus ne détient la base, la
commande répond `"requested": false, "stopped": true` et ne change rien.

`engram stop` n'annonce pas une réussite qu'il n'a pas obtenue. Il attend sur le verrou de
propriété, et si le démon détient toujours la base à l'expiration du délai, il échoue et le dit,
en laissant la demande en place.

Le mécanisme sous-jacent est un fichier sentinelle : un démon sans console ne peut recevoir ni
`Ctrl+C` ni `Ctrl+Break`, donc `engram stop` dépose un `<base>.stop` vide à côté du
`[database].path` configuré — le même répertoire que le `<base>.lock` — et le démon l'efface en
partant. Utilisez la commande plutôt que le fichier : la commande résout ce chemin depuis la
configuration même que le démon a chargée, alors qu'un chemin tapé à la main qui ne correspond à
aucune configuration livrée écrit la demande là où personne ne regarde, et donne l'illusion d'avoir
fonctionné.

Le droit d'arrêter Engram est donc exactement le droit d'écrire dans le répertoire de sa base, qui
est déjà le droit de la corrompre. Aucun port n'expose cette capacité.

Une sentinelle oubliée n'empêche pas le démarrage suivant : le démon efface celle qu'il trouve
**après** avoir pris le verrou. Un second `serve` lancé par erreur échoue sur le verrou sans
toucher à une demande d'arrêt destinée au démon en place.

Pour un diagnostic au premier plan, `engram serve` reste disponible et se comporte comme avant :
il occupe le terminal et s'arrête à sa fermeture ou sur `Ctrl+C`. Arrêtez d'abord le démon, car un
seul processus Engram peut écrire cette base.

Testez avec un client MCP, pas avec un navigateur.

## 3. Choisir un seul client

### La commande unique qui configure n'importe lequel

```text
uv run --python 3.14.6 engram setup client claude
uv run --python 3.14.6 engram setup client codex
uv run --python 3.14.6 engram setup client gemini
```

N'en lancez qu'une. Chacune écrit la configuration MCP du fournisseur en utilisant **l'endpoint de
la configuration chargée**, pas le `8377` qu'affiche cette page : une installation qui a changé de
port reste donc correcte sans que personne n'ait à remarquer la différence.

| Client | Fichier écrit | Fichier d'instructions pour `--protocol` |
| --- | --- | --- |
| `claude` | `.mcp.json` dans le répertoire courant | `CLAUDE.md` dans le répertoire courant |
| `codex` | `~/.codex/config.toml` | `AGENTS.md` dans le répertoire courant |
| `gemini` | `~/.gemini/settings.json` | `GEMINI.md` dans le répertoire courant |

| Option | Effet |
| --- | --- |
| `--protocol` | Ajoute aussi le [protocole client](client-protocol.md) au fichier d'instructions de ce client, une seule fois. Rejouer la commande ne change rien. |
| `--print` | Affiche le bloc au lieu de l'écrire. Rien n'est modifié. |
| `--force` | Remplace une entrée `engram` existante qui nomme un endpoint **différent**. Sans cette option, la commande refuse plutôt que de réorienter en silence un client qui fonctionne. |

Elle fusionne, elle n'écrase pas : les autres serveurs MCP, les clés sans rapport et les
commentaires TOML de ces fichiers survivent. Rejouée alors que l'entrée est déjà correcte, elle
n'écrit rien et le dit.

**Résultat attendu :** le fichier existe et contient votre endpoint. Redémarrez le client, puis
passez à la [vérification](#4-verification-fonctionnelle).

La suite de cette section est la solution de repli, à écrire à la main, pour un client que cette
commande ne couvre pas ou un fichier que vous préférez rédiger vous-même. Les options sont
indépendantes ; configurez-en une.

### Option A : Claude

#### Claude Code

```text
claude mcp add --transport http engram http://127.0.0.1:8377/mcp --scope user
claude mcp list
```

Équivalent dans un `.mcp.json` de projet — c'est ce qu'écrit `engram setup client claude` :

```json
{
  "mcpServers": {
    "engram": {
      "type": "http",
      "url": "http://127.0.0.1:8377/mcp"
    }
  }
}
```

Ajoutez le [protocole client](client-protocol.md) aux instructions utilisateur de Claude Code ou
dans un `CLAUDE.md` local non commité.

**Résultat attendu :** `claude mcp list` affiche `engram` connecté. Référence :
[guide MCP Claude Code](https://code.claude.com/docs/en/mcp).

#### Claude Desktop

Claude Desktop résout ses connecteurs HTTP depuis une infrastructure distante : `127.0.0.1` sur
votre PC n'est pas joignable. Pour Desktop, placez Engram derrière un proxy HTTPS authentifié, puis
ajoutez cette URL dans **Settings > Connectors > Add custom connector**.

Les extensions Desktop et les serveurs MCP locaux sont un mécanisme distinct. Engram ne livre pas
encore d'extension Desktop ni de transport stdio ; pour cette release HTTP, choisissez Claude Code
en local ou un connecteur distant sécurisé. Références :
[connecteurs MCP distants](https://support.claude.com/en/articles/11175166-get-started-with-custom-connectors-using-remote-mcp) et
[connecteurs Desktop ou web](https://support.claude.com/en/articles/11725091-when-to-use-desktop-and-web-connectors).

### Option B : Codex

Ajoutez dans `~/.codex/config.toml` :

```toml
[mcp_servers.engram]
url = "http://127.0.0.1:8377/mcp"
enabled = true
startup_timeout_sec = 10
tool_timeout_sec = 30
```

**N'ajoutez jamais la clé `required` à ce bloc.** OpenAI la définit comme faisant échouer le
démarrage de Codex quand le serveur ne peut pas s'initialiser : un courtier de mémoire simplement
arrêté emporterait alors tout votre assistant avec lui. Le bloc ci-dessus est exactement ce
qu'écrit `engram setup client codex`, et il omet cette clé délibérément.

Redémarrez Codex, puis ajoutez le [protocole client](client-protocol.md) aux instructions
utilisateur ou à un `AGENTS.md` de portée adaptée.

**Résultat attendu :** Codex voit `recall` et `remember` sous `engram`. Dans l'application desktop,
le même réglage se trouve dans **Settings > MCP servers > Add server > Streamable HTTP**.
Référence : [guide MCP Codex](https://developers.openai.com/codex/mcp/).

### Option C : Gemini

Gemini CLI et Gemini Code Assist pour VS Code partagent `~/.gemini/settings.json` :

```json
{
  "mcpServers": {
    "engram": {
      "httpUrl": "http://127.0.0.1:8377/mcp"
    }
  }
}
```

Placez le [protocole client](client-protocol.md) dans le `GEMINI.md` utilisateur ou projet, puis
exécutez `/mcp`. Rechargez VS Code si nécessaire.

**Résultat attendu :** `/mcp` affiche Engram et ses deux outils. Gemini Code Assist pour IntelliJ
prend aussi en charge MCP, mais utilise un fichier `mcp.json` séparé dans le dossier de
configuration de l'IDE ; ne réutilisez pas automatiquement `~/.gemini/settings.json`.
Référence : [documentation Gemini Code Assist](https://developers.google.com/gemini-code-assist/docs/use-agentic-chat-pair-programmer).

<a id="4-verification-fonctionnelle"></a>

## 4. Vérification fonctionnelle

Avant d'ouvrir le client, confirmez une fois le côté serveur :

```text
uv run --python 3.14.6 engram doctor
```

**Résultat attendu :** aucune ligne `[fail]`, `daemon` indique `serving`, et `endpoint` indique que
votre URL accepte les connexions. Si le client ne se connecte toujours pas après cela, le problème
est dans le fichier de configuration du client, pas dans Engram.

Dans le client choisi :

1. appelez `recall` avec une requête de contexte et `scope="user"` ;
2. appelez `remember` avec un épisode de test non sensible ;
3. rappelez la même requête depuis le même client ;
4. vérifiez que le candidat apparaît dans `own_pending`, pas dans `current`.

Le candidat est volontairement en quarantaine. Un autre client ne doit pas le voir dans son propre
`own_pending`.

**Étape suivante :** suivez le [guide utilisateur](user-guide.md). Si un résultat manque, ouvrez
la [FAQ](faq.md) avant de modifier plusieurs réglages.
