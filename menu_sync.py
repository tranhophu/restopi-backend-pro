"""
Synchronise le site avec le menu de référence (menu_source.json).

    python menu_sync.py           # aperçu depuis le site en ligne : rien n'est écrit
    python menu_sync.py --apply   # applique en base (prix, images, nouveaux plats)

Ne supprime, ne masque et ne renomme jamais un plat : les ids restent valables pour
les commandes, le ticket cuisine et les fiches de coût.
Images : déposer dans backend/images un fichier nommé d'après le plat
(« Soupe Raviolis » → soupe_raviolis.png) ; il est repris automatiquement.
"""

import json
import os
import re
import sys
import unicodedata
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
SOURCE = os.path.join(HERE, "menu_source.json")
IMAGES = os.path.join(HERE, "images")
LIVE_URL = "https://restopi-backend-pro-production.up.railway.app/products"
IMAGE_EXT = (".png", ".jpg", ".jpeg", ".webp")


def slug(text):
    ascii_name = unicodedata.normalize("NFKD", text.replace("œ", "oe").replace("Œ", "OE"))
    ascii_name = ascii_name.encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+", "_", ascii_name).strip("_")


def available_images():
    return {
        os.path.splitext(f)[0].lower(): f
        for f in os.listdir(IMAGES)
        if f.lower().endswith(IMAGE_EXT)
    }


def resolve_image(dish, variant, name, files):
    """Priorité : fichier au nom du plat exact, image de la variante, image du plat."""
    label = re.sub(r"\(.*?\)", "", variant.get("label", ""))
    base = re.sub(r"(au choix|[–-] \d pi[eè]ces?)", "", dish["name"], flags=re.I)
    for key in (slug(name), slug(f"{base} {label}"), slug(dish["name"])):
        if key in files:
            return "images/" + files[key]
    for candidate in (variant.get("img"), dish.get("img")):
        if (
            candidate
            and os.path.splitext(candidate)[0].lower() in files
            and image_fits(candidate, name, variant.get("label", ""))
        ):
            return "images/" + candidate
    return None


PROTEINS = ("crevette", "porc", "poulet", "boeuf", "canard", "saumon")


def image_fits(image, name, label=""):
    """Jamais la photo d'un autre ingrédient (crevettes sur un plat végé ou au poulet : allergies)."""
    wanted = slug(f"{name} {label}")
    return all(word in wanted for word in PROTEINS if word in slug(image))


def hidden_ids(source, products):
    """Ids des plats « hidden » et de leurs doublons (aliases) : plus proposés ni commandables."""
    by_name = {p["name"].strip().lower(): p["id"] for p in products}
    ids = set()
    for section in source["sections"]:
        for dish in section["dishes"]:
            if not dish.get("hidden"):
                continue
            for v in dish["variants"]:
                pid = v.get("id") or by_name.get(v["product"].strip().lower())
                if pid:
                    ids.add(pid)
    ids |= {int(alias) for alias, target in source.get("aliases", {}).items() if target in ids}
    return ids


def build_public_menu(source, products):
    """Menu du site : prix et disponibilité viennent de la base, l'organisation de menu_source.json."""
    by_id = {p["id"]: p for p in products}
    by_name = {p["name"].strip().lower(): p for p in products}
    used = (
        {int(a) for a in source.get("aliases", {})}
        | set(source.get("retired", []))
        | hidden_ids(source, products)
    )
    sections = []

    for section in source["sections"]:
        dishes = []
        for dish in section["dishes"]:
            if dish.get("hidden"):
                continue
            variants = []
            for v in dish["variants"]:
                p = by_id.get(v["id"]) if v.get("id") else by_name.get(v["product"].strip().lower())
                if not p:
                    continue
                used.add(p["id"])
                img = p.get("img") or ""
                variants.append({
                    "id": p["id"],
                    "label": v.get("label", ""),
                    "name": p["name"],
                    "price": p["price"],
                    "tva": p["tva"],
                    "img": "" if "default" in img else img,
                    "print": v.get("print", True),
                    "choose": v.get("choose"),
                })
            if variants:
                printable = [x["price"] for x in variants if x["print"]] or [x["price"] for x in variants]
                dishes.append({
                    "name": dish["name"],
                    "desc": dish.get("desc", ""),
                    "print_desc": dish.get("print_desc", ""),
                    "img": f"images/{dish['img']}" if dish.get("img") else "",
                    "price": min(printable),
                    "price_from": len(set(printable)) > 1,
                    "print": any(x["print"] for x in variants),
                    "variants": variants,
                })
        if dishes:
            sections.append({
                "key": section["key"], "title": section["title"],
                "column": section["column"], "dishes": dishes,
                "print_note": section.get("print_note", ""),
            })

    others = [p for p in products if p["id"] not in used]
    return {"sections": sections, "others": others, "midi": source.get("midi")}


def choice_rules(source, products):
    """{id produit: règle « choose »} : choix obligatoires (viandes Duo/King, parfums…)."""
    by_name = {p["name"].strip().lower(): p["id"] for p in products}
    rules = {}
    for section in source["sections"]:
        for dish in section["dishes"]:
            for v in dish["variants"]:
                if not v.get("choose"):
                    continue
                pid = v.get("id") or by_name.get(v["product"].strip().lower())
                if pid:
                    rules[pid] = v["choose"]
    return rules


def clean_options(rule, options):
    """Vérifie les choix du client (côté serveur). ValueError si incomplets ou inconnus."""
    if not rule:
        return []
    if not isinstance(options, list):
        raise ValueError("choix manquant")
    cleaned = [str(o).strip() for o in options]
    if len(cleaned) != rule["count"]:
        raise ValueError(f"{rule['count']} choix attendus")
    if any(o not in rule["from"] for o in cleaned):
        raise ValueError("choix inconnu")
    if rule.get("distinct") and len(set(cleaned)) != len(cleaned):
        raise ValueError("choix en double")
    return sorted(cleaned)


def format_options(options):
    """['Matcha', 'Matcha', 'Coco'] → 'Coco + Matcha x2' (ticket cuisine, email)."""
    parts = []
    for option in sorted(set(options)):
        n = options.count(option)
        parts.append(f"{option} x{n}" if n > 1 else option)
    return " + ".join(parts)


def expected_products(source, files):
    out = []
    for section in source["sections"]:
        for dish in section["dishes"]:
            # plat masqué : absent de /products, ne doit être ni signalé ni recréé
            if dish.get("hidden"):
                continue
            for v in dish["variants"]:
                name = v.get("product") or dish["name"]
                out.append({
                    "id": v.get("id"),
                    "name": name,
                    "price": v["price"],
                    "category": section["key"],
                    "tva": dish.get("tva", section["tva"]),
                    "img": resolve_image(dish, v, name, files),
                    "replace_img": dish.get("replace_img", False),
                    "print": v.get("print", True),
                })
    return out


def build_plan(source, current, files):
    by_id = {p["id"]: p for p in current}
    by_name = {p["name"].strip().lower(): p for p in current}
    expected = expected_products(source, files)
    plan = {"price": [], "image": [], "create": [], "errors": []}

    ids = [e["id"] for e in expected if e["id"] is not None]
    for dup in sorted({i for i in ids if ids.count(i) > 1}):
        plan["errors"].append(f"id {dup} utilisé plusieurs fois")

    for e in expected:
        p = by_id.get(e["id"]) if e["id"] is not None else by_name.get(e["name"].strip().lower())
        if e["id"] is not None and not p:
            plan["errors"].append(f"id {e['id']} introuvable ({e['name']})")
            continue
        if not p:
            plan["create"].append(e)
            continue
        if abs(float(p["price"]) - e["price"]) > 0.001:
            plan["price"].append((p["id"], p["name"], p["price"], e["price"]))
        current_img = p.get("img") or ""
        has_real = current_img and "default" not in current_img and not current_img.startswith("http")
        # replace_img : la photo du menu remplace volontairement celle déjà en base
        if e["img"] and (not has_real or (e["replace_img"] and current_img != e["img"])):
            plan["image"].append((p["id"], p["name"], e["img"]))

    # doublons de l'onglet Végétariens : même prix que le plat de référence
    targets = {e["id"]: e for e in expected if e["id"] is not None}
    for alias, target in source.get("aliases", {}).items():
        p, t = by_id.get(int(alias)), targets.get(target)
        if p and t and abs(float(p["price"]) - t["price"]) > 0.001:
            plan["price"].append((p["id"], p["name"], p["price"], t["price"]))

    known = set(ids) | {int(a) for a in source.get("aliases", {})} | set(source.get("retired", []))
    plan["outside"] = [p for p in current if p["id"] not in known]
    plan["no_image"] = sorted({e["name"] for e in expected if not e["img"]})
    return plan


def show(plan):
    print(f"Prix à modifier : {len(plan['price'])}")
    for pid, name, old, new in plan["price"]:
        print(f"  #{pid} {name} : {old} -> {new}")
    print(f"Nouveaux plats : {len(plan['create'])}")
    for e in plan["create"]:
        print(f"  {e['category']:10} {e['price']:>6} tva={e['tva']:<4} {e['name']} [{e['img'] or 'sans image'}]")
    print(f"Images ajoutées : {len(plan['image'])}")
    for pid, name, img in plan["image"]:
        print(f"  #{pid} {name} -> {img}")
    print(f"Plats en ligne absents du menu (non modifiés) : {len(plan['outside'])}")
    for p in plan["outside"]:
        print(f"  #{p['id']} {p['name']} ({p['price']} €)")
    print(f"Plats sans photo : {len(plan['no_image'])}")
    print(f"Erreurs : {plan['errors'] or 'aucune'}")


def apply_plan(plan):
    import psycopg2
    from dotenv import load_dotenv

    load_dotenv(os.path.join(HERE, ".env"))
    url = os.environ.get("DATABASE_URL" if os.environ.get("RAILWAY_ENVIRONMENT") else "DATABASE_PUBLIC_URL")
    conn = psycopg2.connect(url, connect_timeout=15)
    try:
        with conn, conn.cursor() as cur:  # une seule transaction : tout ou rien
            for pid, _, _, new in plan["price"]:
                cur.execute("UPDATE products SET price = %s WHERE id = %s", (new, pid))
            for pid, _, img in plan["image"]:
                cur.execute("UPDATE products SET img = %s WHERE id = %s", (img, pid))
            for e in plan["create"]:
                cur.execute("""
                INSERT INTO products (name, price, category, tva, img, active, featured)
                VALUES (%s, %s, %s, %s, %s, TRUE, FALSE)
                """, (e["name"], e["price"], e["category"], e["tva"], e["img"]))
    finally:
        conn.close()


def main():
    with open(SOURCE, encoding="utf-8") as f:
        source = json.load(f)

    req = urllib.request.Request(LIVE_URL, headers={"User-Agent": "menu-sync"})
    current = json.load(urllib.request.urlopen(req, timeout=30))

    plan = build_plan(source, current, available_images())
    show(plan)

    if "--apply" not in sys.argv:
        print("\nAperçu seulement : rien n'a été écrit.")
        return
    if plan["errors"]:
        sys.exit("Erreurs dans le menu : rien n'a été écrit.")
    apply_plan(plan)
    print("\nMenu appliqué.")


if __name__ == "__main__":
    main()
