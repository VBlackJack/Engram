# Démarrage en 5 minutes

[Français](quick-start.md) | [English](../en/quick-start.md)

> **Objectif :** lancer Engram et vérifier un premier souvenir.<br>
> **Temps :** 5 à 10 minutes, hors installation de Python.<br>
> **Risque :** faible avec une nouvelle base. Pour une base existante, utiliser le
> [guide opérateur](operator-guide.md).<br>
> **Résultat final :** un client MCP peut appeler `recall` et `remember`.

Gardez seulement l'étape en cours ouverte. Les explications détaillées sont liées, pas nécessaires
pour finir ce parcours.

## 1. Installer

Ce parcours exige Git et `uv` 0.12.1 ou plus récent. Vérifiez-les d'abord :

```text
git --version
uv --version
```

**Vous devez voir :** les deux numéros de version. Sinon, installez
[Git](https://git-scm.com/downloads) ou
[`uv`](https://docs.astral.sh/uv/getting-started/installation/) avant de continuer.

Toutes les commandes de cette page sont identiques sous Windows, macOS et Linux.

> **STOP reprise :** si vous réutilisez un checkout, une configuration ou une base Engram, ne
> lancez pas ce bloc. `engram init` refuse d'écraser un `engram.toml` existant, mais le chemin sûr
> pour une base antérieure reste le
> [guide opérateur](operator-guide.md#migrer-une-base-existante).

```text
git clone https://github.com/VBlackJack/Engram.git
cd Engram
uv sync --python 3.14.6
uv run --python 3.14.6 engram init
```

**Vous devez voir :** `Wrote .../engram.toml`, le chemin de base de données auquel ce fichier
aboutit, l'endpoint, puis `Next: engram doctor`.

`engram init` écrit la configuration de départ à partir de la copie empaquetée dans la
distribution : elle fonctionne depuis une installation par wheel comme depuis ce checkout, et sur
tous les systèmes d'exploitation. Elle refuse de remplacer un `engram.toml` existant, sauf avec
`--force`.

L'épinglage `3.14.6` n'est pas un choix de style. Le SQLite lié par un runtime se décide par build,
pas par version de Python, et la plupart des distributions en lient un trop ancien pour Engram ;
`3.14.6` est mesuré conforme sous Windows et sous Linux. Substituer un interpréteur déjà en place
échouera très probablement au contrôle SQLite que `engram doctor` effectue à l'étape suivante. Voir
[Windows et SQLite](installation-windows.md) pour les mesures. L'installer exige `uv` 0.12.1 ou plus
récent.

## 2. Contrôler l'installation

```text
uv run --python 3.14.6 engram doctor
```

**Vous devez voir :** une ligne `[ ok ]` pour `python`, pour `sqlite` (`3.51.3` ou plus récent) et
pour `configuration`. `database` et `daemon` restent en avertissement jusqu'à l'étape suivante ; un
avertissement n'est pas un échec. Chaque ligne en échec affiche la commande qui la répare, et la
commande ne sort en non-zéro que si quelque chose a échoué.

**Sinon :** appliquez la réparation nommée par la ligne en échec. Pour SQLite en particulier,
suivez [Windows et SQLite](installation-windows.md).

## 3. Lancer

```text
uv run --python 3.14.6 engram serve
```

**Vous devez voir :** le processus reste actif et l'endpoint local est
`http://127.0.0.1:8377/mcp`.

Gardez ce terminal ouvert. Un navigateur n'est pas un test MCP. `engram serve` dans un terminal est
la bonne forme pour un premier essai ; pour garder Engram sans terminal, installez l'intégration de
démarrage une fois cette page terminée :

- **Windows :** `uv run --python 3.14.6 engram setup autostart --install` enregistre une tâche
  d'ouverture de session qui lance le démon sans fenêtre de console. Sans elle, Engram s'arrête à
  la prochaine fermeture de session. Détails dans le
  [guide de mise en place](setup.md#2-configurer-et-lancer).
- **macOS / Linux :** `engram setup autostart` est réservé à Windows et sort en `2` ailleurs.
  Utilisez l'unité systemd ou l'agent launchd prêts à remplir dans
  [Installer en service sous macOS et Linux](installation-unix.md).

Pour arrêter le démon plus tard, quelle que soit l'installation :
`uv run --python 3.14.6 engram stop`.

**Sinon :** allez à [Le client ne se connecte pas](faq.md#le-client-ne-se-connecte-pas).

## 4. Connecter un seul client

Le chemin le plus court écrit le fichier du fournisseur à votre place, avec l'endpoint issu de
votre propre configuration :

```text
uv run --python 3.14.6 engram setup client claude --protocol
```

Remplacez `claude` par `codex` ou `gemini`. `--protocol` ajoute aussi le protocole de session de
l'étape 5 dans `CLAUDE.md`, `AGENTS.md` ou `GEMINI.md`. Ajoutez `--print` pour afficher le bloc sans
rien écrire.

Les équivalents écrits à la main, et la limitation de Claude Desktop, sont dans le
[guide de mise en place](setup.md#3-choisir-un-seul-client).

**Vous devez voir :** un serveur nommé `engram` et deux outils, `recall` et `remember`.

Vous n'avez pas besoin de configurer les trois clients pour continuer.

## 5. Installer le comportement mémoire

`engram setup client --protocol` l'a déjà fait. Si vous avez configuré le client à la main, copiez
le bloc **Ready-to-paste instruction** du
[protocole client](client-protocol.md#texte-dinstruction-pret-a-coller) dans les instructions du
client choisi.

**Pourquoi :** MCP transporte les appels, mais Engram ne voit pas passivement la conversation.
Sans ce protocole, le serveur fonctionne et le client peut simplement oublier de l'utiliser.

## 6. Vérifier

Demandez au client d'appeler `recall` avec :

```text
query = "Engram demarrage et prochaine action"
scope = "project/engram"
```

**Vous devez voir :**

- une capsule structurée ;
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

Rappelez la même requête depuis le même client.

**Vous devez voir :** le candidat dans `own_pending`. Il ne doit pas apparaître dans `current` :
c'est la quarantaine normale.

## C'est fini

- Pour que Engram survive à un redémarrage : [guide de mise en place](setup.md#2-configurer-et-lancer)
  sous Windows, [installation en service macOS et Linux](installation-unix.md) ailleurs.
- Pour l'usage de tous les jours : [guide utilisateur](user-guide.md).
- Pour choisir entre Engram, Datacron et Cortex :
  [guide de la trilogie](datacron-cortex.md).
- Pour attester, migrer, réindexer ou consolider :
  [guide opérateur](operator-guide.md).
- En cas de symptôme précis : [FAQ](faq.md). Lancez `engram doctor` d'abord ; il nomme la
  réparation.
