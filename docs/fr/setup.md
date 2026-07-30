# Mise en place

[Francais](setup.md) | [English](../en/setup.md)

> **Objectif :** installer Engram et connecter un seul client MCP.<br>
> **Temps :** 10 a 20 minutes.<br>
> **Resultat :** le client affiche `recall` et `remember`.<br>
> **Verifie avec :** Engram `2026.0730.02`, le 2026-07-30.

## 1. Installer Engram

Engram exige Git, `uv`, Python 3.13+ et SQLite 3.51.3+ dans le module `sqlite3` de ce Python.

```powershell
git --version
uv --version
```

**Resultat attendu :** les deux commandes affichent une version. Sinon, installez
[Git](https://git-scm.com/downloads) ou
[`uv`](https://docs.astral.sh/uv/getting-started/installation/).

```powershell
git clone https://github.com/VBlackJack/Engram.git
Set-Location Engram
uv sync --python 3.14.3
uv run --python 3.14.3 python -c "import sqlite3; print(sqlite3.sqlite_version)"
if (Test-Path -LiteralPath "engram.toml") { throw "Existing Engram configuration: stop" }
if (Test-Path -LiteralPath "engram.db") { throw "Existing Engram database: stop" }
Copy-Item engram.example.toml engram.toml -ErrorAction Stop
```

**Resultat attendu :** SQLite affiche `3.51.3` ou plus recent et `engram.toml` existe.

Si la version est trop ancienne, arretez-vous et suivez
[Windows et SQLite](installation-windows.md). Un `sqlite3.exe` recent dans le PATH ne remplace pas
la bibliotheque chargee par Python.

## 2. Configurer et lancer

Avec une nouvelle installation, les valeurs sures de `engram.example.toml` suffisent :

- endpoint IP loopback `127.0.0.1:8377/mcp` ;
- base locale `engram.db` ;
- retrieval FTS ;
- ecritures Datacron desactivees.

> **STOP reprise :** si vous mettez a niveau ou reutilisez une base anterieure, ne lancez pas la
> commande suivante. Le `engram.toml` neuf cree a l'etape 1 est normal. Pour une ancienne base,
> sauvegardez puis suivez
> [Migrer une base existante](operator-guide.md#migrer-une-base-existante).

Pour une nouvelle base uniquement, lancez :

```powershell
uv run --python 3.14.3 engram serve
```

**Resultat attendu :** le processus reste actif sans erreur. Gardez ce terminal ouvert.

Un seul processus Engram doit ecrire cette base. Testez avec un client MCP, pas avec un navigateur.

## 3. Choisir un seul client

Les options suivantes sont independantes. Configurez-en une, puis passez directement a la
[verification](#4-verification-fonctionnelle).

### Option A : Claude

#### Claude Code

```powershell
claude mcp add --transport http engram http://127.0.0.1:8377/mcp --scope user
claude mcp list
```

Equivalent dans un `.mcp.json` de projet :

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
dans un `CLAUDE.md` local non commite.

**Resultat attendu :** `claude mcp list` affiche `engram` connecte. Reference :
[guide MCP Claude Code](https://code.claude.com/docs/en/mcp).

#### Claude Desktop

Claude Desktop resout ses connecteurs HTTP depuis une infrastructure distante : `127.0.0.1` sur
votre PC n'est pas joignable. Pour Desktop, placez Engram derriere un proxy HTTPS authentifie, puis
ajoutez cette URL dans **Settings > Connectors > Add custom connector**.

Les extensions Desktop et les serveurs MCP locaux sont un mecanisme distinct. Engram ne livre pas
encore d'extension Desktop ni de transport stdio ; pour cette release HTTP, choisissez Claude Code
en local ou un connecteur distant securise. References :
[connecteurs MCP distants](https://support.claude.com/en/articles/11175166-get-started-with-custom-connectors-using-remote-mcp) et
[connecteurs Desktop ou web](https://support.claude.com/en/articles/11725091-when-to-use-desktop-and-web-connectors).

### Option B : Codex

Ajoutez dans `~/.codex/config.toml` :

```toml
[mcp_servers.engram]
url = "http://127.0.0.1:8377/mcp"
enabled = true
required = true
startup_timeout_sec = 10
tool_timeout_sec = 30
```

Redemarrez Codex, puis ajoutez le [protocole client](client-protocol.md) aux instructions
utilisateur ou a un `AGENTS.md` de portee adaptee.

**Resultat attendu :** Codex voit `recall` et `remember` sous `engram`. Dans l'application desktop,
le meme reglage se trouve dans **Settings > MCP servers > Add server > Streamable HTTP**.
Reference : [guide MCP Codex](https://developers.openai.com/codex/mcp/).

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
executez `/mcp`. Rechargez VS Code si necessaire.

**Resultat attendu :** `/mcp` affiche Engram et ses deux outils. Gemini Code Assist pour IntelliJ
prend aussi en charge MCP, mais utilise un fichier `mcp.json` separe dans le dossier de
configuration de l'IDE ; ne reutilisez pas automatiquement `~/.gemini/settings.json`.
Reference : [documentation Gemini Code Assist](https://developers.google.com/gemini-code-assist/docs/use-agentic-chat-pair-programmer).

## 4. Verification fonctionnelle

Dans le client choisi :

1. appelez `recall` avec une requete de contexte et `scope="user"` ;
2. appelez `remember` avec un episode de test non sensible ;
3. rappelez la meme requete depuis le meme client ;
4. verifiez que le candidat apparait dans `own_pending`, pas dans `current`.

Le candidat est volontairement en quarantaine. Un autre client ne doit pas le voir dans son propre
`own_pending`.

**Etape suivante :** suivez le [guide utilisateur](user-guide.md). Si un resultat manque, ouvrez
la [FAQ](faq.md) avant de modifier plusieurs reglages.
