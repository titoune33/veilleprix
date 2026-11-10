# Journal des versions

## 1.0.0 — 2026-11-10

Première version publiable.

### Extraction
- JSON-LD via `extruct` (couche optionnelle), avec repli intégré sans dépendance.
- Ordre : JSON-LD → microdata → OpenGraph → meta itemprop → montant répété dans le texte.
- Rejet des pages bloquées (captcha, 403) au lieu d'un prix inventé.
- Rejet des fourchettes (`39,95 – 157,95 €`) et des montants uniques dans le texte.

### Suivi
- Intervalle de relevé **par suivi** (`--interval`, en minutes).
- Historique complet en SQLite, min/max via `report`.
- Détection de variation à 0,01 € près, alerte immédiate.

### Emails clients
- Activation : prix de départ, fréquence, lien.
- Variation : ancien prix barré, nouveau prix, pourcentage, écart, objectif atteint.
- Bordereau quotidien : prix actuel, **minimum 24 h**, **minimum 7 jours**.
- Échec : après trois relevés impossibles consécutifs.
- Version texte de secours pour chaque message, pied de page `STOP`.

### Qualité
- 12 tests sans réseau, `pytest`, matrice CI 3.10 / 3.11 / 3.12.
- Job CI séparé sans `extruct` pour garantir le repli.
- MIT.
