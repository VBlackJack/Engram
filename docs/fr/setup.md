# Mise en place

[Francais](setup.md) | [English](../en/setup.md)

## 1. Installer Engram

Engram exige Python 3.13+ et SQLite 3.51.3+ dans le module `sqlite3` de ce Python. Installer avec le
runtime Python 3.14.3 gere par `uv`, puis verifier SQLite : un build Python peut encore embarquer
une bibliotheque plus ancienne. La CI remplace explicitement celle-ci par SQLite officiel 3.53.3.

```powershell
git clone https://github.com/VBlackJack/Engram.git
Set-Location Engram
uv sync --extra dev --python 3.14.3
uv run --python 3.14.3 python -c "import sqlite3; print(sqlite3.sqlite_version)"
Copy-Item engram.example.toml engram.toml
```

Si la version affichee est inferieure a `3.51.3`, suivre
[installation-windows.md](installation-windows.md) ou choisir un runtime Python plus recent.

## 2. Configurer et lancer

Editer `engram.toml`. Le serveur accepte uniquement des literals IP loopback (`127.0.0.1` ou
`::1`), conserve la base dans `engram.db`, utilise FTS et desactive les ecritures Datacron par
defaut.

Pour mettre a niveau une configuration existante, ajuster les limites capsule avant de
redemarrer :

```toml
[capsule]
default_token_budget = 4800
min_token_budget = 1200
max_token_budget = 6000
```

Le minimum doit valoir au moins 1200 et le maximum doit etre superieur ou egal au budget par
defaut. Engram refuse au demarrage les anciennes limites plus petites, car elles ne peuvent pas
contenir l'enveloppe de reponse bornee obligatoire.

```powershell
uv run --python 3.14.3 engram serve
```

L'endpoint est `http://127.0.0.1:8377/mcp`. Un seul processus Engram doit utiliser cette base comme
writer. Tester la connexion avec un client MCP, pas avec un navigateur : l'endpoint parle le
protocole MCP Streamable HTTP.

## 3. Brancher Claude

### Claude Code

La commande officielle accepte le transport HTTP :

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

Ajouter le texte de [client-protocol.md](client-protocol.md) aux instructions utilisateur de
Claude Code, ou dans un `CLAUDE.md` local non commite. La syntaxe est documentee dans le
[guide MCP Claude Code](https://code.claude.com/docs/en/mcp).

### Claude Desktop

Claude Desktop configure les serveurs HTTP distants dans **Settings > Connectors > Add custom
connector**. Ce connecteur est resolu par l'infrastructure distante de Claude : `127.0.0.1` sur
votre PC n'est donc pas joignable. Pour Desktop, publier Engram derriere un proxy HTTPS
authentifie (tunnel prive/VPN avec controle d'acces), puis enregistrer
`https://engram.example/mcp`. Le fichier historique `claude_desktop_config.json` n'est pas le
mecanisme des connecteurs distants. Voir le
[guide des connecteurs MCP distants](https://support.claude.com/en/articles/11503834-build-custom-connectors-via-remote-mcp-servers).

Pour un usage strictement local, Claude Code est le chemin direct.

## 4. Brancher Codex

Ajouter le serveur dans `~/.codex/config.toml` :

```toml
[mcp_servers.engram]
url = "http://127.0.0.1:8377/mcp"
enabled = true
required = true
startup_timeout_sec = 10
tool_timeout_sec = 30
```

Redemarrer Codex. Dans l'application desktop, l'equivalent se trouve dans **Settings > MCP
servers > Add server > Streamable HTTP**. Ajouter le texte de
[client-protocol.md](client-protocol.md) aux instructions utilisateur ou dans un `AGENTS.md` local
approprie. La reference officielle est le [guide MCP Codex](https://developers.openai.com/codex/mcp/).

## 5. Brancher Gemini

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

Dans Gemini CLI, executer `/mcp`. Dans VS Code, recharger la fenetre si le serveur n'apparait pas.
Placer le texte de [client-protocol.md](client-protocol.md) dans le `GEMINI.md` utilisateur ou de
projet. Le mode agent Gemini Code Assist pour IntelliJ ne supporte pas actuellement ces outils MCP ;
utiliser Gemini CLI ou VS Code. Voir la
[documentation Gemini Code Assist](https://developers.google.com/gemini-code-assist/docs/use-agentic-chat-pair-programmer).

## 6. Verification fonctionnelle

Dans chaque client :

1. appeler `recall` avec une requete de contexte et `scope="user"` ;
2. appeler `remember` avec un fait de test non sensible ;
3. rappeler la meme requete depuis le meme client et verifier `own_pending` ;
4. rappeler depuis un autre client et verifier que ce candidat n'apparait pas dans son
   `own_pending`.

Le candidat n'apparait pas dans `current` tant qu'il n'a pas suivi le chemin d'attestation.
