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

    raw = str(value).strip().lower()

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

    worksheets = spreadsheet.worksheets()

    synced = 0

    for sheet in worksheets:
        # bỏ qua sheet Infos
        if sheet.title.strip().lower() == "Infos":
            continue
        supplier = sheet.title

        rows = sheet.get_all_records()

        stock_date = parse_sheet_date(
            sheet.acell("E2").value
        )

        order_date = parse_sheet_date(
            sheet.acell("F2").value
        )


        for row in rows:

            product_name = str(
                row.get("Produit", "")
            ).strip()

            if not product_name:
                continue

            unit = str(
                row.get("Unité", "")
            ).strip()

            min_stock = row.get(
                "Stock min",
                0
            ) or 0

            stock_reel = extract_number(
                row.get("Stock Réel", 0)
            )

            ordered_quantity = extract_number(
                row.get("Commander", 0)
            )


            with conn.cursor() as cur:

                cur.execute("""
                SELECT id
                FROM stock_products
                WHERE name=%s
                AND supplier=%s
                """, (
                    product_name,
                    supplier
                ))

                existing = cur.fetchone()

                if existing:

                    cur.execute("""
                    UPDATE stock_products
                    SET stock_quantity=%s,

                        ordered_quantity=%s,

                        stock_date=%s,
                        order_date=%s,

                        unit=%s,
                        min_stock=%s,

                        updated_at=NOW()

                    WHERE id=%s
                    """, (
                        stock_reel,

                        ordered_quantity,

                        stock_date,
                        order_date,
            

                        unit,
                        min_stock,

                        existing[0]
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
                 

                        unit,
                        min_stock
                    )
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
                    """, (
                        product_name,
                        supplier,

                        stock_reel,

                        ordered_quantity,

                        stock_date,
                        order_date,
               

                        unit,
                        min_stock
                    ))

                synced += 1

    conn.close()

    return {
        "success": True,
        "synced": synced
    }

if __name__ == "__main__":

    result = sync_stock()

    print(result)