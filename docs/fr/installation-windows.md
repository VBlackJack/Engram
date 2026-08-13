# Installation Windows et SQLite

[Français](installation-windows.md) | [English](../en/installation-windows.md)

> **Utilisez cette page uniquement si** `engram doctor` affiche une version de SQLite inférieure à
> `3.51.3`, ou si l'erreur de plancher SQLite nomme cette page.<br>
> **Vérifié le :** 2026-08-13.

Cette page est spécifique à Windows. Sur un hôte POSIX, les conseils sur l'interpréteur restent
valables, mais le remplacement de DLL ne s'applique pas ; voir
[Installer en service sous macOS et Linux](installation-unix.md) pour la suite de la mise en place.

## Pourquoi ce prérequis est dur

Engram utilise SQLite en mode WAL. SQLite documente un bug de corruption WAL-reset dans les
versions 3.7.0 à 3.51.2, corrigé en 3.51.3 et dans les branches maintenues ultérieures. Engram
échoue fermé avant migration si le module Python charge une version plus ancienne.

Référence : [WAL-reset bug](https://sqlite.org/wal.html#walreset).

## Le chemin clef en main : un build uv précis

Ce n'est pas `uv` qui fait fonctionner Engram, c'est un build particulier. Mesures sous Windows :

| Distribution | SQLite lié | Satisfait `3.51.3` |
| --- | --- | --- |
| python.org 3.12.10 | 3.49.1 | non |
| python.org 3.13.6 | 3.50.4 | non |
| python.org 3.14.6 | 3.50.4 | non |
| uv-managed 3.13.12 | 3.50.4 | non |
| uv-managed 3.14.3 | 3.53.3 | oui |
| uv-managed 3.14.4 | 3.50.4 | non |
| **uv-managed 3.14.6** | **3.53.1** | **oui** |

**Le SQLite lié par un runtime se décide par build, pas par version de Python.** La ligne 3.14.4
n'est pas une coquille : ce build est repassé sous le plancher avant que les suivants ne repassent
au-dessus, et un même numéro de version lie des SQLite différents selon le système d'exploitation.
Ne jamais déduire la version liée de la version de Python, sur aucune plateforme. Lancez le
contrôle.

Tout autre chemin de cette page répare un runtime qui échouerait autrement. Installez plutôt celui
qui fonctionne ; il ne modifie pas un runtime existant :

```text
uv python install 3.14.6
uv sync --python 3.14.6
uv run --python 3.14.6 engram doctor
```

Installer `3.14.6` exige `uv` 0.12.1 ou plus récent : les versions antérieures ne connaissent que
les builds jusqu'à `3.14.3`.

La ligne `sqlite` doit afficher une version au moins égale à `3.51.3` (le build testé pour cette
release affiche `3.53.1`), et la ligne `python` nomme l'interpréteur mesuré. Utiliser le même
`--python 3.14.6` pour `serve` et les autres commandes. Les contributeurs installent séparément
`--extra dev` avant les tests.

L'équivalent en une ligne, pour obtenir les numéros bruts sans le reste du diagnostic :

```text
uv run --python 3.14.6 python -c "import sys, sqlite3; print(sys.executable); print(sqlite3.sqlite_version)"
```

### Demandez le patch, pas la mineure

`uv python install 3.14` n'est pas cette méthode. Sans numéro de patch, `uv` installe le build 3.14
le plus récent que **sa propre version** connaît : le build obtenu est donc décidé par le `uv` que
vous avez sous la main, pas par ce que vous avez demandé. Or certains de ces builds, dont `3.14.4`,
lient un SQLite sous le plancher. Nommez `3.14.6`.

Si vous utilisez délibérément un autre build, rien ne l'interdit ici, mais `engram doctor` est la
seule preuve qu'il fonctionne. Lancez-le. S'il affiche une version inférieure à `3.51.3`, ce build
ne peut pas faire tourner Engram : installez `3.14.6`, ou réparez le runtime existant avec la
méthode DLL ci-dessous.

**Un `pip install` qui passe ne prouve rien ici.** Il passe sur toutes les distributions du tableau
ci-dessus, y compris les quatre qui ne peuvent pas faire tourner Engram. Le contrôle de version est
le seul signal.

Le prérequis porte sur SQLite, pas sur Python : un runtime 3.13 dont la DLL a été remplacée
fonctionne, et c'est pourquoi le paquet ne refuse pas de s'installer sur 3.13.

## Réparer un runtime existant : remplacement de la DLL SQLite

À utiliser uniquement s'il faut conserver un runtime CPython Windows déjà en place, et seulement si
son `_sqlite3.pyd` charge une DLL `sqlite3.dll` séparée. Préférez l'interpréteur géré par uv
ci-dessus dès que le choix est libre. Ne jamais remplacer un fichier pendant qu'un processus Python
tourne.

1. Identifier l'interpréteur et la version chargée :

   ```text
   python -c "import sys, sqlite3, _sqlite3; print(sys.executable); print(_sqlite3.__file__); print(sqlite3.sqlite_version)"
   ```

2. Fermer Engram, Python et les IDE qui utilisent ce runtime. Pour Engram, `engram stop` ferme la
   base proprement, y compris pour un démon lancé par la tâche d'ouverture de session.
3. Télécharger `sqlite-dll-win-x64-3530300.zip` (ou `win-arm64` selon l'architecture) depuis la
   [page officielle SQLite](https://www.sqlite.org/download.html). Pour 3.53.3 x64, vérifier le
   SHA3-256 publié :
   `3a494861ce24d1f330efbc6c3fb58ce4972f2cf8df4e43122246ed987109dc8a`.
4. Trouver la `sqlite3.dll` du runtime, en général à côté de `python.exe` ou dans son dossier
   `DLLs`. Copier l'ancienne DLL vers `sqlite3.dll.backup-<version>`.
5. Extraire l'archive et remplacer uniquement cette `sqlite3.dll`, en conservant l'architecture
   x64/ARM64 du runtime. Ne pas copier la DLL dans `System32` et ne pas modifier une installation
   Python partagée sans accord administrateur.
6. Rouvrir un terminal et vérifier :

   ```text
   python -c "import sqlite3; print(sqlite3.sqlite_version); assert sqlite3.sqlite_version_info >= (3, 51, 3)"
   ```

Ou, de façon équivalente, `engram doctor`, qui nomme aussi la réparation si la version est encore
trop basse.

Si Python ne démarre plus ou charge toujours l'ancienne version, restaurer la sauvegarde et utiliser
le runtime géré par uv. Certains builds lient SQLite statiquement : la DLL ne peut alors pas
les mettre à niveau ; il faut remplacer le runtime.

Cette réparation est exercée à chaque exécution d'intégration continue, sur un runtime Windows non
modifié dont l'échec vient d'être constaté : les étapes ci-dessus restent vérifiées et pas seulement
écrites.

La commande Python de l'étape 6 est le contrôle sans mutation. `engram reindex` est une opération de
maintenance qui exige l'arrêt du démon ; utilisez-la uniquement depuis le
[guide opérateur](operator-guide.md#reindexer-engram).

## Serveur HTTP

Après vérification, conserver :

```toml
[server]
host = "127.0.0.1"
port = 8377
path = "/mcp"
```

Autoriser le processus Python dans le pare-feu uniquement sur le profil et l'interface nécessaires.
Pour localhost, aucune ouverture de port entrant depuis le LAN n'est requise.
