import gspread
from oauth2client.service_account import ServiceAccountCredentials
import json
import psycopg2
import os
import re
from dotenv import load_dotenv
from datetime import datetime
import pytz

load_dotenv()

# =========================
# DATABASE
# =========================

if os.environ.get("RAILWAY_ENVIRONMENT"):
    DATABASE_URL = os.environ.get("DATABASE_URL")
else:
    DATABASE_URL = os.environ.get("DATABASE_PUBLIC_URL")

scope = [
    "https://spreadsheets.google.com/feeds",
    "https://www.googleapis.com/auth/drive"
]

# =========================
# GOOGLE AUTH fffffff
# =========================

if os.environ.get("RAILWAY_ENVIRONMENT"):

    google_json = json.loads(
        os.environ.get(
            "GOOGLE_SERVICE_ACCOUNT_JSON"
        )
    )

    creds = ServiceAccountCredentials.from_json_keyfile_dict(
        google_json,
        scope
    )

else:

    creds = ServiceAccountCredentials.from_json_keyfile_name(
        "restopi-stock-sync.json",
        scope
    )

client = gspread.authorize(creds)

SHEET_ID = "1LUG-_t_H4Ol4QCUZX0lDTdQa3ISWB5m6qUDhXa4LxU0"

spreadsheet = client.open_by_key(SHEET_ID)

PARIS_TZ = pytz.timezone("Europe/Paris")

def extract_number(value):

    raw = str(value).strip().lower().replace(",", ".")

    match = re.search(r"[\d\.]+", raw)

    if match:
        return float(match.group())

    return 0

def parse_sheet_date(value):

    raw = str(value).strip()

    if not raw:
        return None

    # 15/11/2026 (format du Sheet), 15/11/26, 2026-11-15
    for fmt in ("%d/%m/%Y", "%d/%m/%y", "%Y-%m-%d"):
        try:
            return datetime.strptime(raw, fmt).date()
        except ValueError:
            continue

    return None


# =========================
# DATE DE CONTRÔLE PAR PRODUIT
# Chaque ligne produit a sa propre date de contrôle (colonne « Date contrôle »).
# La synchro n'applique le « Stock Réel » d'une ligne que si SA date est plus récente :
# valider 5 produits le 15/11 ne touche que ces 5 produits.
# D2 (date de l'onglet) ne sert plus que pour un onglet qui n'a pas encore la colonne.
# =========================

CONTROL_DATE_HEADER = "Date contrôle"
FIRST_DATA_ROW = 3  # ligne 1 = en-têtes, ligne 2 = dates D2/E2


def fmt_sheet_date(d):
    return d.strftime("%d/%m/%Y") if d else ""


def ensure_control_date_column(sheet, cur):
    """
    Ajoute la colonne « Date contrôle » à l'onglet si elle manque, remplie avec la date
    de contrôle actuelle de chaque produit (base RestoPi, sinon D2) : rien ne change au départ.
    Retourne True si la colonne existe (ou vient d'être créée).
    """
    values = sheet.get_all_values()
    if not values:
        return False

    headers = [str(h).strip() for h in values[0]]
    if CONTROL_DATE_HEADER in headers:
        return True
    if "Produit" not in headers:
        return False

    pcol = headers.index("Produit")
    col = max(len(r) for r in values) + 1  # après la dernière colonne utilisée
    if sheet.col_count < col:
        sheet.add_cols(col - sheet.col_count)

    tab_date = parse_sheet_date(values[1][3]) if len(values) > 1 and len(values[1]) > 3 else None  # D2

    cur.execute("""
    SELECT LOWER(TRIM(name)), stock_date FROM stock_products
    WHERE LOWER(TRIM(supplier)) = LOWER(TRIM(%s))
    """, (sheet.title,))
    db_dates = dict(cur.fetchall())

    column = [[CONTROL_DATE_HEADER]] + [[""]] * (FIRST_DATA_ROW - 2)
    for r in values[FIRST_DATA_ROW - 1:]:
        name = r[pcol].strip() if len(r) > pcol else ""
        d = (db_dates.get(name.lower()) or tab_date) if name else None
        column.append([fmt_sheet_date(d)])

    sheet.update(
        values=column,
        range_name=gspread.utils.rowcol_to_a1(1, col),
        value_input_option="RAW",  # texte « 15/11/2026 » tel quel, quelle que soit la langue du Sheet
    )
    return True

def sync_stock():

    # date du jour à Paris, recalculée à chaque synchro
    # (avant : calculée une seule fois au démarrage du serveur, en UTC)
    today = datetime.now(PARIS_TZ).date()

    conn = psycopg2.connect(DATABASE_URL)
    conn.autocommit = True
    
    with conn.cursor() as cur:
        cur.execute("""
        CREATE TABLE IF NOT EXISTS stock_snapshots (
            id SERIAL PRIMARY KEY,
            snapshot_date DATE,
            product_name TEXT,
            supplier TEXT,
            stock_quantity FLOAT,
            purchase_unit TEXT,
            created_at TIMESTAMP DEFAULT NOW()
        );
        """)
        
        
        
        cur.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS idx_stock_snapshots_unique
        ON stock_snapshots (
            snapshot_date,
            LOWER(product_name),
            LOWER(supplier)
        );
        """)

        # ⏭ produit « passé » lors de l'inventaire RestoPi de cette date (voir /admin/stock/inventory)
        cur.execute("""
        ALTER TABLE stock_products
        ADD COLUMN IF NOT EXISTS skipped_control_date DATE;
        """)

    worksheets = spreadsheet.worksheets()

    synced = 0
    sheet_products = set()


    for sheet in worksheets:

        if sheet.title.strip().lower() == "infos":
            continue

        supplier = sheet.title
        rows = sheet.get_all_records()

        # colonne « Date contrôle » : créée au besoin (une seule fois par onglet)
        has_row_dates = bool(rows) and CONTROL_DATE_HEADER in rows[0]
        if rows and not has_row_dates:
            with conn.cursor() as cur:
                has_row_dates = ensure_control_date_column(sheet, cur)
            if has_row_dates:
                rows = sheet.get_all_records()

        # D2 : date de l'onglet, seulement pour un onglet sans colonne « Date contrôle »
        tab_stock_date = parse_sheet_date(
            sheet.acell("D2").value
        )

        order_date = parse_sheet_date(
            sheet.acell("E2").value
        )

        for row in rows:

            product_name = str(
                row.get("Produit", "")
            ).strip()

            if not product_name:
                continue

            # date de contrôle de CETTE ligne (vide = jamais contrôlé → stock du Sheet non appliqué)
            stock_date = (
                parse_sheet_date(row.get(CONTROL_DATE_HEADER, ""))
                if has_row_dates
                else tab_stock_date
            )

            sheet_products.add((
                product_name.strip().lower(),
                supplier.strip().lower()
            ))

            purchase_unit = str(
                row.get("Unité achat", "")
            ).strip()

            min_stock = row.get(
                "Stock min",
                0
            ) or 0

            stock_reel = extract_number(row.get("Stock Réel", 0))
            

            ordered_quantity = extract_number(
                row.get("Commander", 0)
            )

            with conn.cursor() as cur:

                cur.execute("""
                SELECT id, stock_quantity, stock_date, skipped_control_date
                FROM stock_products
                WHERE LOWER(name)=%s
                AND LOWER(supplier)=%s
                """, (
                    product_name.strip().lower(),
                    supplier.strip().lower()
                ))

                existing = cur.fetchone()

                if existing:

                    product_id = existing[0]
                    old_stock = float(existing[1] or 0)
                    old_stock_date = existing[2]

                    new_stock = float(stock_reel or 0)

                    # Chỉ update stock_quantity nếu Google Sheet có ngày kiểm kho mới hơn
                    should_update_stock = False

                    if stock_date and not old_stock_date:
                        should_update_stock = True

                    elif stock_date and old_stock_date and stock_date > old_stock_date:
                        should_update_stock = True

                    # ⏭ passé lors de l'inventaire de cette date : D2 (date du contrôle) ne le concerne pas.
                    # Sinon la synchro le « confirme » à cette date → snapshot → semaine sans consommation.
                    if should_update_stock and existing[3] and stock_date == existing[3]:
                        should_update_stock = False

                    if should_update_stock:
                        # =========================
                        # SAVE SNAPSHOT
                        # =========================

                        cur.execute("""
                        INSERT INTO stock_snapshots
                        (
                            snapshot_date,
                            product_name,
                            supplier,
                            stock_quantity,
                            purchase_unit
                        )
                        VALUES (%s,%s,%s,%s,%s)
                        ON CONFLICT (
                            snapshot_date,
                            LOWER(product_name),
                            LOWER(supplier)
                        )
                        DO UPDATE SET
                            stock_quantity = EXCLUDED.stock_quantity,
                            purchase_unit = EXCLUDED.purchase_unit,
                            created_at = NOW()
                        """, (
                            stock_date,
                            product_name,
                            supplier,
                            new_stock,
                            purchase_unit
                        ))
                        delta = new_stock - old_stock

                        cur.execute("""
                        UPDATE stock_products
                        SET stock_quantity=%s,
                            ordered_quantity=%s,
                            stock_date=%s,
                            order_date=%s,

                            purchase_unit=%s,

                            updated_at=NOW()

                        WHERE id=%s
                        """, (
                            stock_reel,
                            ordered_quantity,
                            stock_date,
                            order_date,
                            purchase_unit,
                            product_id
                        ))

                        if delta != 0:

                            movement_type = (
                                "CONSUMPTION"
                                if delta < 0
                                else "INVENTORY_ADJUSTMENT"
                            )

                            cur.execute("""
                            INSERT INTO stock_movements
                            (
                                product_name,
                                supplier,
                                movement_type,
                                quantity,
                                total_price,
                                purchase_date,
                                purchase_unit,
                                note
                            )
                            VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
                            """, (
                                product_name,
                                supplier,
                                movement_type,
                                delta,
                                0,
                                stock_date or today,
                                purchase_unit,
                                "Nouveau contrôle stock Google Sheet"
                            ))

                    else:

                        # Même ancienne date de contrôle:
                        # update seulement commander + unité achat
                        # NE PAS écraser unité stock manager

                        cur.execute("""
                        UPDATE stock_products
                        SET ordered_quantity=%s,
                            order_date=%s,

                            purchase_unit=%s,

                            updated_at=NOW()
                        WHERE id=%s
                        """, (
                            ordered_quantity,
                            order_date,

                            purchase_unit,

                            product_id
                        ))

                else:

                    cur.execute("""
                    INSERT INTO stock_products
                    (
                        name,
                        supplier,

                        stock_quantity,
                        ordered_quantity,

                        stock_date,
                        order_date,

                        purchase_unit,
                        stock_unit,

                        min_stock
                    )
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    """, (
                        product_name,
                        supplier,

                        stock_reel,
                        ordered_quantity,

                        stock_date,
                        order_date,

                        purchase_unit,

                        purchase_unit,

                        min_stock

                    
                    ))

                    if stock_date:

                        cur.execute("""
                        INSERT INTO stock_snapshots
                        (
                            snapshot_date,
                            product_name,
                            supplier,
                            stock_quantity,
                            purchase_unit
                        )
                        VALUES (%s,%s,%s,%s,%s)
                        ON CONFLICT (
                            snapshot_date,
                            LOWER(product_name),
                            LOWER(supplier)
                        )
                        DO UPDATE SET
                            stock_quantity = EXCLUDED.stock_quantity,
                            purchase_unit = EXCLUDED.purchase_unit,
                            created_at = NOW()
                        """, (
                            stock_date,
                            product_name,
                            supplier,
                            stock_reel,
                            purchase_unit
                        ))

                synced += 1
    

   
    # =========================
    # DELETE PRODUITS ABSENTS GOOGLE SHEET
    # =========================

    with conn.cursor() as cur:

        cur.execute("""
        SELECT id, name, supplier, stock_quantity, purchase_unit, average_price
        FROM stock_products
        """)

        db_products = cur.fetchall()

        for p in db_products:

            product_id = p[0]
            name = p[1]
            supplier = p[2]
            qty = float(p[3] or 0)
            unit = p[4]
            avg_price = float(p[5] or 0)

            key = (
                name.strip().lower(),
                supplier.strip().lower()
            )

            if key not in sheet_products:

                cur.execute("""
                INSERT INTO stock_movements
                (
                    product_name,
                    supplier,
                    movement_type,
                    quantity,
                    purchase_unit,
                    total_price,
                    unit_price,
                    purchase_date,
                    note
                )
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
                """, (
                    name,
                    supplier,
                    "DELETED_FROM_SHEET",
                    -qty,
                    unit,
                    -(qty * avg_price),
                    avg_price,
                    today,
                    "Produit supprimé car absent du Google Sheet"
                ))

                cur.execute("""
                DELETE FROM stock_products
                WHERE id=%s
                """, (product_id,))

    conn.close()

    return {
        "success": True,
        "synced": synced
    }

if __name__ == "__main__":

    result = sync_stock()

    print(result)