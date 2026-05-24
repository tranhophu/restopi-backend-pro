import gspread
from oauth2client.service_account import ServiceAccountCredentials
import json
import psycopg2
import os
import re
from dotenv import load_dotenv
from datetime import datetime

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

today = datetime.now().date()

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

    try:
        return datetime.strptime(
            raw,
            "%d/%m/%Y"
        ).date()

    except:
        return None

def sync_stock():

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

    worksheets = spreadsheet.worksheets()

    synced = 0
    sheet_products = set()


    for sheet in worksheets:

        if sheet.title.strip().lower() == "infos":
            continue

        supplier = sheet.title
        rows = sheet.get_all_records()

        stock_date = parse_sheet_date(
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
                SELECT id, stock_quantity, stock_date
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