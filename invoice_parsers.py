"""
Lecture des factures fournisseurs PDF (import historique des achats).

Chaque parser renvoie :
{
    "format": "yenanh",
    "supplier": "Yen Anh",
    "invoice_number": "FA202609063",
    "invoice_date": "2026-09-10",
    "delivery_number": "BL202609103",
    "delivery_date": "2026-09-09",
    "total_ttc": 739.25,
    "lines": [
        {"code", "description", "quantity", "unit_price", "total", "tva", "unit_hint", "kind",
         "quantity_source"}   # "invoice" | "deduced" (montant ÷ P.U.) | "history" | "missing"
    ],
    "lines_total": 739.25,
    "warnings": [...]
}

Tous les prix sont ramenés en TTC (même convention que les achats saisis à la main) :
les factures HT (Metro, Emballage futé, Delidrinks) sont converties avec le taux de TVA de chaque ligne.
Les remises globales deviennent une ligne kind="discount" (code "REMISE").
"""

import io
import re
import unicodedata

import pdfplumber


def _num(value):
    """'1 234,56' / '-2,00' / '3.98' → float"""
    s = str(value).replace(" ", "").replace("€", "")
    if "," in s:
        s = s.replace(".", "").replace(",", ".")
    return float(s)


def _iso(date_fr):
    """'10/09/2026' ou '10-09-2026' → '2026-09-10'"""
    d, m, y = re.split(r"[/-]", date_fr)
    return f"{y}-{m}-{d}"


def _ttc(amount_ht, rate):
    return amount_ht * (1 + rate / 100)


def extract_text(pdf_bytes, max_char_size=None):
    """max_char_size : ignore les gros caractères (filigrane « Duplicata » Metro)."""
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        pages = []
        for page in pdf.pages:
            if max_char_size:
                page = page.filter(
                    lambda o: o.get("object_type") != "char" or o.get("size", 0) < max_char_size
                )
            pages.append(page.extract_text() or "")
        return "\n".join(pages)


def _empty(fmt, supplier):
    return {
        "format": fmt,
        "supplier": supplier,
        "invoice_number": "",
        "invoice_date": "",
        "delivery_number": "",
        "delivery_date": "",
        "total_ttc": None,
        "lines": [],
        "warnings": [],
    }


def _line(code, description, quantity, unit_price, total, tva=None, unit_hint="", kind="product",
          quantity_source="invoice"):
    return {
        "code": str(code),
        "description": re.sub(r"\s+", " ", description).strip(),
        "quantity": round(quantity, 3) if quantity is not None else None,
        "unit_price": round(unit_price, 4) if unit_price is not None else None,
        "total": round(total, 2),
        "tva": tva,
        "unit_hint": unit_hint,
        "kind": kind,
        "quantity_source": quantity_source,
    }


def _deduce_quantity(total, unit_price):
    """Colonne quantité vide : quantité = montant ÷ prix unitaire (None si impossible)."""
    if not unit_price:
        return None
    qty = total / unit_price
    # arrondi : entier si très proche, sinon 3 décimales (articles au kg)
    return float(round(qty)) if abs(qty - round(qty)) < 0.02 else round(qty, 3)


def _finalize(result):
    lines = result["lines"]
    result["lines_total"] = round(sum(l["total"] for l in lines), 2)

    if not result["invoice_number"]:
        result["warnings"].append("Numéro de facture introuvable")
    if not result["invoice_date"]:
        result["warnings"].append("Date de facture introuvable")
    if not lines:
        result["warnings"].append("Aucune ligne article trouvée")
    elif result["total_ttc"] is not None:
        # conversion HT → TTC ligne par ligne : quelques centimes d'arrondi possibles
        tolerance = max(0.05, 0.01 * len(lines))
        if abs(result["lines_total"] - result["total_ttc"]) > tolerance:
            result["warnings"].append(
                f"Total lignes {result['lines_total']:.2f} € ≠ total facture {result['total_ttc']:.2f} €"
            )

    return result


# =========================
# YEN ANH (logiciel EBP) — prix TTC
# =========================

# quantité / prix unitaires : sans séparateur de milliers
# (sinon "ROYAL DANCER 2026 1,00" serait lu comme une quantité de 20261)
_AMOUNT = r"-?\d+,\d{2}"
_TOTAL = r"-?\d{1,3}(?: \d{3})+,\d{2}|-?\d+,\d{2}"

_YENANH_LINE = re.compile(
    rf"^(?P<code>\d{{5,7}})\s+(?P<desc>.+?)\s+"
    rf"(?P<qty>{_AMOUNT})\s+(?P<pu_ttc>{_AMOUNT})\s+(?P<pu_ht>{_AMOUNT})\s+"
    rf"(?:(?P<remise>{_AMOUNT})\s+)?"
    rf"(?P<total>{_TOTAL})\s+(?P<tva>\d+,\d{{2}})$"
)


# même ligne mais colonne « Qté » vide (certaines factures EBP) :
# "600165 GIO DAC BIET PATE DE PORC LKV 5,15 4,88 15,45 5,50"
_YENANH_LINE_NO_QTY = re.compile(
    rf"^(?P<code>\d{{5,7}})\s+(?P<desc>.+?)\s+"
    rf"(?P<pu_ttc>{_AMOUNT})\s+(?P<pu_ht>{_AMOUNT})\s+"
    rf"(?:(?P<remise>{_AMOUNT})\s+)?"
    rf"(?P<total>{_TOTAL})\s+(?P<tva>\d+,\d{{2}})$"
)

# seulement montant + TVA lisibles
_YENANH_LINE_TOTAL_ONLY = re.compile(
    rf"^(?P<code>\d{{5,7}})\s+(?P<desc>.+?)\s+"
    rf"(?P<total>{_TOTAL})\s+(?P<tva>\d+,\d{{2}})$"
)


def _parse_yenanh_line(line):
    """
    → (code, description, quantité | None, P.U. TTC | None, montant TTC, TVA) ou None.
    Le format complet (avec quantité) reste prioritaire ; les variantes sans quantité
    ne sont essayées que s'il ne correspond pas.
    """
    lm = _YENANH_LINE.match(line)
    if lm:
        return (lm.group("code"), lm.group("desc"), _num(lm.group("qty")),
                _num(lm.group("pu_ttc")), _num(lm.group("total")), _num(lm.group("tva")))

    lm = _YENANH_LINE_NO_QTY.match(line)
    if lm:
        return (lm.group("code"), lm.group("desc"), None,
                _num(lm.group("pu_ttc")), _num(lm.group("total")), _num(lm.group("tva")))

    lm = _YENANH_LINE_TOTAL_ONLY.match(line)
    if lm:
        return (lm.group("code"), lm.group("desc"), None,
                None, _num(lm.group("total")), _num(lm.group("tva")))

    return None


def is_yenanh(text):
    t = text.lower()
    return "yenanh.fr" in t or "413 357 286" in t


def parse_yenanh(text):
    result = _empty("yenanh", "Yen Anh")

    # "FACTURE FA202609063" ou (autre modèle EBP) "Facture N° FA202602058"
    m = re.search(r"FACTURE\s+(FA\d+)", text) or re.search(r"Facture\s+N\S*\s*(FA\d+)", text)
    if m:
        result["invoice_number"] = m.group(1)

    # ligne sous l'entête : "10/09/2026 86RESTOPI 10/09/2026 Virement Bancaire"
    # ou "FA202602058 12/02/2026 86RESTOPI Virement Bancaire"
    m = re.search(r"(\d{2}/\d{2}/\d{4})\s+\S+\s+\d{2}/\d{2}/\d{4}", text) or re.search(
        r"FA\d+\s+(\d{2}/\d{2}/\d{4})", text
    )
    if m:
        result["invoice_date"] = _iso(m.group(1))

    # "Transformé de : Bon de livraison N° BL202609103 du 09/09/2026." (parfois "11/ 02/ 2026")
    m = re.search(r"(BL\d+)\s+du\s+(\d{2}/\s*\d{2}/\s*\d{4})", text)
    if m:
        result["delivery_number"] = m.group(1)
        result["delivery_date"] = _iso(re.sub(r"\s+", "", m.group(2)))
    else:
        m = re.search(r"Date de livraison\s*:\s*(\d{2}/\d{2}/\d{4})", text)
        if m:
            result["delivery_date"] = _iso(m.group(1))

    m = re.search(r"Total TTC\s+(\d{1,3}(?: \d{3})*,\d{2})", text)
    if m:
        result["total_ttc"] = _num(m.group(1))
    else:
        # libellés et montants séparés : "Net à payer … 350,80 €" (hors « Capital : 40 000,00 € »)
        without_capital = re.sub(r"Capital\s*:\s*[\d ,.]+€", "", text)
        amounts = [_num(a) for a in re.findall(r"(\d{1,3}(?: \d{3})*,\d{2})\s*€", without_capital)]
        if amounts:
            result["total_ttc"] = max(amounts)

    for raw in text.split("\n"):
        parsed = _parse_yenanh_line(raw.strip())
        if not parsed:
            continue

        code, desc, qty, pu, total, tva = parsed
        source = "invoice"

        if qty is None:
            # colonne quantité vide → montant ÷ P.U. ; sinon vide (complétée par l'historique côté serveur)
            if total == 0:
                continue
            qty = _deduce_quantity(total, pu)
            source = "deduced" if qty is not None else "missing"

        if qty == 0:
            continue

        if source == "invoice" and abs(qty * pu - total) > 0.05:
            result["warnings"].append(f"{code} {desc} : {qty} × {pu} ≠ {total}")

        result["lines"].append(_line(code, desc, qty, pu, total, tva, quantity_source=source))

    deduced = sum(1 for l in result["lines"] if l["quantity_source"] == "deduced")
    missing = sum(1 for l in result["lines"] if l["quantity_source"] == "missing")

    if deduced:
        result["warnings"].append(
            f"Quantités absentes de la facture : {deduced} ligne(s) déduite(s) (montant ÷ P.U.) — à vérifier"
        )
    if missing:
        result["warnings"].append(f"{missing} ligne(s) sans quantité ni prix unitaire lisibles")

    return _finalize(result)


# =========================
# METRO — prix HT, TVA par code (B = 5,50 %, D = 20 %)
# =========================

_METRO_LINE = re.compile(
    r"^(?:(?P<ean>\d{8,14})\s+)?"           # EAN (factures d'achat en magasin)
    r"(?P<code>\d{7})\s+(?P<desc>.+?)\s+"
    r"(?:(?P<poids>\d+,\d{3})\s+)?"          # poids (articles vendus au kg)
    r"(?P<prix>\d+,\d{3})\s+"                # prix unitaire HT (ou prix au kg)
    r"(?:(?P<colis>\d+)\s+)?"                # colisage (nb unités par colis)
    r"(?P<qty>-?\d+)\s+"                     # quantité de colis
    r"(?P<montant>-?\d{1,3}(?: \d{3})*,\d{2}|-?\d+,\d{2})\s+"
    r"(?P<tva>[A-Z])(?:\s+P)?$"
)


# remise sur l'article précédent (montant HT suivi de « - »)
_METRO_LINE_DISCOUNT = re.compile(
    r"^(?P<label>[A-Za-zÀ-ÿ][^\d]*?)\s+(?P<amount>\d{1,3}(?: \d{3})*,\d{2})-$"
)


def is_metro(text):
    return "METRO France" in text or "METRO POITIERS" in text


def parse_metro(text):
    result = _empty("metro", "Metro")

    m = re.search(r"FACTURE\s+([\d/()]+)", text)
    if m:
        result["invoice_number"] = m.group(1)

    m = re.search(r"Date facture\s*:\s*(\d{2}-\d{2}-\d{4})", text)
    if m:
        result["invoice_date"] = _iso(m.group(1))

    m = re.search(r"Livr[ée] le\s*:\s*(\d{2}-\d{2}-\d{4})", text)
    if m:
        result["delivery_date"] = _iso(m.group(1))

    m = re.search(r"commande\s*(\d+-\d+)", text)
    if m:
        result["delivery_number"] = m.group(1)

    m = re.search(r"Total à payer\s+(\d{1,3}(?: \d{3})*,\d{2})", text)
    if m:
        result["total_ttc"] = _num(m.group(1))

    # "438,75 B = 5,50% 24,13 462,88"
    rates = {
        code: _num(rate)
        for code, rate in re.findall(r"([A-Z])\s*=\s*(\d+,\d{2})\s*%", text)
    }

    for raw in text.split("\n"):
        line = raw.strip()

        # remise promo → déduite de l'article qui la précède (prix unitaire réel)
        dm = _METRO_LINE_DISCOUNT.match(line)
        if dm and result["lines"] and result["lines"][-1]["kind"] == "product":
            last = result["lines"][-1]
            discount_ttc = _ttc(_num(dm.group("amount")), last["tva"] or 0)
            last["total"] = round(last["total"] - discount_ttc, 2)
            if last["quantity"]:
                last["unit_price"] = round(last["total"] / last["quantity"], 4)
            result["warnings"].append(
                f"{last['code']} {last['description']} : {dm.group('label').strip()} "
                f"-{dm.group('amount')} € HT déduit du prix"
            )
            continue

        lm = _METRO_LINE.match(line)
        if not lm:
            continue

        code = lm.group("code")
        desc = lm.group("desc").lstrip("*").strip()
        rate = rates.get(lm.group("tva"))

        if rate is None:
            result["warnings"].append(f"{code} {desc} : taux TVA « {lm.group('tva')} » inconnu (compté 0 %)")
            rate = 0.0

        montant = _num(lm.group("montant"))
        prix = _num(lm.group("prix"))
        qty_colis = int(lm.group("qty"))

        if lm.group("poids"):
            # vendu au kg : quantité = poids, prix = prix au kg
            poids = _num(lm.group("poids")) * (1 if qty_colis >= 0 else -1)
            result["lines"].append(_line(
                code, desc, poids, _ttc(prix, rate), _ttc(montant, rate), rate, unit_hint="kg"
            ))
            continue

        if qty_colis == 0:
            continue

        colis = int(lm.group("colis") or 1)

        result["lines"].append(_line(
            code,
            desc,
            qty_colis,
            _ttc(montant / qty_colis, rate),
            _ttc(montant, rate),
            rate,
            unit_hint=f"colis de {colis}" if colis > 1 else "",
        ))

    return _finalize(result)


# =========================
# EXOSTAR (Shopify) — prix TTC
# =========================

def _code_from_name(name, prefix="SANS-SKU-"):
    """Article sans code : code stable dérivé du nom (mémorisable comme un vrai code)."""
    ascii_name = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    slug = re.sub(r"[^A-Z0-9]+", "-", ascii_name.upper()).strip("-")
    return prefix + (slug[:40] or "ARTICLE")


def is_exostar(text):
    return "exostar" in text.lower()


def parse_exostar(text):
    result = _empty("exostar", "Exostar")

    m = re.search(r"Facture\s+(E\d+)", text)
    if m:
        result["invoice_number"] = m.group(1)

    m = re.search(r"Date:\s*(\d{2}/\d{2}/\d{4})", text)
    if m:
        result["invoice_date"] = _iso(m.group(1))

    m = re.search(r"Total T\.T\.C\s*€\s*([\d.,]+)", text)
    if m:
        result["total_ttc"] = _num(m.group(1))

    item_re = re.compile(r"^(?:(?P<name>.*?)\s+)?(?P<qty>-?\d+)\s+€(?P<pu>[\d.,]+)\s+€(?P<total>-?[\d.,]+)$")

    in_items = False
    current = None
    without_sku = []

    def flush():
        """enregistre l'article en cours — même sans ligne « SKU: » (nouveau produit Shopify)"""
        nonlocal current
        if current is None:
            return
        name = " ".join(current["name"]).strip()
        code = current.get("code") or _code_from_name(name)
        if not current.get("code"):
            without_sku.append(name or code)
        result["lines"].append(_line(
            code, name or code, current["qty"], current["pu"], current["total"], 5.5
        ))
        current = None

    for raw in text.split("\n"):
        line = raw.strip()

        if line.startswith("Articles Quantit"):
            in_items = True
            continue

        if not in_items or not line:
            continue

        if re.match(r"(Remise|Total Produits|TVA\(|Exp[ée]dition|Total T\.T\.C)", line):
            flush()
            in_items = False
            continue

        sku = re.match(r"SKU:\s*(\S*)", line)
        if sku:
            if current is not None:
                current["code"] = sku.group(1)
                flush()
            continue

        im = item_re.match(line)
        if im:
            # nouvel article alors que le précédent n'a pas eu de SKU → ne pas le perdre
            flush()
            current = {
                "name": [im.group("name")] if im.group("name") else [],
                "qty": int(im.group("qty")),
                "pu": _num(im.group("pu")),
                "total": _num(im.group("total")),
            }
        elif current is not None:
            current["name"].append(line)

    flush()

    if without_sku:
        result["warnings"].append(
            f"{len(without_sku)} article(s) sans SKU (code créé à partir du nom) : " + ", ".join(without_sku)
        )

    # remise globale (code promo)
    m = re.search(r"Remise(?:\s*\(([^)]*)\))?\s*-\s*€\s*([\d.,]+)", text)
    if m and _num(m.group(2)) > 0:
        label = f"Remise {m.group(1)}" if m.group(1) else "Remise"
        amount = _num(m.group(2))
        result["lines"].append(_line("REMISE", label, 1, -amount, -amount, kind="discount"))

    return _finalize(result)


# =========================
# EMBALLAGE FUTÉ — prix HT
# =========================

_EF_LINE = re.compile(
    r"^(?P<code>EF\S+)\s+(?P<qty>-?\d+)\s+(?P<desc>.+?)\s+"
    r"(?P<pu>-?\d[\d ]*,\d{2}) €\s+(?P<total>-?\d[\d ]*,\d{2}) €\s+(?P<ct>\d+)$"
)


def is_emballage_fute(text):
    return "emballagefute" in text.lower() or "EMBALLAGE FUTE" in text


def parse_emballage_fute(text):
    result = _empty("emballage_fute", "Emballage futé")

    m = re.search(r"FACTURE\s+(\d+)", text)
    if m:
        result["invoice_number"] = m.group(1)

    m = re.search(r"FACTURE\s+\d+.*\n\s*(\d{2}/\d{2}/\d{4})", text)
    if m:
        result["invoice_date"] = _iso(m.group(1))

    m = re.search(r"BL N°\s*(\d+).*?du\s+(\d{2})/(\d{2})", text)
    if m:
        result["delivery_number"] = m.group(1)
        if result["invoice_date"]:
            result["delivery_date"] = f"{result['invoice_date'][:4]}-{m.group(3)}-{m.group(2)}"

    m = re.search(r"NET A PAYER\s+(\d[\d ]*,\d{2})", text) or re.search(r"Total TTC\s+(\d[\d ]*,\d{2})", text)
    if m:
        result["total_ttc"] = _num(m.group(1))

    # tableau TVA : "1 467,12 € 20 % 93,42 €" → code 1 = 20 %
    rates = {
        code: _num(rate)
        for code, rate in re.findall(r"^(\d)\s+\d[\d ,]*€\s+(\d+(?:,\d+)?)\s*%", text, re.M)
    }

    last = None

    for raw in text.split("\n"):
        line = raw.strip()
        lm = _EF_LINE.match(line)

        if lm:
            rate = rates.get(lm.group("ct"), 20.0)
            qty = int(lm.group("qty"))
            total_ht = _num(lm.group("total"))

            if "remise" in lm.group("code").lower() or "remise" in lm.group("desc").lower():
                last = None
                result["lines"].append(_line(
                    "REMISE", lm.group("desc"), 1, _ttc(total_ht, rate), _ttc(total_ht, rate),
                    rate, kind="discount"
                ))
                continue

            if qty == 0:
                continue

            last = _line(
                lm.group("code"), lm.group("desc"), qty,
                _ttc(_num(lm.group("pu")), rate), _ttc(total_ht, rate), rate
            )
            result["lines"].append(last)
            continue

        # suite de désignation sur la ligne suivante : "(LOT DE 300)"
        if last is not None and re.match(r"^\(.*\)$", line):
            last["description"] += " " + line
        else:
            last = None

    return _finalize(result)


# =========================
# DELIDRINKS (Natural Drinks) — prix HT
# =========================

_DD_LINE = re.compile(
    r"^(?P<code>\d{5,7})\s+(?P<desc>.+?)\s+(?P<qty>-?\d+\.\d{2})\s+"
    r"(?P<pu>\d+,\d{2})\s+(?:(?P<remise>\d+,\d{2})\s+)?(?P<pu_net>\d+,\d{2})\s+"
    r"(?P<total>-?\d[\d ]*,\d{2})\s+(?P<tva>\d)$"
)


def is_delidrinks(text):
    return "delidrinks" in text.lower() or "NATURAL DRINKS" in text


def parse_delidrinks(text):
    result = _empty("delidrinks", "Delidrinks")

    m = re.search(r"^(\d{2}/\d{2}/\d{4})\s+(\d+)\s+CW", text, re.M)
    if m:
        result["invoice_date"] = _iso(m.group(1))
        result["invoice_number"] = m.group(2)

    m = re.search(r"Bon de livraison n°\s*(\d+)", text)
    if m:
        result["delivery_number"] = m.group(1)

    m = re.search(r"\*+\s*(\d[\d ]*,\d{2})\s*EUR", text)
    if m:
        result["total_ttc"] = _num(m.group(1))

    # "228,37 2 228,37 5,50 12,56 ******240,93EUR" → code 2 = 5,50 %
    rates = {
        code: _num(rate)
        for code, rate in re.findall(r"(?<![\d,])(\d)\s+\d+,\d{2}\s+(\d+,\d{2})\s+\d+,\d{2}\s+\*", text)
    }

    lines = text.split("\n")

    for i, raw in enumerate(lines):
        lm = _DD_LINE.match(raw.strip())
        if not lm:
            continue

        rate = rates.get(lm.group("tva"))
        if rate is None:
            result["warnings"].append(f"{lm.group('code')} : taux TVA inconnu (compté 5,5 %)")
            rate = 5.5

        qty = float(lm.group("qty"))
        if qty == 0:
            continue

        hint = lines[i + 1].strip().lower() if i + 1 < len(lines) else ""

        result["lines"].append(_line(
            lm.group("code"), lm.group("desc"), qty,
            _ttc(_num(lm.group("pu_net")), rate), _ttc(_num(lm.group("total")), rate), rate,
            unit_hint=hint if hint.startswith("a la ") else "",
        ))

    if re.search(r"FRAIS DE PORT\s+\d", text):
        result["warnings"].append("Frais de port présents : non importés")

    return _finalize(result)


# =========================
# DISPATCH
# =========================

# =========================
# NHU Y — SAS FAST VIET / Petite Asie (Chasseneuil) — prix HT, pas de code article
# =========================

_NHUY_LINE = re.compile(
    r"^(?P<qty>-?\d+(?:,\d+)?)\s+(?P<desc>.+?)\s+"
    r"(?P<pu>\d{1,3}(?: \d{3})*,\d{2})\s*€\s+"
    r"(?P<total>-?\d{1,3}(?: \d{3})*,\d{2})\s*€$"
)

_FR_MONTHS = {
    "janv": 1, "jan": 1, "fevr": 2, "févr": 2, "fev": 2, "fév": 2, "mars": 3, "avr": 4,
    "mai": 5, "juin": 6, "juil": 7, "aout": 8, "août": 8, "sept": 9, "oct": 10,
    "nov": 11, "dec": 12, "déc": 12,
}


def is_nhuy(text):
    t = text.lower()
    return "fast viet" in t or "petiteasie" in t or "821 959 947" in t


def parse_nhuy(text):
    result = _empty("nhuy", "Nhu Y")

    m = re.search(r"Facture\s+num\S*\s*:\s*(\S+)", text)
    if m:
        result["invoice_number"] = m.group(1)

    m = re.search(r"Date\s*:\s*(\d{2}/\d{2}/\d{4})", text)
    if m:
        result["invoice_date"] = _iso(m.group(1))

    m = re.search(r"Total TTC\s+(\d{1,3}(?: \d{3})*,\d{2})", text)
    if m:
        result["total_ttc"] = _num(m.group(1))

    m = re.search(r"Total HT\s+(\d{1,3}(?: \d{3})*,\d{2})", text)
    total_ht = _num(m.group(1)) if m else None

    # TVA de la facture : taux qui ont un montant (« T.V.A 5,5% 15,53 € », « T.V.A 10% » vide)
    rates = [
        _num(rate)
        for rate, amount in re.findall(r"T\.?V\.?A\.?\s*(\d+(?:,\d+)?)\s*%\s*(\d{1,3}(?: \d{3})*,\d{2})?", text)
        if amount and _num(amount) > 0
    ]

    if len(rates) == 1:
        rate = rates[0]
    elif total_ht and result["total_ttc"]:
        # plusieurs taux sans détail par ligne → taux moyen de la facture
        rate = round((result["total_ttc"] / total_ht - 1) * 100, 2)
        result["warnings"].append(
            f"Plusieurs taux de TVA sans détail par ligne : taux moyen {rate} % appliqué"
        )
    else:
        rate = 5.5
        result["warnings"].append("Taux de TVA introuvable : 5,5 % appliqué")

    for raw in text.split("\n"):
        lm = _NHUY_LINE.match(raw.strip())
        if not lm:
            continue

        qty = _num(lm.group("qty"))
        total_ht_line = _num(lm.group("total"))
        desc = lm.group("desc")

        if qty == 0:
            continue

        # prix affiché arrondi (52 × 0,64 ≠ 33,37) → prix réel = total ÷ quantité
        result["lines"].append(_line(
            _code_from_name(desc, prefix="NHUY-"),
            desc,
            qty,
            _ttc(total_ht_line / qty, rate),
            _ttc(total_ht_line, rate),
            rate,
        ))

    # facture récapitulative : tableau des livraisons « 26-mars 120 50 », « 24-avr 120 50 24 »
    deliveries = []
    year = int(result["invoice_date"][:4]) if result["invoice_date"] else None
    invoice_month = int(result["invoice_date"][5:7]) if result["invoice_date"] else None

    for day, month in re.findall(r"^(\d{1,2})-([a-zéû]+)\.?(?:\s+\d+)+\s*$", text, re.M | re.I):
        mon = _FR_MONTHS.get(month.lower().rstrip("."))
        if not mon or not year:
            continue
        y = year - 1 if invoice_month and mon > invoice_month else year
        deliveries.append(f"{y}-{mon:02d}-{int(day):02d}")

    if deliveries:
        deliveries.sort()
        result["delivery_date"] = deliveries[-1]
        result["warnings"].append(
            "Facture récapitulative, livraisons du "
            + ", ".join(f"{d[8:10]}/{d[5:7]}" for d in deliveries)
            + f" : date d'achat = dernière livraison ({deliveries[-1][8:10]}/{deliveries[-1][5:7]}), modifiable"
        )

    return _finalize(result)


# =========================
# AUCHAN (facture drive / magasin) — prix HT + colonne TTC par ligne
# =========================

# "3228857001347 HARRYS BRIOC TR CHOCO SAD 485G 3,00 1,80 1 1,20 5,50 1,27"
#  EAN           désignation                   P.U.HT remise.U qté net.HT TVA net.TTC
_AUCHAN_LINE = re.compile(
    rf"^(?P<ean>\d{{8,14}})\s+(?P<desc>.+?)\s+"
    rf"(?P<pu>\d+,\d{{2}})\s+"
    rf"(?:(?P<remise>\d+,\d{{2}})\s+)?"
    rf"(?P<qty>\d+(?:,\d{{1,3}})?)\s+"
    rf"(?P<net>{_TOTAL})\s+"
    rf"(?P<tva>\d+,\d{{2}})\s+"
    rf"(?P<ttc>{_TOTAL})$"
)

# "Eco-participation : 0,10 0,20 0,21"  (unitaire HT, total HT, total TTC)
_AUCHAN_ECO = re.compile(r"^.co-participation\s*:.*?(?P<ttc>-?\d+,\d{2})$", re.I)


def is_auchan(text):
    return "Groupe Auchan" in text or "AUCHAN HYPERMARCHE" in text or re.search(r"\bAHY\d{8,}", text)


def parse_auchan(text):
    result = _empty("auchan", "Auchan")

    m = re.search(r"\b(AHY\d{8,})\b", text)
    if m:
        result["invoice_number"] = m.group(1)

    # libellé et date sur des lignes différentes (mise en page en 2 colonnes) :
    # « Facture éditée le : » … « 08/11/2026 » puis « Date de commande : » … « 06/11/2026 »
    m = re.search(r"dit.e le\s*:.*?(\d{2}/\d{2}/\d{4})", text, re.S)
    if m:
        result["invoice_date"] = _iso(m.group(1))

    m = re.search(r"Date de commande\s*:.*?(\d{2}/\d{2}/\d{4})", text, re.S)
    if m:
        result["delivery_date"] = _iso(m.group(1))   # date d'achat réelle

    m = re.search(r"Total . payer[^\d\n]*(\d{1,3}(?: \d{3})*,\d{2})", text)
    if m:
        result["total_ttc"] = _num(m.group(1))

    for raw in text.split("\n"):
        line = raw.strip()

        em = _AUCHAN_ECO.match(line)
        if em:
            amount = _num(em.group("ttc"))
            if amount:
                result["lines"].append(_line(
                    "ECO-PART", "Éco-participation", 1, amount, amount, kind="discount"
                ))
            continue

        lm = _AUCHAN_LINE.match(line)
        if not lm:
            continue

        ean = lm.group("ean")
        desc = lm.group("desc")
        rate = _num(lm.group("tva"))
        qty = _num(lm.group("qty"))
        pu_ht = _num(lm.group("pu"))
        remise_ht = _num(lm.group("remise")) if lm.group("remise") else 0.0
        net_ht = _num(lm.group("net"))
        total_ttc = _num(lm.group("ttc"))   # montant TTC imprimé : pas d'arrondi de conversion

        if qty == 0:
            continue

        if abs((pu_ht - remise_ht) * qty - net_ht) > max(0.05, 0.01 * abs(net_ht)):
            result["warnings"].append(
                f"{ean} {desc} : ligne ambiguë ({lm.group('pu')} − {lm.group('remise') or '0'}) "
                f"× {lm.group('qty')} ≠ {lm.group('net')} € HT, à vérifier"
            )

        if total_ttc < 0:
            # remise de lot plus grande que l'article (promo répartie sur d'autres articles) :
            # article au prix normal + ligne de remise séparée
            gross_ttc = round(_ttc(pu_ht * qty, rate), 2)
            result["lines"].append(_line(ean, desc, qty, gross_ttc / qty, gross_ttc, rate))
            result["lines"].append(_line(
                "REMISE", f"Remise lot {desc}", 1, total_ttc - gross_ttc, total_ttc - gross_ttc,
                rate, kind="discount",
            ))
            result["warnings"].append(
                f"{ean} {desc} : remise lot {remise_ht:.2f} € HT/u supérieure au prix, "
                f"article compté au prix normal + ligne de remise {total_ttc - gross_ttc:.2f} €"
            )
            continue

        if remise_ht:
            result["warnings"].append(
                f"{ean} {desc} : remise {lm.group('remise')} € HT/u déduite du prix"
            )

        result["lines"].append(_line(ean, desc, qty, total_ttc / qty, total_ttc, rate))

    return _finalize(result)


def parse_supplier_invoice(pdf_bytes):
    text = extract_text(pdf_bytes)

    if is_auchan(text):
        return parse_auchan(text)

    if is_metro(text):
        # filigrane « Duplicata » en gros caractères gris mélangé au texte
        return parse_metro(extract_text(pdf_bytes, max_char_size=20))

    if is_exostar(text):
        return parse_exostar(text)

    if is_emballage_fute(text):
        return parse_emballage_fute(text)

    if is_delidrinks(text):
        return parse_delidrinks(text)

    if is_yenanh(text):
        return parse_yenanh(text)

    if is_nhuy(text):
        return parse_nhuy(text)

    result = _empty(None, "")
    result["lines_total"] = 0
    result["warnings"].append(
        "Format de facture non reconnu (gérés : Yen Anh, Metro, Exostar, Emballage futé, Delidrinks, Nhu Y, Auchan)"
    )
    return result
