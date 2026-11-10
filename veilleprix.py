#!/usr/bin/env python3
"""
veilleprix — moteur de veille tarifaire.

Surveille des pages produit, relève le prix toutes les heures, conserve
l'historique et alerte dès qu'un tarif change.

Commandes
  init                          crée la base
  add <url> [--label L] [--email E] [--target P]
  remove <id>
  list
  check                         relève tous les prix (une passe)
  digest                        bordereau du jour
  report <id>                   historique d'une adresse
  test <url>                    teste l'extraction sans enregistrer

Aucune dépendance hors bibliothèque standard.
"""

import argparse
import gzip
import io
import json
import os
import re
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone, timedelta
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

HOME = os.path.expanduser("~")
DB_PATH = os.environ.get("VEILLEPRIX_DB", os.path.join(HOME, "work", "veilleprix-engine", "veilleprix.db"))
NOTIFY = os.path.join(HOME, ".local", "bin", "notify-discord")
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/142.0.0.0 Safari/537.36"
)

BLOCK_MARKERS = (
    "captcha",
    "unusual traffic",
    "access denied",
    "accès refusé",
    "are you a human",
    "robot",
    "cf-browser-verification",
    "just a moment",
)


# --------------------------------------------------------------------------
# Base
# --------------------------------------------------------------------------
def db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init():
    with db() as c:
        c.executescript(
            """
            CREATE TABLE IF NOT EXISTS watches (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                url TEXT NOT NULL UNIQUE,
                label TEXT NOT NULL DEFAULT '',
                email TEXT NOT NULL DEFAULT '',
                target REAL,
                currency TEXT NOT NULL DEFAULT 'EUR',
                active INTEGER NOT NULL DEFAULT 1,
                last_price REAL,
                last_status TEXT NOT NULL DEFAULT 'new',
                last_checked TEXT,
                fail_count INTEGER NOT NULL DEFAULT 0,
                interval_min INTEGER NOT NULL DEFAULT 60,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS prices (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                watch_id INTEGER NOT NULL,
                price REAL NOT NULL,
                currency TEXT NOT NULL,
                method TEXT NOT NULL,
                checked_at TEXT NOT NULL,
                FOREIGN KEY (watch_id) REFERENCES watches(id) ON DELETE CASCADE
            );
            CREATE INDEX IF NOT EXISTS idx_prices_watch ON prices(watch_id, checked_at);
            """
        )
        # Migration : bases créées avant l'ajout de l'intervalle par formule.
        columns = {row["name"] for row in c.execute("PRAGMA table_info(watches)")}
        if "interval_min" not in columns:
            c.execute("ALTER TABLE watches ADD COLUMN interval_min INTEGER NOT NULL DEFAULT 60")
    print(f"base prête : {DB_PATH}")


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# --------------------------------------------------------------------------
# Extraction du prix
# --------------------------------------------------------------------------
def fetch(url):
    req = Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "fr-FR,fr;q=0.9,en;q=0.7",
            "Cache-Control": "no-cache",
        },
    )
    try:
        resp = urlopen(req, timeout=30)
    except HTTPError as exc:
        return None, f"HTTP {exc.code}"
    except (URLError, TimeoutError, OSError) as exc:
        return None, f"réseau: {type(exc).__name__}"

    raw = resp.read()
    if resp.headers.get("Content-Encoding") == "gzip":
        try:
            raw = gzip.decompress(io.BytesIO(raw).getvalue())
        except OSError:
            pass
    return raw.decode("utf-8", "ignore"), "200"


def to_float(value):
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return round(float(value), 2)
    text = str(value).strip().replace("\u00a0", "").replace(" ", "")
    text = re.sub(r"[^\d,.\-]", "", text)
    if not text:
        return None
    if "," in text and "." in text:
        if text.rfind(",") > text.rfind("."):
            text = text.replace(".", "").replace(",", ".")
        else:
            text = text.replace(",", "")
    elif "," in text:
        text = text.replace(",", ".")
    try:
        return round(float(text), 2)
    except ValueError:
        return None


def walk_jsonld(node, out):
    if isinstance(node, dict):
        for key, value in node.items():
            if key in ("price", "lowPrice", "highPrice") and not isinstance(value, (dict, list)):
                out.append((to_float(value), node.get("priceCurrency")))
            else:
                walk_jsonld(value, out)
    elif isinstance(node, list):
        for item in node:
            walk_jsonld(item, out)


def _walk_prices(node):
    """Parcourt un objet extruct et rend les couples (prix, devise)."""
    if isinstance(node, dict):
        for key, value in node.items():
            if key in ("price", "lowPrice", "highPrice") and not isinstance(value, (dict, list)):
                yield to_float(value), node.get("priceCurrency")
            else:
                yield from _walk_prices(value)
    elif isinstance(node, list):
        for item in node:
            yield from _walk_prices(item)


def _from_extruct(html):
    """Couche open source : extruct (JSON-LD, microdata, OpenGraph).

    Retourne (prix, devise, méthode) ou None si la bibliothèque est absente
    ou ne trouve rien. Le moteur reste utilisable sans elle.
    """
    try:
        import extruct
    except ImportError:
        return None
    try:
        data = extruct.extract(
            html,
            syntaxes=["json-ld", "microdata", "opengraph"],
            uniform=True,
            errors="ignore",
        )
    except Exception:  # noqa: BLE001
        return None

    for key in ("json-ld", "microdata"):
        for item in data.get(key) or []:
            for price, currency in _walk_prices(item):
                if price and price > 0:
                    return price, currency or "EUR", key
    for item in data.get("opengraph") or []:
        props = item.get("properties") or {}
        for key in ("product:price:amount", "og:price:amount", "price:amount"):
            if key not in props:
                continue
            raw = props[key]
            value = to_float(raw[0] if isinstance(raw, list) and raw else raw)
            if value and value > 0:
                return value, "EUR", "opengraph"
    return None


def extract_price(html):
    """Renvoie (prix, devise, méthode) ou (None, None, motif)."""
    lowered = html.lower()
    if len(html) < 2500 and any(marker in lowered for marker in BLOCK_MARKERS):
        return None, None, "page_bloquee"

    structured = _from_extruct(html)
    if structured:
        return structured

    candidates = []

    for block in re.findall(
        r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
        html,
        re.S | re.I,
    ):
        payload = block.strip()
        if not payload:
            continue
        try:
            data = json.loads(payload)
        except json.JSONDecodeError:
            continue
        found = []
        walk_jsonld(data, found)
        for price, currency in found:
            if price and price > 0:
                candidates.append((price, currency or "EUR", "json-ld"))

    meta_patterns = [
        r'property=["\']product:price:amount["\'][^>]*content=["\']([^"\']+)',
        r'property=["\']og:price:amount["\'][^>]*content=["\']([^"\']+)',
        r'itemprop=["\']price["\'][^>]*content=["\']([^"\']+)',
        r'content=["\']([^"\']+)["\'][^>]*property=["\']product:price:amount["\']',
    ]
    for pattern in meta_patterns:
        for match in re.findall(pattern, html, re.I):
            value = to_float(match)
            if value and value > 0:
                candidates.append((value, "EUR", "meta"))

    if not candidates:
        # Dernier recours : un montant en euros répété dans le corps.
        # Une valeur unique est ignorée : sur une page de comparatif ou une 404,
        # elle vient du contenu éditorial, pas du produit.
        body = re.sub(r"<script.*?</script>|<style.*?</style>", " ", html, flags=re.S | re.I)
        body = re.sub(r"<[^>]+>", " ", body)
        counts = {}
        price_re = re.compile(r"(?<![\d\-–—])(\d{1,6}(?:[ .]\d{3})*[,.]\d{2})\s*(?:€|EUR)")
        for match in price_re.finditer(body):
            prefix = body[max(0, match.start() - 16):match.start()]
            # « 39,95 – 157,95 € » : la valeur suit une fourchette, pas un prix produit.
            if re.search(r"\d\s*[–—-]\s*$", prefix):
                continue
            value = to_float(match.group(1))
            if value and 0 < value < 100000:
                counts[value] = counts.get(value, 0) + 1
        repeated = [(count, value) for value, count in counts.items() if count >= 2]
        if repeated:
            repeated.sort(reverse=True)
            candidates.append((repeated[0][1], "EUR", "texte"))

    if not candidates:
        return None, None, "prix_introuvable"

    # Privilégier les sources structurées, puis la valeur la plus fréquente.
    candidates.sort(key=lambda c: {"json-ld": 0, "meta": 1, "texte": 2}[c[2]])
    best = candidates[0]
    return best[0], best[1], best[2]


# --------------------------------------------------------------------------
# Notifications
# --------------------------------------------------------------------------
def send_email(to_addr, subject, body, html=None):
    """Envoie un email si un mot de passe d'application est configuré.

    Variables : SMTP_HOST (def. smtp.gmail.com), SMTP_PORT (def. 587),
    SMTP_USER, SMTP_PASS. Sans ces deux dernières, la fonction est inerte.
    """
    user = os.environ.get("SMTP_USER", "")
    password = os.environ.get("SMTP_PASS", "")
    candidates = [
        os.path.join(HOME, ".hermes", ".env"),
        os.path.join(HOME, "secrets", "smtp.env"),
        os.path.join(HOME, "obsidian", "Second Cerveau", "wiki", "SMTP — identifiants.md"),
    ]
    if not (user and password):
        for path in candidates:
            if not os.path.exists(path):
                continue
            for raw in open(path, encoding="utf-8", errors="ignore"):
                line = raw.strip().lstrip("#").strip()
                if line.startswith("SMTP_USER"):
                    user = line.split(":", 1)[1].strip() if ":" in line else line.split("=", 1)[-1].strip()
                    user = user.strip().strip('"').strip("'").strip("`")
                elif line.startswith("SMTP_PASS"):
                    password = line.split(":", 1)[1].strip() if ":" in line else line.split("=", 1)[-1].strip()
                    password = password.strip().strip('"').strip("'").strip("`")
            if user and password:
                break
    if user:
        user = user.replace(" ", "")
    if password:
        password = password.replace(" ", "")
    if not user or not password:
        return False

    import smtplib
    from email.message import EmailMessage

    message = EmailMessage()
    message["From"] = f"VeillePrix <{user}>"
    message["To"] = to_addr
    message["Subject"] = subject
    message.set_content(body)
    if html:
        message.add_alternative(html, subtype="html")
    host = os.environ.get("SMTP_HOST", "smtp.gmail.com")
    port = int(os.environ.get("SMTP_PORT", "587"))
    try:
        with smtplib.SMTP(host, port, timeout=25) as server:
            server.starttls()
            server.login(user, password)
            server.send_message(message)
        return True
    except Exception as exc:  # noqa: BLE001
        print(f"  (email non envoyé: {exc})", file=sys.stderr)
        return False


PAPER = "#f7f5f0"
INK = "#14120f"
INK2 = "#5c5952"
RULE = "#ddd8ce"
ACCENT = "#c2410c"


def _esc(text):
    return (
        str(text)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def email_shell(preheader, body):
    """Enveloppe HTML sobre, compatible clients mail (tables + styles inline)."""
    return f"""<!DOCTYPE html>
<html lang="fr"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>VeillePrix</title></head>
<body style="margin:0;padding:0;background:{PAPER};">
<div style="display:none;max-height:0;overflow:hidden;opacity:0;">{_esc(preheader)}</div>
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:{PAPER};padding:32px 16px;">
<tr><td align="center">
<table role="presentation" width="560" cellpadding="0" cellspacing="0" style="width:560px;max-width:100%;background:#fffefb;border:1px solid {RULE};">
  <tr><td style="padding:20px 28px;border-bottom:1px solid {INK};">
    <span style="font-family:Georgia,'Times New Roman',serif;font-size:19px;letter-spacing:-.3px;color:{INK};">Veille<b>Prix</b></span>
    <span style="font-family:'Courier New',monospace;font-size:9px;letter-spacing:2px;color:{ACCENT};vertical-align:super;">FR</span>
  </td></tr>
  <tr><td style="padding:32px 28px 8px;">{body}</td></tr>
  <tr><td style="padding:20px 28px 28px;border-top:1px solid {RULE};">
    <p style="margin:0;font-family:Helvetica,Arial,sans-serif;font-size:11px;line-height:1.6;color:{INK2};">
      Vous recevez ce message parce qu'une veille de prix a été ouverte avec cette adresse.<br>
      Répondez <b>STOP</b> pour arrêter les alertes. VeillePrix · Titouan Marty · veilleprix.pages.dev
    </p>
  </td></tr>
</table>
</td></tr></table></body></html>"""


def _price_row(label, value, strong=False, color=INK):
    weight = "700" if strong else "400"
    return (
        f'<tr><td style="padding:9px 0;border-bottom:1px solid {RULE};font-family:Helvetica,Arial,sans-serif;'
        f'font-size:13px;color:{INK2};">{_esc(label)}</td>'
        f'<td align="right" style="padding:9px 0;border-bottom:1px solid {RULE};'
        f"font-family:'Courier New',monospace;font-size:15px;font-weight:{weight};color:{color};\">{_esc(value)}</td></tr>"
    )


def email_activation(watch, price, currency):
    subject = f"Votre veille est active — {watch['label'] or 'votre produit'}"
    text = (
        f"Votre veille est active.\n\n{watch['label'] or watch['url']}\n"
        f"Premier relevé : {fmt(price, currency)}\n{watch['url']}\n\n"
        f"Relevé toutes les heures. Un message à chaque variation.\n"
    )
    body = f"""
    <p style="margin:0 0 10px;font-family:'Courier New',monospace;font-size:10px;letter-spacing:2px;color:{ACCENT};">VEILLE OUVERTE</p>
    <h1 style="margin:0 0 14px;font-family:Georgia,serif;font-weight:400;font-size:28px;line-height:1.15;color:{INK};">Votre registre est ouvert.</h1>
    <p style="margin:0 0 22px;font-family:Helvetica,Arial,sans-serif;font-size:14px;line-height:1.6;color:{INK2};">
      {_esc(watch['label'] or watch['url'])} est désormais relevé toutes les heures. Voici le point de départ.
    </p>
    <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="margin-bottom:22px;">
      {_price_row('Premier relevé', fmt(price, currency), True, ACCENT)}
      {_price_row('Fréquence', 'toutes les heures')}
    </table>
    <p style="margin:0 0 18px;font-family:Helvetica,Arial,sans-serif;font-size:13px;line-height:1.6;color:{INK2};">
      Vous recevrez un email à chaque variation, et un bordereau chaque matin.
    </p>
    <a href="{_esc(watch['url'])}" style="display:inline-block;padding:12px 22px;background:{INK};color:{PAPER};font-family:'Courier New',monospace;font-size:11px;letter-spacing:1px;text-decoration:none;">VOIR LA PAGE</a>
    """
    return subject, text, email_shell(f"{fmt(price, currency)} — veille active", body)


def email_variation(watch, previous, price, currency):
    delta = price - previous
    pct = (delta / previous * 100) if previous else 0
    arrow = "▼" if delta < 0 else "▲"
    mouvement = "baisse" if delta < 0 else "hausse"
    name = watch["label"] or "Votre produit"
    subject = f"{arrow} {name} : {fmt(previous, currency)} → {fmt(price, currency)}"
    text = (
        f"{name}\n{mouvement.capitalize()} de {abs(pct):.1f} %\n"
        f"{fmt(previous, currency)} → {fmt(price, currency)}\n{watch['url']}\n"
    )
    target_line = ""
    if watch["target"] is not None and price <= watch["target"]:
        target_line = (
            f'<p style="margin:0 0 18px;padding:10px 14px;border-left:2px solid {ACCENT};'
            f'background:#fbeee7;font-family:Helvetica,Arial,sans-serif;font-size:13px;color:#7c2d12;">'
            f"Objectif atteint : le prix passe sous {fmt(watch['target'], currency)}.</p>"
        )
    body = f"""
    <p style="margin:0 0 10px;font-family:'Courier New',monospace;font-size:10px;letter-spacing:2px;color:{ACCENT};">{_esc(mouvement.upper())} DÉTECTÉE</p>
    <h1 style="margin:0 0 18px;font-family:Georgia,serif;font-weight:400;font-size:28px;line-height:1.15;color:{INK};">{_esc(name)}</h1>
    <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="margin-bottom:20px;">
      <tr>
        <td style="padding:0 12px 0 0;font-family:'Courier New',monospace;font-size:22px;color:{INK2};text-decoration:line-through;">{_esc(fmt(previous, currency))}</td>
        <td style="padding:0 12px;font-family:'Courier New',monospace;font-size:18px;color:{INK2};">→</td>
        <td style="font-family:'Courier New',monospace;font-size:26px;font-weight:700;color:{ACCENT};">{_esc(fmt(price, currency))}</td>
        <td align="right" style="font-family:'Courier New',monospace;font-size:14px;color:{ACCENT};">{arrow} {abs(pct):.1f} %</td>
      </tr>
    </table>
    <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="margin-bottom:20px;">
      {_price_row('Ancien prix', fmt(previous, currency))}
      {_price_row('Nouveau prix', fmt(price, currency), True, ACCENT)}
      {_price_row('Écart', f"{'+' if delta>0 else '−'}{fmt(abs(delta), currency)}")}
    </table>
    {target_line}
    <a href="{_esc(watch['url'])}" style="display:inline-block;padding:12px 22px;background:{ACCENT};color:#ffffff;font-family:'Courier New',monospace;font-size:11px;letter-spacing:1px;text-decoration:none;">OUVRIR LA PAGE</a>
    """
    return subject, text, email_shell(f"{fmt(previous, currency)} → {fmt(price, currency)}", body)


def email_failure(watch, reason):
    name = watch["label"] or watch["url"]
    subject = f"Relevé impossible — {name[:40]}"
    text = f"Le prix n'a pas pu être relevé : {reason}.\n{watch['url']}\n"
    body = f"""
    <p style="margin:0 0 10px;font-family:'Courier New',monospace;font-size:10px;letter-spacing:2px;color:{ACCENT};">RELEVÉ INTERROMPU</p>
    <h1 style="margin:0 0 14px;font-family:Georgia,serif;font-weight:400;font-size:24px;color:{INK};">La page n'a pas répondu.</h1>
    <p style="margin:0 0 16px;font-family:Helvetica,Arial,sans-serif;font-size:14px;line-height:1.6;color:{INK2};">
      {_esc(name)} n'a pas pu être relevé trois fois de suite ({_esc(reason)}).
      La page est peut-être protégée, hors ligne ou son adresse a changé.
    </p>
    <p style="margin:0 0 18px;font-family:Helvetica,Arial,sans-serif;font-size:13px;color:{INK2};">
      Répondez à ce message avec l'adresse à jour, ou dites simplement <b>STOP</b> pour arrêter.
    </p>
    <a href="{_esc(watch['url'])}" style="display:inline-block;padding:12px 22px;background:{INK};color:{PAPER};font-family:'Courier New',monospace;font-size:11px;letter-spacing:1px;text-decoration:none;">VOIR LA PAGE</a>
    """
    return subject, text, email_shell("Relevé interrompu", body)


def _row_price(row):
    try:
        value = row["price"]
    except (IndexError, KeyError):
        value = row["last_price"]
    return value


def email_client_digest(email_addr, items):
    """items : liste de dicts {label, url, price, currency, low, low_at, high, low7}."""
    if not items:
        return None
    subject = "Votre bordereau VeillePrix du matin"
    lines = ["Votre bordereau du matin", ""]
    for it in items:
        line = f"{it['label']} — {fmt(it['price'], it['currency'])}"
        if it.get("low") is not None and it["low"] < it["price"] - 0.01:
            line += f" (plus bas sur 24 h : {fmt(it['low'], it['currency'])} à {it['low_at']})"
        lines.append(line)
    text = "\n".join(lines) + "\n"

    def cell(value, strong=False, color=INK2):
        weight = "700" if strong else "400"
        return (
            f'<td align="right" style="padding:9px 0 9px 10px;border-bottom:1px solid {RULE};'
            f"font-family:'Courier New',monospace;font-size:13px;font-weight:{weight};color:{color};\">{_esc(value)}</td>"
        )

    header = (
        f'<tr>'
        f'<td style="padding:8px 0;border-bottom:1px solid {INK};font-family:\'Courier New\',monospace;'
        f'font-size:9px;letter-spacing:1.5px;color:{INK2};">PRODUIT</td>'
        + "".join(
            f'<td align="right" style="padding:8px 0 8px 10px;border-bottom:1px solid {INK};'
            f"font-family:'Courier New',monospace;font-size:9px;letter-spacing:1.5px;color:{INK2};\">{h}</td>"
            for h in ("ACTUEL", "MIN 24 H", "MIN 7 J")
        )
        + "</tr>"
    )

    body_rows = ""
    for it in items:
        low = it.get("low")
        low7 = it.get("low7")
        name_td = (
            f'<td style="padding:9px 0;border-bottom:1px solid {RULE};font-family:Helvetica,Arial,sans-serif;'
            f'font-size:13px;color:{INK};">{_esc(it["label"])}</td>'
        )
        body_rows += (
            "<tr>"
            + name_td
            + cell(fmt(it["price"], it["currency"]), True, INK)
            + cell(fmt(low, it["currency"]) if low is not None else "—", low is not None and low < it["price"] - 0.01, ACCENT)
            + cell(fmt(low7, it["currency"]) if low7 is not None else "—")
            + "</tr>"
        )

    better = [it for it in items if it.get("low") is not None and it["low"] < it["price"] - 0.01]
    alert_html = ""
    if better:
        alert_html = (
            f'<p style="margin:0 0 18px;padding:10px 14px;border-left:2px solid {ACCENT};background:#fbeee7;'
            f'font-family:Helvetica,Arial,sans-serif;font-size:13px;line-height:1.55;color:#7c2d12;">'
            + "<br>".join(
                f"{_esc(it['label'])} est passé sous son prix actuel : {fmt(it['low'], it['currency'])} à {_esc(it['low_at'])}."
                for it in better
            )
            + "</p>"
        )

    body = f"""
    <p style="margin:0 0 10px;font-family:'Courier New',monospace;font-size:10px;letter-spacing:2px;color:{ACCENT};">BORDEREAU DU MATIN</p>
    <h1 style="margin:0 0 18px;font-family:Georgia,serif;font-weight:400;font-size:26px;color:{INK};">Les prix de vos concurrents.</h1>
    {alert_html}
    <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="margin-bottom:18px;">{header}{body_rows}</table>
    <p style="margin:0;font-family:Helvetica,Arial,sans-serif;font-size:12px;line-height:1.6;color:{INK2};">
      Le minimum sur 24 h capte les baisses passagères, même si le prix est revenu depuis.
      Une alerte part de toute façon dès qu'une variation est détectée.
    </p>
    """
    return subject, text, email_shell("Bordereau du matin", body)


def notify(message):
    if not os.path.exists(NOTIFY):
        print("  (notify-discord absent, alerte non envoyée)", file=sys.stderr)
        return False
    try:
        subprocess.run([NOTIFY], input=message.encode("utf-8"), check=True, timeout=30)
        return True
    except Exception as exc:  # noqa: BLE001
        print(f"  (échec notification: {exc})", file=sys.stderr)
        return False


def fmt(price, currency="EUR"):
    symbol = "€" if currency in ("EUR", None) else currency
    return f"{price:,.2f}".replace(",", " ").replace(".", ",") + f" {symbol}"


# --------------------------------------------------------------------------
# Commandes
# --------------------------------------------------------------------------
def add(url, label="", email="", target=None, interval_min=60):
    with db() as c:
        try:
            cur = c.execute(
                "INSERT INTO watches (url, label, email, target, interval_min, created_at) VALUES (?,?,?,?,?,?)",
                (url, label, email, target, interval_min, now_iso()),
            )
        except sqlite3.IntegrityError:
            row = c.execute("SELECT id FROM watches WHERE url=?", (url,)).fetchone()
            print(f"déjà suivie (id {row['id']}) : {url}")
            return row["id"]
        print(f"ajoutée id={cur.lastrowid} : {url} (relevé toutes les {interval_min} min)")
        return cur.lastrowid


def remove(watch_id):
    with db() as c:
        c.execute("DELETE FROM watches WHERE id=?", (watch_id,))
    print(f"supprimée : {watch_id}")


def listing():
    with db() as c:
        rows = c.execute("SELECT * FROM watches ORDER BY id").fetchall()
    if not rows:
        print("aucune adresse suivie")
        return
    print(f"{'id':>3}  {'état':<10} {'dernier':>12}  {'nom':<22} url")
    for row in rows:
        price = fmt(row["last_price"], row["currency"]) if row["last_price"] is not None else "—"
        label = (row["label"] or "—")[:22]
        flag = "" if row["active"] else " (inactive)"
        print(f"{row['id']:>3}  {row['last_status'][:10]:<10} {price:>12}  {label:<22} {row['url']}{flag}")


def _is_due(row):
    """Vrai si le délai propre à ce suivi est écoulé."""
    interval = row["interval_min"] or 60
    last = row["last_checked"]
    if not last:
        return True
    try:
        stamp = datetime.fromisoformat(last)
    except ValueError:
        return True
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) - stamp >= timedelta(minutes=interval)


def _note_failure(conn, row, reason):
    """Enregistre un échec et prévient le client à son troisième échec consécutif."""
    fails = row["fail_count"] + 1
    conn.execute(
        "UPDATE watches SET last_status=?, last_checked=?, fail_count=? WHERE id=?",
        (reason, now_iso(), fails, row["id"]),
    )
    conn.commit()
    if fails == 3 and row["email"]:
        try:
            subject, text, html = email_failure(row, reason)
            send_email(row["email"], subject, text, html)
        except Exception as exc:  # noqa: BLE001
            print(f"  (email d'échec non envoyé: {exc})", file=sys.stderr)
        notify(
            f"**Relevé interrompu — {row['label'] or row['url']}**\n"
            f"3 échecs consécutifs ({reason})\n{row['url']}"
        )


def check(watch_id=None, quiet=False):
    conn = db()
    query = "SELECT * FROM watches WHERE active=1"
    params = ()
    if watch_id:
        query += " AND id=?"
        params = (watch_id,)
    rows = conn.execute(query, params).fetchall()
    rows = [row for row in rows if _is_due(row)]
    if not rows:
        if not quiet:
            print("aucune adresse à relever maintenant")
        return 0

    changes = 0
    errors = []
    for row in rows:
        html, status = fetch(row["url"])
        if html is None:
            _note_failure(conn, row, status)
            errors.append((row, status))
            continue

        price, currency, method = extract_price(html)
        if price is None:
            _note_failure(conn, row, method)
            errors.append((row, method))
            continue

        conn.execute(
            "INSERT INTO prices (watch_id, price, currency, method, checked_at) VALUES (?,?,?,?,?)",
            (row["id"], price, currency, method, now_iso()),
        )

        previous = row["last_price"]
        if previous is None:
            conn.execute(
                "UPDATE watches SET last_price=?, currency=?, last_status='ok', last_checked=?, fail_count=0 WHERE id=?",
                (price, currency, now_iso(), row["id"]),
            )
            conn.commit()
            if not quiet:
                print(f"  #{row['id']} premier relevé : {fmt(price, currency)} ({method})")
            if row["email"]:
                subject, text, html = email_activation(row, price, currency)
                send_email(row["email"], subject, text, html)
            continue

        conn.execute(
            "UPDATE watches SET last_price=?, currency=?, last_status='ok', last_checked=?, fail_count=0 WHERE id=?",
            (price, currency, now_iso(), row["id"]),
        )
        conn.commit()

        if abs(price - previous) >= 0.01:
            changes += 1
            delta = price - previous
            pct = (delta / previous * 100) if previous else 0
            arrow = "▼" if delta < 0 else "▲"
            name = row["label"] or row["url"]
            message = (
                f"**Variation de prix — {name}**\n"
                f"{fmt(previous, currency)} → {fmt(price, currency)}  "
                f"({arrow} {abs(pct):.1f} %)\n{row['url']}"
            )
            if row["target"] is not None and price <= row["target"] and previous > row["target"]:
                message += f"\n**Objectif atteint** : sous {fmt(row['target'], currency)}"
            notify(message)
            if row["email"]:
                subject, text, html = email_variation(row, previous, price, currency)
                send_email(row["email"], subject, text, html)
            if not quiet:
                print(f"  #{row['id']} {fmt(previous, currency)} → {fmt(price, currency)} ({arrow} {abs(pct):.1f} %)")
        elif not quiet:
            print(f"  #{row['id']} stable : {fmt(price, currency)}")

    if errors and not quiet:
        print("\nÉchecs :")
        for row, reason in errors:
            print(f"  #{row['id']} {reason} — {row['url']}")

    if changes and not quiet:
        print(f"\n{changes} variation(s), alerte(s) envoyée(s).")
    return 0


def digest():
    conn = db()
    since = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat(timespec="seconds")
    rows = conn.execute(
        """
        SELECT w.id, w.label, w.url, w.currency, p.price, p.checked_at
        FROM prices p JOIN watches w ON w.id = p.watch_id
        WHERE p.checked_at >= ?
        ORDER BY w.id, p.checked_at
        """,
        (since,),
    ).fetchall()
    watches = conn.execute("SELECT * FROM watches WHERE active=1").fetchall()

    lines = ["**Bordereau VeillePrix — dernières 24 h**"]
    if not rows:
        lines.append("Aucun relevé enregistré sur la période.")
    else:
        current = None
        for row in rows:
            if row["id"] != current:
                current = row["id"]
                lines.append(f"\n**{row['label'] or row['url']}**")
            lines.append(f"· {fmt(row['price'], row['currency'])} — {row['checked_at'][11:16]} UTC")

    moved = conn.execute(
        """
        SELECT w.id, w.label, w.url, w.currency,
               (SELECT price FROM prices WHERE watch_id=w.id ORDER BY checked_at LIMIT 1) AS first,
               (SELECT price FROM prices WHERE watch_id=w.id ORDER BY checked_at DESC LIMIT 1) AS last
        FROM watches w WHERE w.active=1
        """
    ).fetchall()
    variations = [r for r in moved if r["first"] is not None and r["last"] is not None and abs(r["last"] - r["first"]) >= 0.01]
    lines.append(f"\n{len(watches)} adresse(s) suivie(s) · {len(variations)} variation(s) sur la période.")
    for row in variations:
        delta = row["last"] - row["first"]
        arrow = "▼" if delta < 0 else "▲"
        lines.append(f"{arrow} {row['label'] or row['url']} : {fmt(row['first'], row['currency'])} → {fmt(row['last'], row['currency'])}")

    notify("\n".join(lines))
    print("\n".join(lines))

    day_ago = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat(timespec="seconds")
    week_ago = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat(timespec="seconds")

    clients = {}
    for row in conn.execute(
        "SELECT * FROM watches WHERE active=1 AND email<>'' AND last_price IS NOT NULL ORDER BY id"
    ):
        lowest = conn.execute(
            "SELECT price, checked_at FROM prices WHERE watch_id=? AND checked_at>=? "
            "ORDER BY price ASC, checked_at ASC LIMIT 1",
            (row["id"], day_ago),
        ).fetchone()
        low7 = conn.execute(
            "SELECT MIN(price) AS p FROM prices WHERE watch_id=? AND checked_at>=?",
            (row["id"], week_ago),
        ).fetchone()
        clients.setdefault(row["email"], []).append(
            {
                "label": row["label"] or row["url"],
                "url": row["url"],
                "price": row["last_price"],
                "currency": row["currency"] or "EUR",
                "low": lowest["price"] if lowest else None,
                "low_at": (lowest["checked_at"][11:16] + " UTC") if lowest else "",
                "low7": low7["p"] if low7 else None,
            }
        )
    for addr, client_items in clients.items():
        payload = email_client_digest(addr, client_items)
        if not payload:
            continue
        subject, text, html = payload
        if send_email(addr, subject, text, html):
            print(f"  bordereau envoyé à {addr} ({len(client_items)} adresse(s))")


def _discord():
    token = os.environ.get("DISCORD_BOT_TOKEN", "")
    if not token:
        env_path = os.path.join(HOME, ".hermes", ".env")
        if os.path.exists(env_path):
            for line in open(env_path, encoding="utf-8"):
                if line.startswith("DISCORD_BOT_TOKEN="):
                    token = line.split("=", 1)[1].strip().strip('"').strip("'")
                    break
    channel = os.environ.get("VEILLEPRIX_LEAD_CHANNEL", "1569423883147284510")
    return token, channel


STATE_PATH = os.path.join(os.path.dirname(DB_PATH), "ingest.state")


def ingest():
    """Crée un suivi pour chaque demande reçue depuis le formulaire du site."""
    token, channel = _discord()
    if not token:
        print("jeton Discord introuvable", file=sys.stderr)
        return 1

    last_id = "0"
    if os.path.exists(STATE_PATH):
        last_id = open(STATE_PATH, encoding="utf-8").read().strip() or "0"
    if not last_id.isdigit():
        last_id = "0"

    # Discord renvoie les messages les plus anciens quand `after=0` :
    # au premier passage on lit simplement les derniers messages.
    if last_id == "0":
        url = f"https://discord.com/api/v10/channels/{channel}/messages?limit=20"
    else:
        url = f"https://discord.com/api/v10/channels/{channel}/messages?limit=50&after={last_id}"
    req = Request(url, headers={"Authorization": f"Bot {token}", "User-Agent": "DiscordBot (veilleprix,1.0)"})
    try:
        messages = json.load(urlopen(req, timeout=30))
    except Exception as exc:  # noqa: BLE001
        print(f"lecture Discord impossible : {exc}", file=sys.stderr)
        return 1

    created = 0
    highest = last_id
    for message in sorted(messages, key=lambda m: int(m["id"])):
        highest = str(max(int(highest), int(message["id"])))
        content = message.get("content", "")
        if "Nouvelle demande de registre" not in content:
            continue
        fields = {}
        for line in content.splitlines():
            for key, label in (("name", "Nom"), ("email", "Email"), ("url", "Adresse")):
                if line.startswith(f"{label} :"):
                    fields[key] = line.split(":", 1)[1].strip()
        if not fields.get("url"):
            continue
        watch_id = add(fields["url"], fields.get("name", ""), fields.get("email", ""), None)
        created += 1
        print(f"  demande traitée → suivi #{watch_id} ({fields.get('email', 'sans email')})")

    with open(STATE_PATH, "w", encoding="utf-8") as fh:
        fh.write(highest)

    print(f"{created} nouvelle(s) demande(s) transformée(s) en suivi.")
    return 0


def report(watch_id):
    conn = db()
    row = conn.execute("SELECT * FROM watches WHERE id=?", (watch_id,)).fetchone()
    if not row:
        print("adresse inconnue")
        return
    print(f"#{row['id']} {row['label'] or row['url']}\n{row['url']}\n")
    history = conn.execute(
        "SELECT price, currency, method, checked_at FROM prices WHERE watch_id=? ORDER BY checked_at",
        (watch_id,),
    ).fetchall()
    if not history:
        print("aucun relevé")
        return
    for item in history:
        print(f"  {item['checked_at'][:16].replace('T', ' ')}  {fmt(item['price'], item['currency']):>12}  ({item['method']})")
    prices = [item["price"] for item in history]
    print(f"\nmin {fmt(min(prices))} · max {fmt(max(prices))} · {len(prices)} relevés")


def test(url):
    html, status = fetch(url)
    if html is None:
        print(f"échec : {status}")
        return
    price, currency, method = extract_price(html)
    if price is None:
        print(f"page {status}, {len(html)} octets — {method}")
        return
    print(f"page {status}, {len(html)} octets — prix {fmt(price, currency)} via {method}")


def main():
    parser = argparse.ArgumentParser(prog="veilleprix", description="Veille tarifaire — moteur local")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init")
    sub.add_parser("list")
    sub.add_parser("check", help="relève tous les prix")
    sub.add_parser("digest", help="bordereau des dernières 24 h")
    sub.add_parser("ingest", help="transforme les demandes du site en suivis")

    p_add = sub.add_parser("add")
    p_add.add_argument("url")
    p_add.add_argument("--label", default="")
    p_add.add_argument("--email", default="")
    p_add.add_argument("--target", type=float, default=None)
    p_add.add_argument("--interval", type=int, default=60, help="minutes entre deux relevés")

    p_rm = sub.add_parser("remove")
    p_rm.add_argument("id", type=int)

    p_rep = sub.add_parser("report")
    p_rep.add_argument("id", type=int)

    p_test = sub.add_parser("test")
    p_test.add_argument("url")

    args = parser.parse_args()
    if args.cmd == "init":
        init()
    elif args.cmd == "add":
        init()
        add(args.url, args.label, args.email, args.target, args.interval)
    elif args.cmd == "remove":
        remove(args.id)
    elif args.cmd == "list":
        listing()
    elif args.cmd == "check":
        check(quiet=False)
    elif args.cmd == "digest":
        digest()
    elif args.cmd == "ingest":
        init()
        sys.exit(ingest())
    elif args.cmd == "report":
        report(args.id)
    elif args.cmd == "test":
        test(args.url)


if __name__ == "__main__":
    main()
