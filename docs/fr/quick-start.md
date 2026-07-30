# Demarrage en 5 minutes

[Francais](quick-start.md) | [English](../en/quick-start.md)

> **Objectif :** lancer Engram et verifier un premier souvenir.<br>
> **Temps :** 5 a 10 minutes, hors installation de Python.<br>
> **Risque :** faible avec une nouvelle base. Pour une base existante, utiliser le
> [guide operateur](operator-guide.md).<br>
> **Resultat final :** un client MCP peut appeler `recall` et `remember`.

Gardez seulement l'etape en cours ouverte. Les explications detaillees sont liees, pas necessaires
pour finir ce parcours.

## 1. Installer

Ce parcours exige Git et `uv`. Verifiez-les d'abord :

```powershell
git --version
uv --version
```

**Vous devez voir :** les deux numeros de version. Sinon, installez
[Git](https://git-scm.com/downloads) ou
[`uv`](https://docs.astral.sh/uv/getting-started/installation/) avant de continuer.

Dans PowerShell, pour une nouvelle installation :

> **STOP reprise :** si vous reutilisez un checkout, une configuration ou une base Engram, ne
> lancez pas ce bloc. Utilisez le
> [guide operateur](operator-guide.md#migrer-une-base-existante).

```powershell
git clone https://github.com/VBlackJack/Engram.git
Set-Location Engram
uv sync --python 3.14.3
uv run --python 3.14.3 python -c "import sqlite3; print(sqlite3.sqlite_version)"
if (Test-Path -LiteralPath "engram.toml") { throw "Existing Engram configuration: stop" }
if (Test-Path -LiteralPath "engram.db") { throw "Existing Engram database: stop" }
Copy-Item engram.example.toml engram.toml -ErrorAction Stop
```

**Vous devez voir :** une version SQLite egale ou superieure a `3.51.3`, puis un fichier
`engram.toml`.

**Sinon :** suivez uniquement le
[depannage Windows et SQLite](installation-windows.md).

## 2. Lancer

```powershell
uv run --python 3.14.3 engram serve
```

**Vous devez voir :** le processus reste actif et l'endpoint local est
`http://127.0.0.1:8377/mcp`.

Gardez ce terminal ouvert. Un navigateur n'est pas un test MCP.

**Sinon :** allez a [Le client ne se connecte pas](faq.md#le-client-ne-se-connecte-pas).

## 3. Connecter un seul client

Choisissez une seule option dans le [guide de mise en place](setup.md#3-choisir-un-seul-client) :

- Claude Code ;
- Codex ;
- Gemini CLI ou Gemini Code Assist.

**Vous devez voir :** un serveur nomme `engram` et deux outils, `recall` et `remember`.

Vous n'avez pas besoin de configurer les trois clients pour continuer.

## 4. Installer le comportement memoire

Copiez le bloc **Ready-to-paste instruction** du
[protocole client](client-protocol.md#texte-dinstruction-pret-a-coller) dans les instructions du
client choisi.

**Pourquoi :** MCP transporte les appels, mais Engram ne voit pas passivement la conversation.
Sans ce protocole, le serveur fonctionne et le client peut simplement oublier de l'utiliser.

## 5. Verifier

Demandez au client d'appeler `recall` avec :

```text
query = "Engram demarrage et prochaine action"
scope = "project/engram"
```

**Vous devez voir :**

- une capsule structuree ;
- `notes.recall_complete = true`, ou un avertissement qui explique pourquoi le rappel est
  incomplet ;
- probablement des listes vides sur une nouvelle base.

Demandez ensuite un `remember` de test non sensible :

```text
statement = "Le test de connexion Engram est termine."
kind = "episode"
scope = "project/engram"
subject_keys = ["engram:connection-test"]
```

Rappelez la meme requete depuis le meme client.

**Vous devez voir :** le candidat dans `own_pending`. Il ne doit pas apparaitre dans `current` :
c'est la quarantaine normale.

## C'est fini

- Pour l'usage de tous les jours : [guide utilisateur](user-guide.md).
- Pour choisir entre Engram, Datacron et Cortex :
  [guide de la trilogie](datacron-cortex.md).
- Pour attester, migrer, reindexer ou consolider :
  [guide operateur](operator-guide.md).
- En cas de symptome precis : [FAQ](faq.md).
