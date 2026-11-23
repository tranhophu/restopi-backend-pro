"""Commande à table par QR code : catalogue des formules, horaires et calcul du prix côté serveur."""

import hashlib
import hmac
import json
import os
import re
import unicodedata

import menu_sync

SOURCE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "formules.json")
MAX_LINES = 30
MAX_UNITS = 40
MAX_QTY = 20
CARTE_TABS = [("entree", "Entrées"), ("plat", "Plats"), ("dessert", "Desserts"), ("boisson", "Boissons")]
MAX_NOTE = 200


def load_catalog():
    with open(SOURCE, encoding="utf-8") as f:
        return json.load(f)


def table_key(secret, table):
    """Clé du QR code : sans elle, impossible d'envoyer un ticket en cuisine."""
    return hmac.new(secret.encode(), f"table:{table}".encode(), hashlib.sha256).hexdigest()[:16]


def verify_table(catalog, secret, table, key):
    if not secret or not key:
        return None
    try:
        number = int(table)
    except (TypeError, ValueError):
        return None
    if number not in catalog["tables"]:
        return None
    return number if hmac.compare_digest(table_key(secret, number), str(key)) else None


def current_service(catalog, now):
    """Service en cours (dict) ou None. `now` doit être à l'heure de Paris."""
    service = catalog["service"]
    if now.weekday() not in service["days"]:
        return None
    clock = now.strftime("%H:%M")
    return next((w for w in service["windows"] if w["start"] <= clock < w["end"]), None)


def _cents(value):
    return int(round(float(value or 0) * 100))


def _choice(raw):
    return raw if isinstance(raw, dict) else {"label": raw}


def public_menu(catalog, service_key):
    formules = [f for f in catalog["formules"] if service_key in f["services"]]
    used = set()
    for f in formules:
        for path in f["paths"]:
            used.update(s["group"] for s in path["steps"])
        if f.get("addon"):
            used.update(s["group"] for s in f["addon"]["steps"])
    groups = {
        key: [{**dish, "choices": [_choice(c) for c in dish.get("choices", [])]} for dish in catalog["groups"][key]]
        for key in used
    }
    return {"formules": formules, "groups": groups}


def _read_picks(catalog, steps, picks):
    if not isinstance(picks, list) or len(picks) != len(steps):
        raise ValueError("Choix incomplets")
    details, extra = [], 0
    for step, pick in zip(steps, picks):
        if not isinstance(pick, dict):
            raise ValueError("Choix invalide")
        dish = next((d for d in catalog["groups"][step["group"]] if d["id"] == pick.get("dish")), None)
        if not dish:
            raise ValueError(f"{step['label']} : plat indisponible")
        extra += _cents(dish.get("extra"))
        text = dish["name"]
        choices = [_choice(c) for c in dish.get("choices", [])]
        if choices:
            choice = next((c for c in choices if c["label"] == pick.get("choice")), None)
            if not choice:
                raise ValueError(f"{step['label']} : choix manquant")
            extra += _cents(choice.get("extra"))
            text += f" ({choice['label']})"
        details.append(f"{step['label']} : {text}")
    return details, extra


def build_item(catalog, service_key, raw):
    if not isinstance(raw, dict):
        raise ValueError("Formule invalide")
    formule = next((f for f in catalog["formules"] if f["id"] == raw.get("formule")), None)
    if not formule or service_key not in formule["services"]:
        raise ValueError("Cette formule n'est pas disponible à cette heure")

    path_index = raw.get("path", 0)
    if type(path_index) is not int or not 0 <= path_index < len(formule["paths"]):
        raise ValueError("Formule invalide")

    details = [f"Inclus : {name}" for name in formule.get("fixed", [])]
    picked, cents = _read_picks(catalog, formule["paths"][path_index]["steps"], raw.get("picks"))
    details += picked
    total = _cents(formule["price"]) + cents

    if raw.get("addon"):
        addon = formule.get("addon")
        if not addon:
            raise ValueError("Option indisponible")
        picked, cents = _read_picks(catalog, addon["steps"], raw.get("addon_picks"))
        details += picked
        total += _cents(addon["price"]) + cents

    return {"name": formule["name"], "qty": 1, "price": total / 100, "formule": formule["id"], "details": details}


def ticket_name(text):
    """L'imprimante thermique ne connaît ni « œ » ni « Ê » : Crêpe au bœuf → Crepe au boeuf."""
    text = str(text).replace("œ", "oe").replace("Œ", "OE").replace("–", "-").replace("«", '"').replace("»", '"')
    return " ".join(unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode().split())


def carte_tabs(public):
    """Carte du site par onglet ; les suppléments suivent les plats."""
    tabs = []
    for key, label in CARTE_TABS:
        keys = {key, "supplement"} if key == "plat" else {key}
        sections = [
            {
                "title": section["title"],
                "dishes": [
                    {
                        "name": dish["name"],
                        "desc": dish.get("print_desc") or dish.get("desc") or "",
                        "price": dish["price"],
                        "from": dish["price_from"],
                        "variants": [
                            {
                                "id": v["id"],
                                "name": v["name"],
                                "label": re.sub(r"\s*\(\+.*?\)", "", v.get("label") or ""),
                                "price": v["price"],
                                "choose": v.get("choose"),
                            }
                            for v in dish["variants"]
                        ],
                    }
                    for dish in section["dishes"]
                ],
            }
            for section in public["sections"]
            if section["key"] in keys
        ]
        if sections:
            tabs.append({"key": key, "label": label, "sections": sections})
    return tabs


def build_carte_item(raw, variants):
    """Plat à la carte : prix et choix toujours relus côté serveur."""
    product_id, qty = raw.get("product"), raw.get("qty", 1)
    if type(product_id) is not int or type(qty) is not int or not 1 <= qty <= MAX_QTY:
        raise ValueError("Quantité invalide")
    variant = variants.get(product_id)
    if not variant:
        raise ValueError("Un plat n'est plus disponible. Retirez-le de votre commande.")

    name = variant["name"]
    try:
        options = menu_sync.clean_options(variant.get("choose"), raw.get("options") or [])
    except ValueError as e:
        raise ValueError(f"« {name} » : {e}")

    item = {"id": product_id, "name": ticket_name(name), "base_name": name, "qty": qty, "price": variant["price"]}
    if options:
        item["options"] = options
        item["details"] = [f"Choix : {menu_sync.format_options(options)}"]
    return item


def build_items(catalog, service_key, lines, load_carte):
    """Formules et plats à la carte dans un même ticket. load_carte : menu public, lu seulement si besoin."""
    if not isinstance(lines, list) or not lines:
        raise ValueError("Votre commande est vide")
    if len(lines) > MAX_LINES:
        raise ValueError(f"{MAX_LINES} lignes maximum par envoi")

    variants = None
    items = []
    for line in lines:
        if isinstance(line, dict) and "product" in line:
            if variants is None:
                variants = {v["id"]: v for s in load_carte()["sections"] for d in s["dishes"] for v in d["variants"]}
            items.append(build_carte_item(line, variants))
        else:
            items.append(build_item(catalog, service_key, line))

    if sum(i["qty"] for i in items) > MAX_UNITS:
        raise ValueError(f"{MAX_UNITS} articles maximum par envoi")
    return items, sum(_cents(i["price"]) * i["qty"] for i in items) / 100


def clean_note(note):
    text = " ".join(str(note or "").split())
    return text[:MAX_NOTE]
