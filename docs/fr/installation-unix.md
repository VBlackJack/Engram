# Installer en service sous macOS et Linux

[Français](installation-unix.md) | [English](../en/installation-unix.md)

> **Utilisez cette page quand** vous avez terminé le [démarrage en 5 minutes](quick-start.md) et
> que vous voulez garder Engram actif sans terminal.<br>
> **Temps :** 10 minutes.<br>
> **Résultat :** `engram serve` démarre à l'ouverture de session et est supervisé par le système.<br>
> **Vérifié avec :** Engram `2026.0730.02`, le 2026-08-13.

## Pourquoi cette page existe

`engram setup autostart` enregistre une tâche planifiée Windows. Elle est **réservée à Windows** :
sur toute autre plateforme elle refuse avec le code de sortie `2` et ne change rien, plutôt que
d'annoncer une installation qu'elle n'a pas faite. macOS et Linux disposent déjà d'un superviseur
par session utilisateur ; Engram utilise le leur au lieu d'en inventer un.

Le moteur, lui, est portable. Tout ce qui suit lance le même `engram serve` que la tâche Windows.

## Avant de commencer

Rassemblez trois chemins absolus ; chacun des fichiers d'unité ci-dessous en a besoin, et aucun ne
peut être relatif :

```text
uv run --python 3.14.6 engram doctor
```

**Résultat attendu :** la ligne `configuration` affiche le chemin absolu du `engram.toml` résolu par
le chargeur, et la ligne `database` affiche le chemin absolu de la base visée. Notez-les.

Trouvez ensuite l'exécutable à lancer. Avec un checkout synchronisé par `uv sync`, il se trouve dans
l'environnement virtuel du projet :

```text
cd /chemin/vers/Engram
uv sync --python 3.14.6
readlink -f .venv/bin/engram
```

**Résultat attendu :** un chemin absolu du type `/home/vous/Engram/.venv/bin/engram`. Utilisez ce
chemin dans les fichiers d'unité. Appeler directement l'exécutable du `.venv`, plutôt que de passer
par `uv run`, évite au processus supervisé un parent qui peut lui-même résoudre ou verrouiller le
projet.

Dans toute cette page, remplacez :

- `/home/vous/Engram/.venv/bin/engram` par le chemin que vous venez d'afficher ;
- `/home/vous/Engram/engram.toml` par le chemin de votre configuration ;
- `/home/vous/Engram` par le répertoire qui contient cette configuration.

`--config` est une option globale et se place **avant** la sous-commande :
`engram --config <chemin> serve`.

## Linux : une unité utilisateur systemd

Une unité **utilisateur**, pas une unité système. Engram écrit dans votre répertoire personnel,
détient un verrou exclusif sur une base qui vous appartient et n'écoute que sur la boucle locale ;
le lancer en root n'apporte rien et rend la base illisible par le compte qui l'utilise.

Créez `~/.config/systemd/user/engram.service` :

```ini
[Unit]
Description=Engram local MCP memory broker
Documentation=https://github.com/VBlackJack/Engram
After=default.target

[Service]
Type=simple
WorkingDirectory=/home/vous/Engram
ExecStart=/home/vous/Engram/.venv/bin/engram --config /home/vous/Engram/engram.toml serve
ExecStop=/home/vous/Engram/.venv/bin/engram --config /home/vous/Engram/engram.toml stop
Restart=on-failure
RestartSec=5

[Install]
WantedBy=default.target
```

`ExecStop` lance `engram stop`, qui demande au démon de fermer la base et attend la libération du
verrou de propriété. C'est ce qui transforme un arrêt en fermeture SQLite propre, au lieu d'un
signal qui peut laisser le journal d'écriture anticipée derrière lui.

Activez et démarrez :

```bash
systemctl --user daemon-reload
systemctl --user enable --now engram.service
```

**Résultat attendu :** la commande suivante indique `active (running)`.

| Je veux... | Commande |
| --- | --- |
| Voir l'état | `systemctl --user status engram.service` |
| Démarrer maintenant | `systemctl --user start engram.service` |
| Arrêter maintenant | `systemctl --user stop engram.service` |
| Redémarrer | `systemctl --user restart engram.service` |
| Démarrer à l'ouverture de session | `systemctl --user enable engram.service` |
| Ne plus démarrer à l'ouverture de session | `systemctl --user disable engram.service` |
| Lire le journal du service | `journalctl --user -u engram.service -f` |

Vérifiez avec le diagnostic d'Engram lui-même plutôt qu'avec l'opinion de systemd sur le processus :

```bash
/home/vous/Engram/.venv/bin/engram --config /home/vous/Engram/engram.toml doctor
```

**Résultat attendu :** `daemon` indique `serving`, et `endpoint` indique que l'URL accepte les
connexions.

Une unité utilisateur s'arrête à la fermeture de votre dernière session, sauf si le « lingering »
est activé. Pour garder Engram actif entre deux ouvertures de session sur une machine que vous
administrez :

```bash
loginctl enable-linger "$USER"
```

Le fichier de log propre à Engram reste là où pointe `[logging].path` ; `journalctl` ne montre que
ce que le processus a écrit sur sa console.

## macOS : un LaunchAgent launchd

Un **LaunchAgent** dans votre répertoire personnel, pas un LaunchDaemon : même raisonnement que
ci-dessus, et `~/Library/LaunchAgents` est l'emplacement qui s'exécute sous votre identité.

Créez `~/Library/LaunchAgents/com.github.vblackjack.engram.plist` :

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.github.vblackjack.engram</string>

    <key>ProgramArguments</key>
    <array>
        <string>/Users/vous/Engram/.venv/bin/engram</string>
        <string>--config</string>
        <string>/Users/vous/Engram/engram.toml</string>
        <string>serve</string>
    </array>

    <key>WorkingDirectory</key>
    <string>/Users/vous/Engram</string>

    <key>RunAtLoad</key>
    <true/>

    <key>KeepAlive</key>
    <dict>
        <key>SuccessfulExit</key>
        <false/>
    </dict>

    <key>StandardOutPath</key>
    <string>/Users/vous/Engram/logs/launchd.out.log</string>

    <key>StandardErrorPath</key>
    <string>/Users/vous/Engram/logs/launchd.err.log</string>
</dict>
</plist>
```

`KeepAlive`/`SuccessfulExit=false` relance Engram après un plantage mais le laisse arrêté après un
`engram stop` délibéré, qui sort en `0`. Sans ce dictionnaire, launchd redémarrerait immédiatement
le démon que vous venez de demander d'arrêter.

Chargez et démarrez :

```bash
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.github.vblackjack.engram.plist
launchctl kickstart -k gui/$(id -u)/com.github.vblackjack.engram
```

| Je veux... | Commande |
| --- | --- |
| Voir l'état | `launchctl print gui/$(id -u)/com.github.vblackjack.engram` |
| Lister brièvement | `launchctl list \| grep engram` |
| Démarrer ou redémarrer | `launchctl kickstart -k gui/$(id -u)/com.github.vblackjack.engram` |
| Arrêter le processus | `/Users/vous/Engram/.venv/bin/engram --config /Users/vous/Engram/engram.toml stop` |
| Activer à l'ouverture de session | `launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.github.vblackjack.engram.plist` |
| Désactiver et décharger | `launchctl bootout gui/$(id -u)/com.github.vblackjack.engram` |

Sur les versions de macOS antérieures à `bootstrap`, les équivalents sont `launchctl load -w
<plist>` et `launchctl unload -w <plist>`. Préférez `bootstrap`/`bootout` là où les deux existent.

Vérifiez de la même façon :

```bash
/Users/vous/Engram/.venv/bin/engram --config /Users/vous/Engram/engram.toml doctor
```

## Arrêter et redémarrer pour une procédure opérateur

Toute procédure du [guide opérateur](operator-guide.md) qui exige l'arrêt du démon est satisfaite
par :

```text
engram --config /chemin/absolu/engram.toml stop
```

Redémarrez ensuite avec `systemctl --user start engram.service` ou
`launchctl kickstart -k gui/$(id -u)/com.github.vblackjack.engram`. Sous systemd, `systemctl --user
stop` lance déjà `engram stop` via `ExecStop` ; les deux points d'entrée sont corrects.

## Si le service ne démarre pas

1. Lancez `engram --config <chemin absolu> doctor` à la main, sous le même utilisateur. Il nomme la
   réparation de chaque contrôle en échec.
2. Vérifiez que les chemins du fichier d'unité sont absolus et existent toujours. Une unité
   n'hérite presque rien de l'environnement de votre shell : `~`, `$HOME` et un `engram.toml`
   relatif en sont la cause habituelle.
3. Lisez le journal du service : `journalctl --user -u engram.service -n 50`, ou le fichier
   `StandardErrorPath` sous macOS.
4. Vérifiez que rien d'autre ne détient déjà la base. `engram doctor` rapporte le pid propriétaire,
   et un second démon échoue sur le verrou par construction.
5. Pour un symptôme que le diagnostic n'explique pas, utilisez la [FAQ](faq.md).
