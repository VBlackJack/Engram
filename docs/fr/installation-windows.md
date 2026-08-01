# Installation Windows et SQLite

[Francais](installation-windows.md) | [English](../en/installation-windows.md)

> **Utilisez cette page uniquement si** le controle SQLite du
> [demarrage rapide](quick-start.md) affiche une version inferieure a `3.51.3`.<br>
> **Verifie le :** 2026-08-01.

## Pourquoi ce prerequis est dur

Engram utilise SQLite en mode WAL. SQLite documente un bug de corruption WAL-reset dans les
versions 3.7.0 a 3.51.2, corrige en 3.51.3 et dans les branches maintenues ulterieures. Engram
echoue ferme avant migration si le module Python charge une version plus ancienne.

Reference : [WAL-reset bug](https://sqlite.org/wal.html#walreset).

## Le chemin clef en main : un build uv precis

Ce n'est pas `uv` qui fait fonctionner Engram, c'est un build particulier. Mesures sous Windows :

| Distribution | SQLite lie | Satisfait `3.51.3` |
| --- | --- | --- |
| python.org 3.12.10 | 3.49.1 | non |
| python.org 3.13.6 | 3.50.4 | non |
| python.org 3.14.6 | 3.50.4 | non |
| uv-managed 3.13.12 | 3.50.4 | non |
| uv-managed 3.14.3 | 3.53.3 | oui |
| uv-managed 3.14.4 | 3.50.4 | non |
| **uv-managed 3.14.6** | **3.53.1** | **oui** |

**Le SQLite lie par un runtime se decide par build, pas par version de Python.** La ligne 3.14.4
n'est pas une coquille : ce build est repasse sous le plancher avant que les suivants ne repassent
au-dessus, et un meme numero de version lie des SQLite differents selon le systeme d'exploitation.
Ne jamais deduire la version liee de la version de Python, sur aucune plateforme. Lancez le
controle.

Tout autre chemin de cette page repare un runtime qui echouerait autrement. Installez plutot celui
qui fonctionne ; il ne modifie pas un runtime existant :

```powershell
uv python install 3.14.6
uv sync --python 3.14.6
uv run --python 3.14.6 python -c "import sys, sqlite3; print(sys.executable); print(sqlite3.sqlite_version)"
```

Installer `3.14.6` exige `uv` 0.12.1 ou plus recent : les versions anterieures ne connaissent que
les builds jusqu'a `3.14.3`.

La commande doit afficher une version SQLite au moins egale a `3.51.3` (le build teste pour cette
release affiche `3.53.1`). Utiliser le meme `--python 3.14.6` pour `serve` et les autres commandes.
Les contributeurs installent separement `--extra dev` avant les tests.

### Demandez le patch, pas la mineure

`uv python install 3.14` n'est pas cette methode. Sans numero de patch, `uv` installe le build 3.14
le plus recent que **sa propre version** connait : le build obtenu est donc decide par le `uv` que
vous avez sous la main, pas par ce que vous avez demande. Or certains de ces builds, dont `3.14.4`,
lient un SQLite sous le plancher. Nommez `3.14.6`.

Si vous utilisez deliberement un autre build, rien ne l'interdit ici, mais la troisieme commande
ci-dessus est la seule preuve qu'il fonctionne. Lancez-la. Si elle affiche une version inferieure a
`3.51.3`, ce build ne peut pas faire tourner Engram : installez `3.14.6`, ou reparez le runtime
existant avec la methode DLL ci-dessous.

**Un `pip install` qui passe ne prouve rien ici.** Il passe sur toutes les distributions du tableau
ci-dessus, y compris les quatre qui ne peuvent pas faire tourner Engram. Le controle de version est
le seul signal.

Le prerequis porte sur SQLite, pas sur Python : un runtime 3.13 dont la DLL a ete remplacee
fonctionne, et c'est pourquoi le paquet ne refuse pas de s'installer sur 3.13.

## Reparer un runtime existant : remplacement de la DLL SQLite

A utiliser uniquement s'il faut conserver un runtime CPython Windows deja en place, et seulement si
son `_sqlite3.pyd` charge une DLL `sqlite3.dll` separee. Preferez l'interpreteur gere par uv
ci-dessus des que le choix est libre. Ne jamais remplacer un fichier pendant qu'un processus Python
tourne.

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
le runtime gere par uv. Certains builds lient SQLite statiquement : la DLL ne peut alors pas
les mettre a niveau ; il faut remplacer le runtime.

Cette reparation est exercee a chaque execution d'integration continue, sur un runtime Windows non
modifie dont l'echec vient d'etre constate : les etapes ci-dessus restent verifiees et pas seulement
ecrites.

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
