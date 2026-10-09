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
        {"code", "description", "quantity", "unit_price", "total", "tva", "unit_hint", "kind"}
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


def _line(code, description, quantity, unit_price, total, tva=None, unit_hint="", kind="product"):
    return {
        "code": str(code),
        "description": re.sub(r"\s+", " ", description).strip(),
        "quantity": round(quantity, 3),
        "unit_price": round(unit_price, 4),
        "total": round(total, 2),
        "tva": tva,
        "unit_hint": unit_hint,
        "kind": kind,
    }


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


def is_yenanh(text):
    t = text.lower()
    return "yenanh.fr" in t or "413 357 286" in t


def parse_yenanh(text):
    result = _empty("yenanh", "Yen Anh")

    m = re.search(r"FACTURE\s+(FA\d+)", text)
    if m:
        result["invoice_number"] = m.group(1)

    # ligne sous l'entête : "10/09/2026 86RESTOPI 10/09/2026 Virement Bancaire"
    m = re.search(r"(\d{2}/\d{2}/\d{4})\s+\S+\s+\d{2}/\d{2}/\d{4}", text)
    if m:
        result["invoice_date"] = _iso(m.group(1))

    # "Transformé de : Bon de livraison N° BL202609103 du 09/09/2026."
    m = re.search(r"(BL\d+)\s+du\s+(\d{2}/\d{2}/\d{4})", text)
    if m:
        result["delivery_number"] = m.group(1)
        result["delivery_date"] = _iso(m.group(2))

    m = re.search(r"Total TTC\s+(\d{1,3}(?: \d{3})*,\d{2})", text)
    if m:
        result["total_ttc"] = _num(m.group(1))

    for raw in text.split("\n"):
        lm = _YENANH_LINE.match(raw.strip())
        if not lm:
            continue

        qty = _num(lm.group("qty"))
        pu = _num(lm.group("pu_ttc"))
        total = _num(lm.group("total"))

        if qty == 0:
            continue

        if abs(qty * pu - total) > 0.05:
            result["warnings"].append(f"{lm.group('code')} {lm.group('desc')} : {qty} × {pu} ≠ {total}")

        result["lines"].append(
            _line(lm.group("code"), lm.group("desc"), qty, pu, total, _num(lm.group("tva")))
        )

    return _finalize(result)


# =========================
# METRO — prix HT, TVA par code (B = 5,50 %, D = 20 %)
# =========================

_METRO_LINE = re.compile(
    r"^(?P<code>\d{7})\s+(?P<desc>.+?)\s+"
    r"(?:(?P<poids>\d+,\d{3})\s+)?"          # poids (articles vendus au kg)
    r"(?P<prix>\d+,\d{3})\s+"                # prix unitaire HT (ou prix au kg)
    r"(?:(?P<colis>\d+)\s+)?"                # colisage (nb unités par colis)
    r"(?P<qty>-?\d+)\s+"                     # quantité de colis
    r"(?P<montant>-?\d{1,3}(?: \d{3})*,\d{2}|-?\d+,\d{2})\s+"
    r"(?P<tva>[A-Z])(?:\s+P)?$"
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
        lm = _METRO_LINE.match(raw.strip())
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

    for raw in text.split("\n"):
        line = raw.strip()

        if line.startswith("Articles Quantit"):
            in_items = True
            continue

        if not in_items or not line:
            continue

        if re.match(r"(Remise|Total Produits|TVA\(|Exp[ée]dition|Total T\.T\.C)", line):
            in_items = False
            continue

        sku = re.match(r"SKU:\s*(\S+)", line)
        if sku and current:
            current["code"] = sku.group(1)
            result["lines"].append(_line(
                current["code"], " ".join(current["name"]),
                current["qty"], current["pu"], current["total"], 5.5
            ))
            current = None
            continue

        im = item_re.match(line)
        if im:
            current = {
                "name": [im.group("name")] if im.group("name") else [],
                "qty": int(im.group("qty")),
                "pu": _num(im.group("pu")),
                "total": _num(im.group("total")),
            }
        elif current is not None:
            current["name"].append(line)

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

def parse_supplier_invoice(pdf_bytes):
    text = extract_text(pdf_bytes)

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

    result = _empty(None, "")
    result["lines_total"] = 0
    result["warnings"].append(
        "Format de facture non reconnu (gérés : Yen Anh, Metro, Exostar, Emballage futé, Delidrinks)"
    )
    return result
