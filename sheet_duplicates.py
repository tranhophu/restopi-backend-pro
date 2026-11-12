"""
Produits en double / presque identiques dans le Google Sheet stock,
et tri alphabétique d'un onglet fournisseur.

Utilisé par :
  - app.py  → /admin/stock/similar-products et /admin/stock/sort-sheet (page stock.html)
  - sort_sheet_products.py → même chose en ligne de commande

Structure d'un onglet : ligne 1 = en-têtes, ligne 2 = dates (D2/E2),
produits à partir de la ligne 3. Les lignes 1-2 ne bougent jamais.
Le backend retrouve les produits par leur NOM, pas par numéro de ligne,
donc le tri ne casse rien côté application.
"""

import re
import unicodedata
from difflib import SequenceMatcher

import gspread

FIRST_DATA_ROW = 3  # 1-indexé
SKIP_TABS = {"infos"}
SIMILARITY_THRESHOLD = 0.82

# mots ignorés pour comparer le « fond » du nom (quantités, unités, bruit)
UNIT_WORDS = {
    "g", "gr", "kg", "mg", "l", "ml", "cl", "dl", "pce", "pces", "pc", "pcs",
    "x", "lot", "de", "du", "des", "la", "le", "les", "en", "et", "a", "au",
    "aux", "sac", "pq", "boite", "block", "mc",
}


# =========================
# NORMALISATION
# =========================

def normalize(name):
    """minuscule, sans accent, ponctuation → espace"""
    s = str(name).lower().replace("’", "'").replace("œ", "oe").replace("æ", "ae")
    s = "".join(c for c in unicodedata.normalize("NFKD", s) if not unicodedata.combining(c))
    s = re.sub(r"(\d),(\d)", r"\1.\2", s)  # 1,5kg → 1.5kg
    s = re.sub(r"[^a-z0-9.]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def sort_key(name):
    """clé de tri naturelle : 'carotte 5kg' avant 'carotte 10kg'"""
    return re.sub(
        r"\d+(?:\.\d+)?",
        lambda m: f"{float(m.group()):012.3f}",
        normalize(name),
    )


def base_words(name):
    """mots du nom sans quantités ni unités → détecte « même produit, autre taille »"""
    n = re.sub(r"\d+(?:\.\d+)?\s*[a-z]{0,3}\b", " ", normalize(name))
    return frozenset(w for w in n.split() if w not in UNIT_WORDS and len(w) > 1)


def similarity(a, b):
    na, nb = normalize(a), normalize(b)
    if na == nb:
        return 1.0, "doublon exact"

    ba, bb = base_words(a), base_words(b)
    if ba and ba == bb:
        return 0.95, "même produit, taille/format différent ?"

    ratio = SequenceMatcher(None, na, nb).ratio()

    # mots en commun (ordre indifférent : « sauce soja » / « soja sauce »)
    if ba and bb:
        jacc = len(ba & bb) / len(ba | bb)
        if jacc >= 0.75 and min(len(ba), len(bb)) >= 2:
            return max(ratio, 0.9), "mêmes mots, ordre différent ?"

    return ratio, "nom très proche"


def find_groups(names):
    """
    Regroupe les noms proches (union-find).
    Retourne [{"names": [...], "reason": str, "score": float}], trié par nom.
    """
    parent = list(range(len(names)))
    best = {}

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for i in range(len(names)):
        for j in range(i + 1, len(names)):
            score, reason = similarity(names[i], names[j])
            if score >= SIMILARITY_THRESHOLD:
                parent[find(j)] = find(i)
                best.setdefault(i, []).append((score, reason))

    groups = {}
    for i in range(len(names)):
        groups.setdefault(find(i), []).append(i)

    result = []
    for members in groups.values():
        if len(members) < 2:
            continue
        pairs = [p for i in members for p in best.get(i, [])]
        score, reason = max(pairs)
        result.append({
            "names": sorted((names[i] for i in members), key=sort_key),
            "reason": reason,
            "score": round(score, 2),
        })

    return sorted(result, key=lambda g: sort_key(g["names"][0]))


# =========================
# GOOGLE SHEET
# =========================

def supplier_tabs(spreadsheet):
    return [ws for ws in spreadsheet.worksheets() if ws.title.strip().lower() not in SKIP_TABS]


def read_tab_names(ws, values=None):
    """(index colonne Produit, [noms]) — (None, []) si pas de colonne Produit"""
    values = values if values is not None else ws.get_all_values()
    if not values:
        return None, []
    headers = [h.strip() for h in values[0]]
    if "Produit" not in headers:
        return None, []
    col = headers.index("Produit")
    names = [
        r[col].strip()
        for r in values[FIRST_DATA_ROW - 1:]
        if len(r) > col and r[col].strip()
    ]
    return col, names


def analyse_tab(ws):
    values = ws.get_all_values()
    col, names = read_tab_names(ws, values)
    if col is None:
        return {"supplier": ws.title, "error": "colonne « Produit » introuvable"}

    data_names = [
        (r[col].strip() if len(r) > col else "")
        for r in values[FIRST_DATA_ROW - 1:]
    ]
    filled = [n for n in data_names if n]

    return {
        "supplier": ws.title,
        "count": len(filled),
        "is_sorted": filled == sorted(filled, key=sort_key)
                     and all(data_names[: len(filled)]),  # lignes vides seulement à la fin
        "groups": find_groups(names),
    }


def cross_supplier_duplicates(spreadsheet, tabs=None):
    """même nom (normalisé) dans plusieurs onglets — info seulement"""
    by_norm = {}
    for ws in tabs or supplier_tabs(spreadsheet):
        _, names = read_tab_names(ws)
        for n in names:
            by_norm.setdefault(normalize(n), []).append({"supplier": ws.title, "name": n})
    cross = [v for v in by_norm.values() if len({x["supplier"] for x in v}) > 1]
    return sorted(cross, key=lambda v: sort_key(v[0]["name"]))


def _formulas_in_data(ws, n_rows, n_cols):
    """une formule (références relatives) serait faussée par le tri"""
    rng = f"A{FIRST_DATA_ROW}:{gspread.utils.rowcol_to_a1(n_rows, n_cols)}"
    cells = ws.get(rng, value_render_option="FORMULA")
    return [
        gspread.utils.rowcol_to_a1(FIRST_DATA_ROW + r_i, c_i + 1)
        for r_i, row in enumerate(cells)
        for c_i, v in enumerate(row)
        if isinstance(v, str) and v.startswith("=")
    ]


def sort_tab(spreadsheet, ws):
    """
    Trie les lignes produits (3 → fin) par nom normalisé, ordre naturel.
    1) colonne temporaire avec la clé de tri
    2) tri natif Google Sheets (la ligne entière bouge : valeurs, couleurs, notes)
    3) suppression de la colonne temporaire
    Les lignes sans nom partent à la fin.
    Retourne {"sorted": n} ou lève ValueError.
    """
    values = ws.get_all_values()
    col, _ = read_tab_names(ws, values)
    if col is None:
        raise ValueError(f"Colonne « Produit » introuvable dans « {ws.title} »")

    n_rows = len(values)
    if n_rows <= FIRST_DATA_ROW:
        return {"sorted": max(0, n_rows - FIRST_DATA_ROW + 1)}

    n_cols = max(len(r) for r in values)
    formulas = _formulas_in_data(ws, n_rows, n_cols)
    if formulas:
        raise ValueError(
            f"Tri annulé : formule(s) dans « {ws.title} » ({', '.join(formulas[:5])})"
        )

    keys = []
    for r in values[FIRST_DATA_ROW - 1:]:
        name = r[col].strip() if len(r) > col else ""
        keys.append(["1" + sort_key(name) if name else "9"])

    helper = ws.col_count  # 0-indexé : juste après la dernière colonne
    col_range = {"sheetId": ws.id, "dimension": "COLUMNS",
                 "startIndex": helper, "endIndex": helper + 1}

    spreadsheet.batch_update({"requests": [{"insertDimension": {"range": col_range}}]})
    try:
        ws.update(
            values=keys,
            range_name=gspread.utils.rowcol_to_a1(FIRST_DATA_ROW, helper + 1),
            value_input_option="RAW",
        )
        spreadsheet.batch_update({"requests": [{
            "sortRange": {
                "range": {
                    "sheetId": ws.id,
                    "startRowIndex": FIRST_DATA_ROW - 1,
                    "endRowIndex": n_rows,
                    "startColumnIndex": 0,
                    "endColumnIndex": helper + 1,
                },
                "sortSpecs": [{"dimensionIndex": helper, "sortOrder": "ASCENDING"}],
            }
        }]})
    finally:
        spreadsheet.batch_update({"requests": [{"deleteDimension": {"range": col_range}}]})

    return {"sorted": n_rows - FIRST_DATA_ROW + 1}
