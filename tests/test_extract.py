"""Tests du moteur VeillePrix — extraction du prix, sans réseau.

Lancer : .venv/bin/pytest -q
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import veilleprix as vp  # noqa: E402

JSONLD_PAGE = """
<html><head><title>Produit</title></head><body>
<script type="application/ld+json">
{"@context":"https://schema.org","@type":"Product","name":"Beelink EQ7",
 "offers":{"@type":"Offer","price":"429.99","priceCurrency":"EUR","availability":"InStock"}}
</script>
<p>Une page produit ordinaire.</p>
</body></html>
"""

JSONLD_COMMA = """
<html><body>
<script type="application/ld+json">
{"@type":"Product","offers":{"@type":"AggregateOffer","lowPrice":"399,90","priceCurrency":"EUR"}}
</script></body></html>
"""

OPENGRAF_PAGE = """
<html><head>
<meta property="og:type" content="product">
<meta property="product:price:amount" content="19.90">
<meta property="product:price:currency" content="EUR">
</head><body><h1>Article</h1></body></html>
"""

MICRODATA_PAGE = """
<html><body itemscope itemtype="https://schema.org/Product">
<span itemprop="name">Chose</span>
<div itemprop="offers" itemscope itemtype="https://schema.org/Offer">
  <meta itemprop="price" content="12.50">
  <meta itemprop="priceCurrency" content="EUR">
</div></body></html>
"""

BLOCKED_PAGE = (
    "<html><body><h1>Just a moment...</h1>"
    "<p>Checking your browser before accessing. captcha</p></body></html>"
)

NO_PRICE_PAGE = """
<html><body><h1>Notre mission</h1>
<p>Nous vendons depuis 1932. Une équipe de 40 personnes.</p></body></html>
"""

RANGE_PAGE = """
<html><body><h1>Tarifs</h1>
<table><tr><td>Starter</td><td>39,95 €</td></tr>
<tr><td>Business</td><td>157,95 €</td></tr></table>
<p>Grille indicative : 39,95 – 157,95 € selon la formule.</p></body></html>
"""

REPEATED_PAGE = """
<html><body>
<h1>Chaussure</h1>
<div class="price">89,90 €</div>
<span>Ajouter au panier — 89,90 €</span>
</body></html>
"""


def test_jsonld():
    price, currency, method = vp.extract_price(JSONLD_PAGE)
    assert price == 429.99
    assert currency == "EUR"
    assert method in ("json-ld", "json-ld")


def test_jsonld_comma_lowprice():
    price, currency, _ = vp.extract_price(JSONLD_COMMA)
    assert price == 399.90
    assert currency == "EUR"


def test_opengraph():
    price, _, method = vp.extract_price(OPENGRAF_PAGE)
    assert price == 19.90
    assert method in ("opengraph", "meta")


def test_microdata():
    price, _, method = vp.extract_price(MICRODATA_PAGE)
    assert price == 12.50


def test_blocked_page():
    price, _, reason = vp.extract_price(BLOCKED_PAGE)
    assert price is None
    assert reason == "page_bloquee"


def test_no_price():
    price, _, reason = vp.extract_price(NO_PRICE_PAGE)
    assert price is None
    assert reason == "prix_introuvable"


def test_range_is_not_a_price():
    """Une fourchette commerciale ne doit jamais passer pour un prix produit."""
    price, _, reason = vp.extract_price(RANGE_PAGE)
    assert price is None


def test_repeated_text_fallback():
    price, _, method = vp.extract_price(REPEATED_PAGE)
    assert price == 89.90
    assert method in ("texte", "microdata", "json-ld", "opengraph")


def test_to_float_variants():
    assert vp.to_float("429,99") == 429.99
    assert vp.to_float("1 299,00") == 1299.00
    assert vp.to_float("1,299.00") == 1299.00
    assert vp.to_float("42") == 42.0
    assert vp.to_float(None) is None


def test_fmt_french():
    assert vp.fmt(1299.9) == "1 299,90 €"


def test_is_due_respects_interval():
    from datetime import datetime, timedelta, timezone

    now = datetime.now(timezone.utc)
    recent = {"interval_min": 60, "last_checked": (now - timedelta(minutes=5)).isoformat()}
    old = {"interval_min": 60, "last_checked": (now - timedelta(minutes=90)).isoformat()}
    fresh = {"interval_min": 15, "last_checked": (now - timedelta(minutes=20)).isoformat()}
    never = {"interval_min": 60, "last_checked": None}

    assert vp._is_due(recent) is False
    assert vp._is_due(old) is True
    assert vp._is_due(fresh) is True
    assert vp._is_due(never) is True


def test_email_templates_render():
    watch = {
        "label": "Beelink EQ7",
        "url": "https://exemple.fr/p/1",
        "target": 430.0,
        "email": "client@exemple.fr",
        "currency": "EUR",
    }
    subject, text, html = vp.email_variation(watch, 449.99, 429.99, "EUR")
    assert "429,99" in subject
    assert "449,99" in text
    assert "BAISSE" in html
    assert "Objectif atteint" in html

    subject, text, html = vp.email_activation(watch, 429.99, "EUR")
    assert "429,99" in html
    assert "registre est ouvert" in html

    subject, text, html = vp.email_failure(watch, "HTTP 403")
    assert "HTTP 403" in html
