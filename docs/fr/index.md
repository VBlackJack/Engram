# Documentation Engram

[Français](index.md) | [English](../en/index.md)

Engram est la mémoire opérationnelle locale de la trilogie Datacron, Cortex et Engram.

## Je veux...

| Objectif | Ouvrir |
| --- | --- |
| Lancer Engram sans tout lire | [Démarrage en 5 minutes](quick-start.md) |
| Utiliser `recall` et `remember` au quotidien | [Guide utilisateur](user-guide.md) |
| Choisir entre Engram, Datacron et Cortex | [Guide de la trilogie](datacron-cortex.md) |
| Connecter Claude, Codex ou Gemini | [Mise en place](setup.md#3-choisir-un-seul-client) |
| Garder Engram actif après une déconnexion, sous Windows | [`engram setup autostart`](setup.md#windows-la-tache-douverture-de-session) |
| Garder Engram actif après une déconnexion, sous macOS ou Linux | [systemd et launchd](installation-unix.md) |
| Installer le comportement automatique du client | [Protocole client](client-protocol.md) |
| Attester, migrer, réindexer ou consolider | [Guide opérateur](operator-guide.md) |
| Savoir pourquoi Engram ne fonctionne pas | `engram doctor`, puis la [FAQ](faq.md) |
| Résoudre un symptôme précis | [FAQ](faq.md) |

Si vous ne savez pas quoi choisir, ouvrez seulement le
[démarrage en 5 minutes](quick-start.md).

## Les quatre commandes qui répondent à la plupart des questions

| Commande | Ce qu'elle répond |
| --- | --- |
| `engram init` | « D'où vient la configuration ? » — écrit `engram.toml` depuis la copie empaquetée, sur tout système d'exploitation et depuis toute installation |
| `engram doctor` | « Pourquoi cela ne marche pas ? » — interpréteur, plancher SQLite, configuration réellement résolue, base et version de schéma, propriétaire du verrou, endpoint, fichier de log, chacun avec sa réparation |
| `engram stop` | « Comment arrêter un démon sans fenêtre ? » — lui demande de fermer la base, attend, et rapporte s'il s'est réellement arrêté |
| `engram setup client claude\|codex\|gemini` | « Que dois-je coller dans mon client ? » — écrit le fichier du fournisseur avec votre propre endpoint, en fusionnant au lieu d'écraser |

## Parcours court

```text
Demarrage en 5 minutes
  -> Guide utilisateur
  -> Guide Engram / Datacron / Cortex
```

Le [guide opérateur](operator-guide.md) est nécessaire uniquement pour modifier la confiance, la
base ou le vault Datacron.

## Références techniques

Ces pages sont utiles pour comprendre ou auditer le produit. Elles ne sont pas nécessaires pour
démarrer.

| Référence | Contenu |
| --- | --- |
| [Contrat de données](spec.md) | Types, champs, provenance, cycle de vie, TTL et fraîcheur |
| [Architecture](architecture.md) | SQLite, MCP HTTP, retrieval, capsule et gateway Datacron |
| [Sécurité](security.md) | Frontières de confiance, quarantaine et confinement |
| [Windows et SQLite](installation-windows.md) | Mise à niveau du runtime SQLite |
| [Installation en service macOS et Linux](installation-unix.md) | Unité utilisateur systemd et LaunchAgent launchd |
| [README](../../README.md) | Vue générale et référence de release |
| [Changelog](../../CHANGELOG.md) | Historique CalVer |
| [Notices tiers](../../THIRD_PARTY_NOTICES.md) | Dépendances et licences |

## Règle de lecture

- Faites une seule procédure à la fois.
- Arrêtez-vous au premier **Résultat attendu** manquant.
- Lancez `engram doctor` avant la FAQ, et la FAQ avant de changer plusieurs réglages.
- Ne lancez jamais une commande opérateur sur une base existante sans sauvegarde.
