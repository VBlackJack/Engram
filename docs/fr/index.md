# Documentation Engram

[Francais](index.md) | [English](../en/index.md)

Engram est la memoire operationnelle locale de la trilogie Datacron, Cortex, Engram. Ce hub
oriente vers le parcours le plus court selon le besoin.

## Demarrer

| Guide | Contenu |
| --- | --- |
| [Mise en place](setup.md) | Installation, configuration et connexion Claude, Codex, Gemini |
| [Windows et SQLite](installation-windows.md) | Mise a niveau de la DLL SQLite et verification |
| [Guide utilisateur](user-guide.md) | Usage quotidien, reindex, evaluation et consolidation |
| [Protocole client](client-protocol.md) | Instructions de session pretes a coller |

## Comprendre

| Reference | Contenu |
| --- | --- |
| [Contrat de donnees](spec.md) | Kinds, champs, provenance, cycle de vie, TTL et fraicheur |
| [Architecture](architecture.md) | SQLite, MCP HTTP, retrieval, capsule et gateway Datacron |
| [README](../../README.md) | Vue d'ensemble, commandes et configuration |
| [Changelog](../../CHANGELOG.md) | Historique des releases CalVer |

## Securite

| Guide | Contenu |
| --- | --- |
| [Modele de securite](security.md) | Frontieres de confiance, quarantaine et confinement |
| [FAQ](faq.md) | Diagnostic par symptome et actions correctives |
| [Notices tiers](../../THIRD_PARTY_NOTICES.md) | Dependances et licences |

Commencer par [setup.md](setup.md), puis installer le [protocole client](client-protocol.md) dans
chaque client connecte. Sans ce protocole, le transport MCP fonctionne mais Engram ne sait pas
quand capturer ou rappeler le contexte.
