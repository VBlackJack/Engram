# Installation Windows et SQLite

[Francais](installation-windows.md) | [English](../en/installation-windows.md)

> **Utilisez cette page uniquement si** le controle SQLite du
> [demarrage rapide](quick-start.md) affiche une version inferieure a `3.51.3`.<br>
> **Verifie le :** 2026-07-30.

## Pourquoi ce prerequis est dur

Engram utilise SQLite en mode WAL. SQLite documente un bug de corruption WAL-reset dans les
versions 3.7.0 a 3.51.2, corrige en 3.51.3 et dans les branches maintenues ulterieures. Engram
echoue ferme avant migration si le module Python charge une version plus ancienne.

Reference : [WAL-reset bug](https://sqlite.org/wal.html#walreset).

## Methode recommandee : Python gere par uv

Cette methode ne modifie pas un runtime existant :

```powershell
uv python install 3.14.3
uv sync --python 3.14.3
uv run --python 3.14.3 python -c "import sys, sqlite3; print(sys.executable); print(sqlite3.sqlite_version)"
```

La commande doit afficher une version SQLite au moins egale a `3.51.3` (le build teste pour cette
release affiche `3.53.3`). Utiliser le meme `--python 3.14.3` pour `serve` et les autres commandes.
Les contributeurs installent separement `--extra dev` avant les tests.

## Methode DLL SQLite 3.53.x

Utiliser cette methode uniquement pour un runtime CPython Windows dont `_sqlite3.pyd` charge une
DLL `sqlite3.dll` separee. Ne jamais remplacer un fichier pendant qu'un processus Python tourne.

1. Identifier l'interpreteur et la version chargee :

   ```powershell
   python -c "import sys, sqlite3, _sqlite3; print(sys.executable); print(_sqlite3.__file__); print(sqlite3.sqlite_version)"
   ```

2. Fermer Engram, Python et les IDE qui utilisent ce runtime.
3. Telecharger `sqlite-dll-win-x64-3530300.zip` (ou `win-arm64` selon l'architecture) depuis la
   [page officielle SQLite](https://www.sqlite.org/download.html). Pour 3.53.3 x64, verifier le
   SHA3-256 publie :
   `3a494861ce24d1f330efbc6c3fb58ce4972f2cf8df4e43122246ed987109dc8a`.
4. Trouver la `sqlite3.dll` du runtime, en general a cote de `python.exe` ou dans son dossier
   `DLLs`. Copier l'ancienne DLL vers `sqlite3.dll.backup-<version>`.
5. Extraire l'archive et remplacer uniquement cette `sqlite3.dll`, en conservant l'architecture
   x64/ARM64 du runtime. Ne pas copier la DLL dans `System32` et ne pas modifier une installation
   Python partagee sans accord administrateur.
6. Rouvrir un terminal et verifier :

   ```powershell
   python -c "import sqlite3; print(sqlite3.sqlite_version); assert sqlite3.sqlite_version_info >= (3, 51, 3)"
   ```

Si Python ne demarre plus ou charge toujours l'ancienne version, restaurer la sauvegarde et utiliser
le runtime `uv` recommande. Certains builds lient SQLite statiquement : la DLL ne peut alors pas
les mettre a niveau ; il faut remplacer le runtime.

La commande Python de l'etape 6 est le controle sans mutation. `engram reindex` est une operation de
maintenance qui exige l'arret du daemon ; utilisez-la uniquement depuis le
[guide operateur](operator-guide.md#reindexer-engram).

## Serveur HTTP

Apres verification, conserver :

```toml
[server]
host = "127.0.0.1"
port = 8377
path = "/mcp"
```

Autoriser le processus Python dans le pare-feu uniquement sur le profil et l'interface necessaires.
Pour localhost, aucune ouverture de port entrant depuis le LAN n'est requise.
