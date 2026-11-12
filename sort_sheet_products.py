"""
Version ligne de commande de l'outil « Doublons & tri » de stock.html
(la logique est dans sheet_duplicates.py).

Usage :
    python sort_sheet_products.py                 # rapport seulement (rien n'est modifié)
    python sort_sheet_products.py --apply Metro   # trie l'onglet Metro
    python sort_sheet_products.py --apply         # trie tous les onglets
    python sort_sheet_products.py --csv rapport.csv
"""

import argparse
import csv
import sys

import gspread
from oauth2client.service_account import ServiceAccountCredentials

from sheet_duplicates import analyse_tab, cross_supplier_duplicates, sort_tab, supplier_tabs

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

SHEET_ID = "1LUG-_t_H4Ol4QCUZX0lDTdQa3ISWB5m6qUDhXa4LxU0"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("tabs", nargs="*", help="onglets à traiter (défaut : tous)")
    ap.add_argument("--apply", action="store_true", help="trie réellement le Google Sheet")
    ap.add_argument("--csv", help="exporte le rapport des doublons en CSV")
    args = ap.parse_args()

    creds = ServiceAccountCredentials.from_json_keyfile_name(
        "restopi-stock-sync.json",
        ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"],
    )
    spreadsheet = gspread.authorize(creds).open_by_key(SHEET_ID)

    wanted = {t.strip().lower() for t in args.tabs}
    tabs = [ws for ws in supplier_tabs(spreadsheet)
            if not wanted or ws.title.strip().lower() in wanted]

    csv_rows = []

    for ws in tabs:
        r = analyse_tab(ws)
        if r.get("error"):
            print(f"\n== {ws.title} : {r['error']}, ignoré")
            continue

        print(f"\n{'=' * 60}\n== {ws.title} : {r['count']} produits, "
              f"{len(r['groups'])} groupe(s) à vérifier"
              f"{'' if r['is_sorted'] else ' (non trié)'}\n{'=' * 60}")

        for g_i, g in enumerate(r["groups"], 1):
            print(f"\n  [{g_i}]")
            for n in g["names"]:
                print(f"     • {n}")
                csv_rows.append([ws.title, g_i, n, g["reason"], f"{g['score']:.0%}"])
            print(f"     → {g['reason']} ({g['score']:.0%})")

        if args.apply:
            try:
                res = sort_tab(spreadsheet, ws)
                print(f"\n  ⇅ « {ws.title} » trié ({res['sorted']} lignes)")
            except ValueError as e:
                print(f"\n  ⚠️ {e}")

    if len(tabs) > 1:
        cross = cross_supplier_duplicates(spreadsheet, tabs)
        if cross:
            print(f"\n{'=' * 60}\n== Même nom chez plusieurs fournisseurs (info)\n{'=' * 60}")
            for v in cross:
                print("  • " + " | ".join(f"{x['supplier']}: {x['name']}" for x in v))

    if args.csv:
        with open(args.csv, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.writer(f, delimiter=";")
            w.writerow(["Fournisseur", "Groupe", "Produit", "Raison", "Similarité"])
            w.writerows(csv_rows)
        print(f"\nRapport CSV : {args.csv}")

    if not args.apply:
        print("\n(Rapport seulement — rien n'a été modifié. Ajoutez --apply pour trier.)")


if __name__ == "__main__":
    main()
