# VeillePrix

Veille tarifaire auto-hébergée. Relève le prix de pages produit, garde
l'historique, et prévient par email dès qu'un tarif change.

Aucun SaaS, aucun compte, aucune dépendance obligatoire : un fichier Python,
une base SQLite, deux emails par jour.

```
pages produit → relevé (JSON-LD / microdata / OpenGraph / texte)
              → SQLite (watches + prices)
              → alerte immédiate sur variation
              → bordereau quotidien avec minimum 24 h et 7 jours
```

## Ce que ça fait

- **Relève** une page produit toutes les N minutes (réglable par suivi).
- **Extrait** le prix en quatre passes : JSON-LD, microdata, OpenGraph, puis
  montant répété dans le texte. Une fourchette commerciale (`39,95 – 157,95 €`)
  n'est jamais prise pour un prix.
- **Détecte** les pages bloquées (captcha, 403) au lieu d'inventer un prix.
- **Alerte** en moins de cinq minutes après une variation : ancien prix, nouveau
  prix, écart, pourcentage.
- **Bordereau** quotidien qui montre le **minimum des 24 dernières heures** —
  donc une baisse passagère reste visible même si le prix est revenu.
- **Prévient** au troisième échec consécutif si une page ne répond plus.
- **Historicise** tout : `report <id>` donne le min, le max et chaque relevé.

## Installation

```bash
git clone <ce dépôt> veilleprix
cd veilleprix
python3 -m venv .venv
.venv/bin/pip install -e ".[extract,dev]"   # extruct + bs4 + lxml + pytest
.venv/bin/python veilleprix.py init
```

Sans `[extract]`, le moteur fonctionne quand même : il retombe sur les
expressions régulières intégrées. `extruct` améliore la couverture
(JSON-LD, microdata, OpenGraph normalisés).

### Docker

```bash
cp .env.example .env      # SMTP_USER / SMTP_PASS
docker compose up -d --build
docker compose exec veilleprix python veilleprix.py add "https://exemple.fr/p" --email vous@exemple.fr
```

Le conteneur relève toutes les 15 minutes et envoie le bordereau à 8 h.
La base vit dans `./data/veilleprix.db` sur l'hôte — un simple `cp` suffit à la sauvegarder.

## Usage

```bash
# suivre une page
veilleprix.py add "https://exemple.fr/produit" \
    --label "Concurrent Lyon" \
    --email client@exemple.fr \
    --target 399 \
    --interval 60

# état des suivis
veilleprix.py list

# une passe de relevé (à mettre en cron)
veilleprix.py check

# bordereau des dernières 24 h
veilleprix.py digest

# historique d'un suivi
veilleprix.py report 1

# tester l'extraction sur une URL sans l'enregistrer
veilleprix.py test "https://exemple.fr/produit"
```

### Planification

```cron
*/15 * * * * cd /opt/veilleprix && .venv/bin/python veilleprix.py check  >> log 2>&1
0 8 * * *    cd /opt/veilleprix && .venv/bin/python veilleprix.py digest >> log 2>&1
```

Le planificateur tourne toutes les 15 minutes ; chaque suivi n'est relevé que si
son propre intervalle est écoulé. Un seul timer suffit pour plusieurs formules.

## Alertes

Deux canaux, indépendants :

- **Email** : renseigner `SMTP_USER` et `SMTP_PASS` (mot de passe d'application
  Gmail, pas le mot de passe du compte) dans l'environnement, ou dans
  `~/.hermes/.env`, `~/.secrets/smtp.env`, ou un fichier `.md` de votre choix
  listé dans `send_email()`.
- **Discord** (optionnel) : un exécutable `~/.local/bin/notify-discord` qui lit
  le message sur l'entrée standard.

Sans configuration, tout reste inerte : rien ne casse, rien n'est envoyé.

## Configuration

| Variable | Défaut | Rôle |
|---|---|---|
| `VEILLEPRIX_DB` | `./veilleprix.db` | chemin de la base SQLite |
| `SMTP_HOST` | `smtp.gmail.com` | serveur d'envoi |
| `SMTP_PORT` | `587` | port STARTTLS |
| `SMTP_USER` | — | adresse d'envoi |
| `SMTP_PASS` | — | mot de passe d'application |
| `VEILLEPRIX_LEAD_CHANNEL` | — | salon Discord pour `ingest` |

## Schéma de la base

```sql
watches(id, url, label, email, target, currency, active,
        last_price, last_status, last_checked, fail_count,
        interval_min, created_at)
prices(id, watch_id, price, currency, method, checked_at)
```

## Tests

```bash
.venv/bin/pytest
```

Douze tests sans réseau : JSON-LD, microdata, OpenGraph, page bloquée, absence de
prix, fourchette refusée, repli texte, conversion des nombres, formats français,
intervalle par suivi, rendu des emails.

## Licence

MIT — voir `LICENSE`. Le code est ouvert ; l'hébergement et le support ne le sont pas.

## English

Self-hosted price monitoring. Fetches product pages, extracts prices from
JSON-LD / microdata / OpenGraph with a text fallback, stores history in SQLite,
and emails on every change plus a daily digest that keeps the 24 h low water mark.
MIT licensed, no dependencies required.
