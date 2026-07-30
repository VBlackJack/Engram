# Documentation Engram

[Francais](index.md) | [English](../en/index.md)

Engram est la memoire operationnelle locale de la trilogie Datacron, Cortex et Engram.

## Je veux...

| Objectif | Ouvrir |
| --- | --- |
| Lancer Engram sans tout lire | [Demarrage en 5 minutes](quick-start.md) |
| Utiliser `recall` et `remember` au quotidien | [Guide utilisateur](user-guide.md) |
| Choisir entre Engram, Datacron et Cortex | [Guide de la trilogie](datacron-cortex.md) |
| Connecter Claude, Codex ou Gemini | [Mise en place](setup.md) |
| Installer le comportement automatique du client | [Protocole client](client-protocol.md) |
| Attester, migrer, reindexer ou consolider | [Guide operateur](operator-guide.md) |
| Resoudre un symptome precis | [FAQ](faq.md) |

Si vous ne savez pas quoi choisir, ouvrez seulement le
[demarrage en 5 minutes](quick-start.md).

## Parcours court

```text
Demarrage en 5 minutes
  -> Guide utilisateur
  -> Guide Engram / Datacron / Cortex
```

Le [guide operateur](operator-guide.md) est necessaire uniquement pour modifier la confiance, la
base ou le vault Datacron.

## References techniques

Ces pages sont utiles pour comprendre ou auditer le produit. Elles ne sont pas necessaires pour
demarrer.

| Reference | Contenu |
| --- | --- |
| [Contrat de donnees](spec.md) | Types, champs, provenance, cycle de vie, TTL et fraicheur |
| [Architecture](architecture.md) | SQLite, MCP HTTP, retrieval, capsule et gateway Datacron |
| [Securite](security.md) | Frontieres de confiance, quarantaine et confinement |
| [Windows et SQLite](installation-windows.md) | Mise a niveau du runtime SQLite |
| [README](../../README.md) | Vue generale et reference de release |
| [Changelog](../../CHANGELOG.md) | Historique CalVer |
| [Notices tiers](../../THIRD_PARTY_NOTICES.md) | Dependances et licences |

## Regle de lecture

- Faites une seule procedure a la fois.
- Arretez-vous au premier **Resultat attendu** manquant.
- Utilisez la FAQ avant de changer plusieurs reglages.
- Ne lancez jamais une commande operateur sur une base existante sans sauvegarde.
