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
        {"code", "description", "quantity", "unit_price", "unit_price_ht", "total", "tva"}
    ],
    "lines_total": 739.25,
    "warnings": [...]
}
Les prix sont en TTC (même convention que les achats saisis à la main).
"""

import io
import re

import pdfplumber


def _num(value):
    """'1 234,56' / '-2,00' → float"""
    return float(value.replace(" ", "").replace(",", "."))


def _iso(date_fr):
    """'10/09/2026' → '2026-09-10'"""
    d, m, y = date_fr.split("/")
    return f"{y}-{m}-{d}"


def extract_text(pdf_bytes):
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        return "\n".join((page.extract_text() or "") for page in pdf.pages)


# =========================
# YEN ANH (logiciel EBP)
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
    result = {
        "format": "yenanh",
        "supplier": "Yen Anh",
        "invoice_number": "",
        "invoice_date": "",
        "delivery_number": "",
        "delivery_date": "",
        "total_ttc": None,
        "lines": [],
        "warnings": [],
    }

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
        line = raw.strip()

        lm = _YENANH_LINE.match(line)
        if not lm:
            continue

        qty = _num(lm.group("qty"))
        pu = _num(lm.group("pu_ttc"))
        total = _num(lm.group("total"))

        if qty == 0:
            continue

        # contrôle : qté × PU ≈ montant
        if abs(qty * pu - total) > 0.05:
            result["warnings"].append(
                f"{lm.group('code')} {lm.group('desc')} : {qty} × {pu} ≠ {total}"
            )

        result["lines"].append({
            "code": lm.group("code"),
            "description": re.sub(r"\s+", " ", lm.group("desc")).strip(),
            "quantity": qty,
            "unit_price": pu,
            "unit_price_ht": _num(lm.group("pu_ht")),
            "total": total,
            "tva": _num(lm.group("tva")),
        })

    result["lines_total"] = round(sum(l["total"] for l in result["lines"]), 2)

    if not result["invoice_number"]:
        result["warnings"].append("Numéro de facture introuvable")
    if not result["invoice_date"]:
        result["warnings"].append("Date de facture introuvable")
    if not result["lines"]:
        result["warnings"].append("Aucune ligne article trouvée")
    elif result["total_ttc"] is not None and abs(result["lines_total"] - result["total_ttc"]) > 0.05:
        result["warnings"].append(
            f"Total lignes {result['lines_total']:.2f} € ≠ total facture {result['total_ttc']:.2f} €"
        )

    return result


# =========================
# DISPATCH
# =========================

def parse_supplier_invoice(pdf_bytes):
    text = extract_text(pdf_bytes)

    if is_yenanh(text):
        return parse_yenanh(text)

    return {
        "format": None,
        "supplier": "",
        "invoice_number": "",
        "invoice_date": "",
        "delivery_number": "",
        "delivery_date": "",
        "total_ttc": None,
        "lines": [],
        "lines_total": 0,
        "warnings": ["Format de facture non reconnu (seul Yen Anh est géré pour l'instant)"],
    }
