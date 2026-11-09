from flask import Flask, request, jsonify
from flask_cors import CORS
import stripe
import os
import resend
import json
import io
import re
from datetime import datetime, timedelta
import pytz
import psycopg2
import random
from dotenv import load_dotenv
from flask import send_from_directory
from werkzeug.security import generate_password_hash, check_password_hash
import secrets
import gspread
from oauth2client.service_account import ServiceAccountCredentials
import json
from sync_stock import sync_stock
from invoice_parsers import parse_supplier_invoice
import difflib
import unicodedata

paris_tz = pytz.timezone("Europe/Paris")

load_dotenv()

if os.environ.get("RAILWAY_ENVIRONMENT"):
    # chạy trên Railway → dùng internal
    DATABASE_URL = os.environ.get("DATABASE_URL")
else:
    # chạy local → dùng public   ggggg
    DATABASE_URL = os.environ.get("DATABASE_PUBLIC_URL")

print("Using DB:", DATABASE_URL)

def get_conn():

    conn = psycopg2.connect(DATABASE_URL)

    conn.autocommit = True

    return conn

# =========================
# GOOGLE SHEET STOCK
# =========================

scope = [
    "https://spreadsheets.google.com/feeds",
    "https://www.googleapis.com/auth/drive"
]

# =========================
# GOOGLE AUTH
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

gclient = gspread.authorize(creds)

SHEET_ID = "1LUG-_t_H4Ol4QCUZX0lDTdQa3ISWB5m6qUDhXa4LxU0"

spreadsheet = gclient.open_by_key(SHEET_ID)
def get_customer_by_email(email):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT id, email, phone, pin_hash, points, reset_token, reset_token_expiry
                FROM customers
                WHERE email = %s
            """, (email.lower(),))
            return cur.fetchone()


def add_points_to_customer(email, phone, amount_eur):
    if not email:
        return

    email = email.strip().lower()
    phone = (phone or "").strip()
    points_to_add = int(amount_eur)

    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM customers WHERE email = %s", (email,))
            row = cur.fetchone()

            if row:
                cur.execute("""
                    UPDATE customers
                    SET points = points + %s,
                        phone = COALESCE(NULLIF(%s, ''), phone)
                    WHERE email = %s
                """, (points_to_add, phone, email))
            else:
                cur.execute("""
                    INSERT INTO customers (email, phone, points)
                    VALUES (%s, %s, %s)
                """, (email, phone, points_to_add))
# =========================
# CONFIG
# =========================
stripe.api_key = os.environ.get("STRIPE_SECRET_KEY")
RESEND_API_KEY = os.environ.get("RESEND_API_KEY")

ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD")
ADMIN_TOKEN = os.environ.get("ADMIN_TOKEN")

if not ADMIN_PASSWORD or not ADMIN_TOKEN:
    raise Exception("Missing ADMIN credentials in ENV")

EMAIL_FROM = "Restopi <contact@pierregroupe.com>"
EMAIL_TO = "restopi2025@gmail.com"

resend.api_key = RESEND_API_KEY

app = Flask(__name__)
CORS(app)

app.config['JSON_AS_ASCII'] = False

def save_order(order):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
            INSERT INTO orders (
                id, date, nom, prenom, tel, adresse,
                pickup_time, note, items, total, printed
            ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT (id) DO NOTHING
            """, (
                order["id"],
                order["date"],
                order["nom"],
                order["prenom"],
                order["tel"],
                order["adresse"],
                order["pickup_time"],
                order["note"],
                json.dumps(order["items"], ensure_ascii=False),
                order["total"],
                order["printed"]
            ))

# =========================
# MEMORY (FIX CRASH)
# =========================

@app.route("/")
def home():
    return "Server OK Pierre 🚀"

# =========================
# ADMIN SECURITY
# =========================
def check_admin(req):
    token = req.headers.get("Authorization")

    # 🔥 check vide
    if not token:
        print("❌ No token provided")
        return False

    # 🔥 check match
    if token != ADMIN_TOKEN:
        print("❌ Invalid token:", token)
        return False

    return True

# =========================
# PRODUCTS DATABASE
# =========================

def load_products():
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM products WHERE active = TRUE")
            rows = cur.fetchall()

    result = {}
    for row in rows:
        result[str(row[0])] = {
            "id": row[0],
            "name": row[1],
            "price": row[2],
            "category": row[3],
            "tva": row[4],
            "img": row[5],
            "active": row[6],
            "featured": row[7]
        }

    return result
@app.route("/products", methods=["GET"])
def public_products():
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM products WHERE active = TRUE")
            rows = cur.fetchall()

    result = []
    for row in rows:
        result.append({
            "id": row[0],
            "name": row[1],
            "price": row[2],
            "category": row[3],
            "tva": row[4],
            "img": row[5],
            "featured": row[7]
        })

    return jsonify(result)

def init_db():
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
            CREATE TABLE IF NOT EXISTS orders (
                id BIGINT PRIMARY KEY,
                date TEXT,
                nom TEXT,
                prenom TEXT,
                tel TEXT,
                adresse TEXT,
                pickup_time TEXT,
                note TEXT,
                items JSONB,
                total FLOAT,
                printed BOOLEAN DEFAULT FALSE
            );
            """)

init_db()

def init_products():
    with get_conn() as conn:
        with conn.cursor() as cur:

            # 1. CREATE TABLE nếu chưa có
            cur.execute("""
            CREATE TABLE IF NOT EXISTS products (
            id SERIAL PRIMARY KEY,
            name TEXT,
            price FLOAT,
            category TEXT,
            tva FLOAT,
            img TEXT,
            active BOOLEAN DEFAULT TRUE,
            featured BOOLEAN DEFAULT FALSE
            );
            """)

            # 2. ADD COLUMN featured nếu chưa có
            cur.execute("""
            DO $$
            BEGIN
                IF NOT EXISTS (
                    SELECT 1 
                    FROM information_schema.columns 
                    WHERE table_name='products' 
                    AND column_name='featured'
                ) THEN
                    ALTER TABLE products ADD COLUMN featured BOOLEAN DEFAULT FALSE;
                END IF;
            END
            $$;
            """)

init_products()

# =========================
# STOCK DATABASE
# =========================

def init_stock():

    with get_conn() as conn:
        with conn.cursor() as cur:

            # fournisseurs
            cur.execute("""
            CREATE TABLE IF NOT EXISTS stock_suppliers (
                id SERIAL PRIMARY KEY,
                name TEXT UNIQUE
            );
            """)

            # produits stock
            cur.execute("""
            CREATE TABLE IF NOT EXISTS stock_products (
                id SERIAL PRIMARY KEY,

                name TEXT,
                supplier TEXT,

                stock_quantity FLOAT DEFAULT 0,

                ordered_quantity FLOAT DEFAULT 0,
                

                stock_date DATE,
                order_date DATE,
          

                purchase_unit TEXT,

                average_price FLOAT DEFAULT 0,
                last_purchase_price FLOAT DEFAULT 0,
                min_stock FLOAT DEFAULT 0,

                updated_at TIMESTAMP DEFAULT NOW()
            );
            """)
            
            
            cur.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS idx_stock_products_unique
            ON stock_products (
                LOWER(name),
                LOWER(supplier)
            );
            """)
            # mouvements
            cur.execute("""
            CREATE TABLE IF NOT EXISTS stock_movements (
                id SERIAL PRIMARY KEY,
                product_name TEXT,
                supplier TEXT,
                movement_type TEXT,
                quantity FLOAT,
                purchase_unit TEXT,
                total_price FLOAT,
                unit_price FLOAT,
                note TEXT,
                purchase_date DATE,
                created_at TIMESTAMP DEFAULT NOW()
            );
            """)

            cur.execute("""
            ALTER TABLE stock_movements
            ADD COLUMN IF NOT EXISTS purchase_date DATE;
            """)
            cur.execute("""
            ALTER TABLE stock_products

            ADD COLUMN IF NOT EXISTS ordered_quantity FLOAT DEFAULT 0,

            ADD COLUMN IF NOT EXISTS stock_date DATE,
            ADD COLUMN IF NOT EXISTS order_date DATE,
      

            ADD COLUMN IF NOT EXISTS average_price FLOAT DEFAULT 0,
            ADD COLUMN IF NOT EXISTS last_purchase_price FLOAT DEFAULT 0;
            """)

            # =========================
            # UNIT CONVERSIONS ERP
            # =========================

            cur.execute("""
            CREATE TABLE IF NOT EXISTS stock_units (

                id SERIAL PRIMARY KEY,

                product_name TEXT,
                supplier TEXT,

                purchase_unit TEXT,
                stock_unit TEXT,

                conversion_factor FLOAT DEFAULT 1
            );
            """)

            # stock_products → unité interne

            cur.execute("""
            ALTER TABLE stock_products

            ADD COLUMN IF NOT EXISTS stock_unit TEXT;
            """)
            
            cur.execute("""
            ALTER TABLE stock_products

            ADD COLUMN IF NOT EXISTS conversion_factor FLOAT DEFAULT 1;
            """)
            # historique achat réel

            cur.execute("""
            ALTER TABLE stock_movements
            ADD COLUMN IF NOT EXISTS purchase_quantity FLOAT;
            """)

            # DLC par lot d'achat
            cur.execute("""
            ALTER TABLE stock_movements
            ADD COLUMN IF NOT EXISTS expiry_date DATE,
            ADD COLUMN IF NOT EXISTS lot_status TEXT DEFAULT 'ACTIF';
            """)
            # snapshots hebdomadaires
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
            ALTER TABLE stock_snapshots
            ADD COLUMN IF NOT EXISTS purchase_unit TEXT;
            """)
            cur.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS idx_stock_snapshots_unique
            ON stock_snapshots (
                snapshot_date,
                LOWER(product_name),
                LOWER(supplier)
            );
            """)

            # prix référence
            cur.execute("""
            CREATE TABLE IF NOT EXISTS stock_reference_prices (

                id SERIAL PRIMARY KEY,

                product_name TEXT,
                supplier TEXT,

                reference_year INTEGER,

                reference_price FLOAT,

                note TEXT,

                created_at TIMESTAMP DEFAULT NOW()
            );
            """)

            # import factures PDF : n° facture sur chaque ligne d'achat (anti-doublon)
            cur.execute("""
            ALTER TABLE stock_movements
            ADD COLUMN IF NOT EXISTS invoice_ref TEXT;
            """)

            # code article fournisseur → produit stock (mémorisé au 1er import)
            # product_name NULL = article à ignorer
            # factor = nb d'unités achat du produit pour 1 article facturé
            cur.execute("""
            CREATE TABLE IF NOT EXISTS stock_supplier_codes (
                id SERIAL PRIMARY KEY,
                supplier TEXT NOT NULL,
                code TEXT NOT NULL,
                description TEXT,
                product_name TEXT,
                factor FLOAT DEFAULT 1,
                updated_at TIMESTAMP DEFAULT NOW()
            );
            """)

            cur.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS idx_stock_supplier_codes_unique
            ON stock_supplier_codes (LOWER(supplier), code);
            """)

            # =========================
            # COÛT DES PLATS (fiches techniques)
            # =========================

            # TVA d'achat (prix stock enregistrés en TTC → HT pour le coût matière)
            cur.execute("""
            ALTER TABLE stock_products
            ADD COLUMN IF NOT EXISTS purchase_tva FLOAT DEFAULT 5.5;
            """)

            # contenu d'une unité stock (ex : 1 chai = 907 g) → fiches en g / ml
            # sans changer l'unité de comptage du stock
            cur.execute("""
            ALTER TABLE stock_products
            ADD COLUMN IF NOT EXISTS content_qty FLOAT,
            ADD COLUMN IF NOT EXISTS content_unit TEXT;
            """)

            # une fiche par plat du menu (table products)
            # portions : nb de portions produites par la recette (ex : sauce pour 20 plats)
            # extra_cost : coût HT fixe ajouté (gaz, garniture non suivie en stock…)
            cur.execute("""
            CREATE TABLE IF NOT EXISTS recipes (
                menu_product_id INTEGER PRIMARY KEY,
                portions FLOAT DEFAULT 1,
                extra_cost FLOAT DEFAULT 0,
                note TEXT,
                updated_at TIMESTAMP DEFAULT NOW()
            );
            """)

            # ingrédients : produit stock par nom + fournisseur (comme les autres tables stock,
            # les id changent quand Google Sheet recrée un produit), quantité en unité stock
            cur.execute("""
            CREATE TABLE IF NOT EXISTS recipe_ingredients (
                id SERIAL PRIMARY KEY,
                menu_product_id INTEGER NOT NULL,
                product_name TEXT NOT NULL,
                supplier TEXT NOT NULL,
                quantity FLOAT NOT NULL,
                note TEXT
            );
            """)

            cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_recipe_ingredients_menu
            ON recipe_ingredients (menu_product_id);
            """)

            # ligne = produit stock OU préparation maison ; unité de saisie (g, cl…) optionnelle
            cur.execute("""
            ALTER TABLE recipe_ingredients
            ADD COLUMN IF NOT EXISTS preparation_id INTEGER,
            ADD COLUMN IF NOT EXISTS unit TEXT;
            """)

            # préparations maison (sauce nuoc mam 8 L, bouillon…) : coût par unité de rendement
            cur.execute("""
            CREATE TABLE IF NOT EXISTS preparations (
                id SERIAL PRIMARY KEY,
                name TEXT NOT NULL,
                yield_qty FLOAT NOT NULL DEFAULT 1,
                yield_unit TEXT NOT NULL DEFAULT 'L',
                extra_cost FLOAT DEFAULT 0,
                note TEXT,
                updated_at TIMESTAMP DEFAULT NOW()
            );
            """)

            cur.execute("""
            CREATE TABLE IF NOT EXISTS preparation_ingredients (
                id SERIAL PRIMARY KEY,
                preparation_id INTEGER NOT NULL,
                product_name TEXT NOT NULL DEFAULT '',
                supplier TEXT NOT NULL DEFAULT '',
                sub_preparation_id INTEGER,
                quantity FLOAT NOT NULL,
                unit TEXT,
                note TEXT
            );
            """)

            cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_preparation_ingredients_prep
            ON preparation_ingredients (preparation_id);
            """)

            # concurrents (Viet Café, Saigon Exotique…) et leurs prix TTC par plat du menu
            cur.execute("""
            CREATE TABLE IF NOT EXISTS competitors (
                id SERIAL PRIMARY KEY,
                name TEXT NOT NULL,
                note TEXT,
                created_at TIMESTAMP DEFAULT NOW()
            );
            """)

            cur.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS idx_competitors_name
            ON competitors (LOWER(name));
            """)

            cur.execute("""
            CREATE TABLE IF NOT EXISTS competitor_prices (
                menu_product_id INTEGER NOT NULL,
                competitor_id INTEGER NOT NULL,
                price FLOAT NOT NULL,
                updated_at TIMESTAMP DEFAULT NOW(),
                PRIMARY KEY (menu_product_id, competitor_id)
            );
            """)

            # quantité vendue par le concurrent pour ce prix (ex : 4 nems) — NULL = même portion que nous
            cur.execute("""
            ALTER TABLE competitor_prices
            ADD COLUMN IF NOT EXISTS quantity FLOAT;
            """)

            # notre portion par plat (ex : 2 pce, 350 g) → comparaison à portion égale
            cur.execute("""
            CREATE TABLE IF NOT EXISTS menu_portions (
                menu_product_id INTEGER PRIMARY KEY,
                quantity FLOAT NOT NULL,
                unit TEXT NOT NULL DEFAULT 'pce',
                updated_at TIMESTAMP DEFAULT NOW()
            );
            """)

init_stock()

@app.route("/customer/set-pin", methods=["POST"])
def customer_set_pin():
    data = request.get_json()
    email = (data.get("email") or "").strip().lower()
    phone = (data.get("phone") or "").strip()
    pin = (data.get("pin") or "").strip()

    if not email:
        return jsonify({"error": "Email manquant"}), 400

    if not pin or len(pin) != 4 or not pin.isdigit():
        return jsonify({"error": "Le PIN doit contenir exactement 4 chiffres"}), 400

    pin_hash = generate_password_hash(pin)

    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id, pin_hash FROM customers WHERE email = %s", (email,))
            row = cur.fetchone()

            if row:
                cur.execute("""
                    UPDATE customers
                    SET phone = COALESCE(NULLIF(%s, ''), phone),
                        pin_hash = %s
                    WHERE email = %s
                """, (phone, pin_hash, email))
            else:
                cur.execute("""
                    INSERT INTO customers (email, phone, pin_hash, points)
                    VALUES (%s, %s, %s, 0)
                """, (email, phone, pin_hash))

    return jsonify({
        "success": True,
        "message": "PIN enregistré avec succès"
    })

@app.route("/customer/verify-pin", methods=["POST"])
def customer_verify_pin():
    data = request.get_json()
    email = (data.get("email") or "").strip().lower()
    pin = (data.get("pin") or "").strip()

    if not email or not pin:
        return jsonify({"error": "Email ou PIN manquant"}), 400

    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT pin_hash, points FROM customers WHERE email = %s", (email,))
            row = cur.fetchone()

    if not row:
        return jsonify({"error": "Client introuvable"}), 404

    pin_hash, points = row

    if not pin_hash:
        return jsonify({"error": "Aucun PIN défini pour ce client"}), 400

    if not check_password_hash(pin_hash, pin):
        return jsonify({"error": "PIN incorrect"}), 401

    return jsonify({
        "success": True,
        "points": points
    })

@app.route("/customer/redeem-points", methods=["POST"])
def customer_redeem_points():
    data = request.get_json()
    email = (data.get("email") or "").strip().lower()
    pin = (data.get("pin") or "").strip()
    points_to_use = int(data.get("points_to_use", 0))

    if not email or not pin:
        return jsonify({"error": "Email ou PIN manquant"}), 400

    if points_to_use <= 0:
        return jsonify({"error": "Nombre de points invalide"}), 400

    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT id, pin_hash, points
                FROM customers
                WHERE email = %s
            """, (email,))
            row = cur.fetchone()

            if not row:
                return jsonify({"error": "Client introuvable"}), 404

            customer_id, pin_hash, current_points = row

            if not pin_hash or not check_password_hash(pin_hash, pin):
                return jsonify({"error": "PIN incorrect"}), 401

            if current_points < points_to_use:
                return jsonify({"error": "Points insuffisants"}), 400

            new_points = current_points - points_to_use

            cur.execute("""
                UPDATE customers
                SET points = %s
                WHERE id = %s
            """, (new_points, customer_id))

    discount_eur = points_to_use / 10

    return jsonify({
        "success": True,
        "points_restants": new_points,
        "discount_eur": discount_eur
    })

@app.route("/customer/request-pin-reset", methods=["POST"])
def request_pin_reset():
    data = request.get_json()
    email = (data.get("email") or "").strip().lower()

    if not email:
        return jsonify({"error": "Email manquant"}), 400

    token = secrets.token_urlsafe(32)
    expiry = datetime.now() + timedelta(minutes=30)

    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM customers WHERE email = %s", (email,))
            row = cur.fetchone()

            if row:
                cur.execute("""
                    UPDATE customers
                    SET reset_token = %s,
                        reset_token_expiry = %s
                    WHERE email = %s
                """, (token, expiry, email))

                reset_link = f"https://aupetitvietnam.com/reset-pin.html?token={token}"

                try:
                    resend.api_key = RESEND_API_KEY
                    resend.Emails.send({
                        "from": "Au P'tit Vietnam <contact@pierregroupe.com>",
                        "to": [email],
                        "subject": "Réinitialisation de votre PIN fidélité",
                        "html": f"""
                            <p>Bonjour,</p>
                            <p>Vous avez demandé la réinitialisation de votre PIN fidélité.</p>
                            <p>
                            <a href="{reset_link}" style="display:inline-block;padding:10px 18px;background:#00cdbd;color:#fff;text-decoration:none;border-radius:8px;">
                                Réinitialiser mon PIN
                            </a>
                            </p>
                            <p>Ce lien expire dans 30 minutes.</p>
                            <p>Si vous n'êtes pas à l'origine de cette demande, ignorez cet email.</p>
                        """
                    })
                except Exception as e:
                    print("Erreur email reset PIN:", e)
                    return jsonify({"error": "Impossible d'envoyer l'email"}), 500

    return jsonify({
        "success": True,
        "message": "Si cet email existe, un lien de réinitialisation a été envoyé."
    })

@app.route("/customer/check-reset-token", methods=["POST"])
def check_reset_token():
    data = request.get_json()
    token = (data.get("token") or "").strip()

    if not token:
        return jsonify({"error": "Token manquant"}), 400

    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT email, reset_token_expiry
                FROM customers
                WHERE reset_token = %s
            """, (token,))
            row = cur.fetchone()

    if not row:
        return jsonify({"error": "Token invalide"}), 400

    email, expiry = row

    # 🔥 FIX TIMEZONE BUG
    now = datetime.now()

    if not expiry or now > expiry:
        return jsonify({"error": "Token expiré"}), 400

    return jsonify({
        "success": True,
        "email": email
    })

@app.route("/customer/reset-pin", methods=["POST"])
def reset_pin():
    data = request.get_json()
    token = (data.get("token") or "").strip()
    new_pin = (data.get("new_pin") or "").strip()

    if not token:
        return jsonify({"error": "Token manquant"}), 400

    if not new_pin or len(new_pin) != 4 or not new_pin.isdigit():
        return jsonify({"error": "Le PIN doit contenir exactement 4 chiffres"}), 400

    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT id, reset_token_expiry
                FROM customers
                WHERE reset_token = %s
            """, (token,))
            row = cur.fetchone()

            if not row:
                return jsonify({"error": "Token invalide"}), 400

            customer_id, expiry = row

            # 🔥 FIX TIMEZONE BUG
            
            now = datetime.now()

            if not expiry or now > expiry:
                return jsonify({"error": "Token expiré"}), 400

            new_pin_hash = generate_password_hash(new_pin)

            cur.execute("""
                UPDATE customers
                SET pin_hash = %s,
                    reset_token = NULL,
                    reset_token_expiry = NULL
                WHERE id = %s
            """, (new_pin_hash, customer_id))

    return jsonify({
        "success": True,
        "message": "PIN réinitialisé avec succès"
    })

@app.route("/create-checkout-session", methods=["POST"])
def create_checkout_session():
    data = request.json or {}
    items = data.get("items", [])
    client = data.get("client", {}) or {}
    points_used = int(data.get("points_used", 0) or 0)

    # ✅ normaliser client
    client["email"] = (client.get("email") or "").strip().lower()
    client["tel"] = (client.get("tel") or "").strip()

    products = load_products()

    # =========================
    # 🔥 CALCUL TOTAL
    # =========================
    total_eur = 0

    for item in items:
        product_id = int(item.get("id"))
        qty = int(item.get("qty", 1))

        product = products.get(str(product_id))
        if not product:
            continue

        total_eur += product["price"] * qty

    # 🔥 sécurité total
    if total_eur <= 0:
        return jsonify({"error": "total invalid"}), 400

    # =========================
    # 🎁 DISCOUNT
    # =========================
    # 🔥 lấy email
    email = (client.get("email") or "").strip().lower()
    pin = (data.get("pin") or "").strip()

    real_points = 0
    pin_hash = None

    if email:
        with get_conn() as conn:
   
            with conn.cursor() as cur:
                cur.execute("SELECT pin_hash, points FROM customers WHERE email = %s", (email,))
                row = cur.fetchone()
                if row:
                    pin_hash, real_points = row

    # không cho dùng quá số point thật
    points_used = min(points_used, real_points)

    # round chuẩn
    valid_points = (points_used // 100) * 100

    # ✅ nếu có dùng điểm thì bắt buộc check PIN
    if valid_points > 0:
        if not pin_hash:
            return jsonify({"error": "Aucun PIN défini pour ce client"}), 400

        if not pin:
            return jsonify({"error": "PIN manquant"}), 400

        if not check_password_hash(pin_hash, pin):
            return jsonify({"error": "PIN incorrect"}), 401

    discount_eur = (valid_points / 100) * 10

    # 🔥 LIMIT theo total
    discount_eur = min(discount_eur, total_eur - 0.5)

    if discount_eur < 0:
        discount_eur = 0

    # 🔥 LIMIT DISCOUNT
    if discount_eur >= total_eur:
        discount_eur = max(0, total_eur - 0.5)

    # =========================
    # 🧾 LINE ITEMS
    # =========================
    line_items = []

    for item in items:
        product_id = int(item.get("id"))
        qty = int(item.get("qty", 1))

        product = products.get(str(product_id))
        if not product:
            continue

        line_items.append({
            "price_data": {
                "currency": "eur",
                "product_data": {
                    "name": product["name"],
                },
                "unit_amount": int(round(product["price"] * 100)),
            },
            "quantity": qty,
        })

    # 🎁 ADD DISCOUNT
    print(f"💳 Stripe total: {total_eur}€ | Discount: {discount_eur}€")
   # 🎁 APPLY DISCOUNT DIRECTLY
    final_total = total_eur - discount_eur

    # sécurité
    if final_total <= 0:
        final_total = 0.5

    # ⚠️ Stripe veut centimes
    amount_cents = int(round(final_total * 100))

    line_items = [{
        "price_data": {
            "currency": "eur",
            "product_data": {
                "name": "Commande Restopi"
            },
            "unit_amount": amount_cents,
        },
        "quantity": 1,
    }]

    if not line_items:
        return jsonify({"error": "no valid items"}), 400

    # =========================
    # 💳 STRIPE
    # =========================
    success_url = data.get("success_url") or "https://aupetitvietnam.com/success.html"
    cancel_url = data.get("cancel_url") or "https://aupetitvietnam.com"

    # payment_method_types không còn được Stripe hỗ trợ → quản lý trong Dashboard
    session = stripe.checkout.Session.create(
        line_items=line_items,
        mode="payment",
        success_url=success_url,
        cancel_url=cancel_url,
        metadata={
            "client": json.dumps(client, ensure_ascii=False),
            "items": json.dumps(items, ensure_ascii=False),
            "points_used": str(valid_points)
        }
    )

    return jsonify({"id": session.id})

def create_order_from_webhook(items, client, stripe_total, points_used=0):
       
    nom = client.get("nom", "").strip()
    prenom = client.get("prenom", "").strip()
    tel = client.get("tel", "").strip()
    adresse = (client.get("adresse", "") + " " + client.get("city", "")).strip()
    pickup_time = client.get("pickup_datetime", "")
    note = client.get("note", "")

    now = datetime.now(paris_tz)
    order_id = int(now.timestamp() * 1000) + random.randint(0, 999)

    # ✅ TOTAL = STRIPE (QUAN TRỌNG)
    total = stripe_total

    clean_items = []

    products = load_products()
    for item in items:
      product_id = int(item.get("id"))
      qty = int(item.get("qty", 0))

      
      product = products.get(str(product_id))
      if not product or qty <= 0:
        continue

      clean_items.append({
        "id": int(product_id),
        "name": product["name"],
        "qty": qty,
        "price": product["price"]
      })

    order_data = {
        "id": order_id,
        "date": now.strftime("%d/%m/%Y %H:%M:%S"),
        "nom": nom,
        "prenom": prenom,
        "tel": tel,
        "adresse": adresse,
        "pickup_time": pickup_time,
        "note": note,
        "items": clean_items,
        "total": total,
        "printed": False
    }

    save_order(order_data)

    print("\n🔥 COMMANDE WEBHOOK 🔥")
    print(f"Commande #{order_id}")
    print(f"Client: {prenom} {nom}")
    print(f"Tel: {tel}")
    print(f"Adresse: {adresse}")
    print(f"Heure retrait: {pickup_time}")
    print(f"Note client: {note}")

    print("\n--- Produits ---")
    for item in clean_items:
        print(f"{item['name']} x {item['qty']} ({item['price']} €)")

    print(f"\nTOTAL: {total} €")
    print("====================\n")

    # =========================
    # 📧 EMAIL VIA RESEND
    # =========================
    try:
        text_content = f"""RESTOPI

Commande #{order_id}
Date: {order_data['date']}

Client: {prenom} {nom}
Tel: {tel}
Adresse: {adresse}

Heure de retrait: {pickup_time}
Note client: {note}

COMMANDE:
"""

        for item in clean_items:
            text_content += f"- {item['name']} x {item['qty']} ({item['price']} €)\n"

        text_content += f"\nTOTAL: {total} €\n"

        params = {
            "from": EMAIL_FROM,
            "to": [EMAIL_TO],
            "subject": f"Nouvelle commande #{order_id}",
            "text": text_content,
        }
        # gửi cho client
        if client.get("email"):
           resend.Emails.send({
           "from": EMAIL_FROM,
           "to": [client.get("email")],
           "subject": "Votre commande Restopi",
           "text": text_content
        })
        # gửi cho admin
        email = resend.Emails.send(params)
        print("📧 Email envoyé avec succès")
        print(email)

    except Exception as e:
        print("❌ Erreur email Resend:", e)

    # =========================
    # 🎯 LOYALTY POINTS
    # =========================

    email = client.get("email")

    if email:
        points_used = int(points_used or 0)

        # 🔥 VERIFY với DB
        with get_conn() as conn:
   
            with conn.cursor() as cur:
                cur.execute("SELECT points FROM customers WHERE email = %s", (email,))
                row = cur.fetchone()

                real_points = row[0] if row else 0

        # không cho dùng quá số point thật
        if points_used > real_points:
            points_used = real_points

        # luôn round đúng
        points_used = (points_used // 100) * 100

        # 🔥 LIMIT SECURITY
        if points_used < 0:
            points_used = 0

        if points_used > 10000:
            points_used = 0

        # 🔥 tính lại total gốc
        original_total = total + (points_used / 100 * 10)

        points_earned = int(original_total)
        with get_conn() as conn:
    
            with conn.cursor() as cur:
                # check customer tồn tại chưa
                cur.execute("SELECT points FROM customers WHERE email = %s", (email,))
                row = cur.fetchone()

                if row:
                    new_points = row[0] + points_earned - points_used
                    if new_points < 0:
                        new_points = 0
                    cur.execute("UPDATE customers SET points = %s WHERE email = %s",
                                (new_points, email))
                else:
                    cur.execute("INSERT INTO customers (email, points) VALUES (%s, %s)",
                                (email, points_earned))

        print(f"🎁 Points ajoutés: {points_earned}")

@app.route("/next-order", methods=["GET"])
def next_order():
    with get_conn() as conn:
   
        with conn.cursor() as cur:
            cur.execute("""
            SELECT * FROM orders
            WHERE printed = FALSE
            ORDER BY id ASC
            LIMIT 1
            """)
            row = cur.fetchone()

            if not row:
                return jsonify({"message": "no order"})

            items = row[8]
            if isinstance(items, str):
                items = json.loads(items)

            return jsonify({
                "id": row[0],
                "date": row[1],
                "nom": row[2],
                "prenom": row[3],
                "tel": row[4],
                "adresse": row[5],
                "pickup_time": row[6],
                "note": row[7],
                "items": items,
                "total": row[9],
                "printed": row[10]
            })

@app.route("/last-order", methods=["GET"])
def last_order():
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
            SELECT * FROM orders
            ORDER BY id DESC
            LIMIT 1
            """)
            row = cur.fetchone()

    if not row:
        return jsonify({"message": "no order"})

    items = row[8]
    if isinstance(items, str):
        items = json.loads(items)

    return jsonify({
        "id": row[0],
        "date": row[1],
        "nom": row[2],
        "prenom": row[3],
        "tel": row[4],
        "adresse": row[5],
        "pickup_time": row[6],
        "note": row[7],
        "items": items,
        "total": row[9],
        "printed": row[10]
    })

@app.route("/orders-history", methods=["GET"])
def orders_history():
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
            SELECT * FROM orders
            ORDER BY id DESC
            LIMIT 20
            """)
            rows = cur.fetchall()

    result = []
    for row in rows:
        items = row[8]
        if isinstance(items, str):
            items = json.loads(items)

        result.append({
            "id": row[0],
            "date": row[1],
            "nom": row[2],
            "prenom": row[3],
            "tel": row[4],
            "adresse": row[5],
            "pickup_time": row[6],
            "note": row[7],
            "items": items,
            "total": row[9],
            "printed": row[10]
        })

    return jsonify(result)

@app.route("/mark-printed", methods=["POST"])
def mark_printed():
    data = request.json
    order_id = data.get("id")

    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
            UPDATE orders SET printed = TRUE WHERE id = %s
            """, (order_id,))

    return jsonify({"status": "ok"})

@app.route("/contact", methods=["POST"])
def contact():
    data = request.json or {}

    name = data.get("name", "")
    email = data.get("email", "")
    message = data.get("message", "")

    try:
        content = f"""
NOUVEAU MESSAGE CLIENT

Nom: {name}
Email: {email}

Message:
{message}
"""

        params = {
            "from": EMAIL_FROM,
            "to": [EMAIL_TO],
            "subject": f"Contact site - {name}",
            "text": content,
        }

        resend.Emails.send(params)

        return jsonify({"status": "ok"})

    except Exception as e:
        print("Erreur contact:", e)
        return jsonify({"status": "error"}), 500

@app.route("/webhook", methods=["POST"])
def stripe_webhook():
    payload = request.data
    sig_header = request.headers.get("Stripe-Signature")
    endpoint_secret = os.environ.get("STRIPE_WEBHOOK_SECRET")
    
    if not endpoint_secret:
        print("⚠️ Missing webhook secret")
        return "", 400

    try:
        event = stripe.Webhook.construct_event(
            payload, sig_header, endpoint_secret
        )
    except Exception as e:
        print("Webhook error:", e)
        return "", 400

    if event["type"] == "checkout.session.completed":
        session = event["data"]["object"]
        metadata = session["metadata"]

        items_raw = metadata["items"] if "items" in metadata else "[]"
        client_raw = metadata["client"] if "client" in metadata else "{}"
        points_raw = metadata["points_used"] if "points_used" in metadata else "0"

        items = json.loads(items_raw)
        client = json.loads(client_raw)
        points_used = int(points_raw or 0)
        print("💰 PAIEMENT CONFIRMÉ")

        amount_total = session["amount_total"] / 100
        create_order_from_webhook(items, client, amount_total, points_used)


    return "", 200
# =========================
# LOGIN ADMIN
# =========================
@app.route("/admin/login", methods=["POST"])
def admin_login():
    data = request.json or {}
    password = data.get("password")

    if not password:
        return jsonify({"error": "missing password"}), 400

    if password != ADMIN_PASSWORD:
        print("❌ Wrong admin password attempt")
        return jsonify({"error": "wrong password"}), 403

    print("✅ Admin login success")
    return jsonify({"token": ADMIN_TOKEN})

# =========================
# ADMIN ROUTES
# =========================
@app.route("/admin/products", methods=["GET"])
def get_products():
    if not check_admin(request):
        return jsonify({"error": "unauthorized"}), 403

    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM products")
            rows = cur.fetchall()

    result = []
    for row in rows:
        result.append({
            "id": row[0],
            "name": row[1],
            "price": row[2],
            "category": row[3],
            "tva": row[4],
            "img": row[5],
            "featured": row[7]
        })

    return jsonify(result)

@app.route("/admin/orders", methods=["GET"])
def get_orders():
    if not check_admin(request):
        return jsonify({"error": "unauthorized"}), 403

    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
            SELECT * FROM orders
            ORDER BY id DESC
            """)
            rows = cur.fetchall()

    result = []
    for row in rows:
        items = row[8]
        if isinstance(items, str):
            items = json.loads(items)

        result.append({
            "id": row[0],
            "date": row[1],
            "nom": row[2],
            "prenom": row[3],
            "tel": row[4],
            "adresse": row[5],
            "pickup_time": row[6],
            "note": row[7],
            "items": items,
            "total": row[9],
            "printed": row[10]
        })

    return jsonify(result)

@app.route("/admin/stats", methods=["GET"])
def get_stats():
    if not check_admin(request):
        return jsonify({"error": "unauthorized"}), 403

    today = datetime.now(paris_tz).strftime("%d/%m/%Y")

    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM orders")
            rows = cur.fetchall()

    total_today = 0
    orders_today = 0
    product_count = {}

    for row in rows:
        order_date = datetime.strptime(row[1], "%d/%m/%Y %H:%M:%S")

        if order_date.strftime("%d/%m/%Y") == today:
            orders_today += 1
            total_today += row[9]

            items = row[8]
            if isinstance(items, str):
                items = json.loads(items)

            for item in items:
                name = item["name"]
                qty = item["qty"]
                product_count[name] = product_count.get(name, 0) + qty

    top_products = sorted(product_count.items(), key=lambda x: x[1], reverse=True)[:5]

    return jsonify({
        "total_today": total_today,
        "orders_today": orders_today,
        "top_products": top_products
    })

@app.route("/admin/products/update", methods=["POST"])
def update_product():
    if not check_admin(request):
        return jsonify({"error": "unauthorized"}), 403

    data = request.json
    product_id = str(data.get("id"))

    with get_conn() as conn:
        with conn.cursor() as cur:

            # 🔥 lấy category cũ
            cur.execute("SELECT category FROM products WHERE id = %s", (product_id,))
            old = cur.fetchone()
            category = data.get("category", old[0] if old else None)

            # 🔥 lấy img cũ
            cur.execute("SELECT img FROM products WHERE id = %s", (product_id,))
            old_img = cur.fetchone()

            img = data.get("img")

            # 🔥 nếu frontend gửi rỗng → giữ ảnh cũ
            if not img and old_img:
                img = old_img[0]

            # 🔥 lấy tva cũ
            cur.execute("SELECT tva FROM products WHERE id = %s", (product_id,))
            old_tva = cur.fetchone()

            tva = data.get("tva")

            # 🔥 FIX QUAN TRỌNG
            if tva == "" or tva is None:
                tva = old_tva[0] if old_tva else 10

            # 🔥 UPDATE
            cur.execute("""
            UPDATE products
            SET name = %s, price = %s, category = %s, tva = %s, img = %s, featured = %s
            WHERE id = %s
            """, (
                data.get("name"),
                float(data.get("price")),
                category,
                float(tva),   # ✅ FIX CHÍNH
                img,
                data.get("featured", False),
                product_id
            ))

    return jsonify({"status": "ok"})

@app.route("/admin/products/create", methods=["POST"])
def create_product():
    if not check_admin(request):
        return jsonify({"error": "unauthorized"}), 403

    data = request.json

    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
            INSERT INTO products (name, price, category, tva, img, active, featured)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            """, (
                data.get("name"),
                float(data.get("price")),
                data.get("category"),
                float(data.get("tva") or 10),
                data.get("img"),
                True,
                data.get("featured", False)
            ))

    return jsonify({"status": "created"})

@app.route("/admin/products/delete", methods=["POST"])
def delete_product():
    if not check_admin(request):
        return jsonify({"error": "unauthorized"}), 403

    data = request.json
    product_id = str(data.get("id"))

    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM products WHERE id = %s", (product_id,))

        return jsonify({"success": True})

    except Exception as e:
        print("DELETE ERROR:", e)
        return jsonify({"error": "delete failed"}), 500

@app.route("/admin/images", methods=["GET"])
def get_images():
    if not check_admin(request):
        return jsonify({"error": "unauthorized"}), 403

    folder = "images"

    try:
        files = os.listdir(folder)
        images = [f for f in files if f.lower().endswith((".png", ".jpg", ".jpeg", ".webp"))]
        return jsonify(images)
    except:
        return jsonify([])
    
@app.route('/images/<path:filename>')
def get_image(filename):
    return send_from_directory('images', filename)

@app.route("/top-products", methods=["GET"])
def top_products():

    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM orders")
            rows = cur.fetchall()

    product_count = {}

    for row in rows:
        items = row[8]

        if isinstance(items, str):
            items = json.loads(items)

        for item in items:
            product_id = item.get("id")
            qty = item.get("qty", 0)

            # 🔥 nếu order cũ không có id → bỏ qua
            if not product_id:
                continue

            product_count[product_id] = product_count.get(product_id, 0) + qty

    top = sorted(product_count.items(), key=lambda x: x[1], reverse=True)

    top_ids = [str(p[0]) for p in top[:5]]

    return jsonify(top_ids)

def init_customers():
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
            CREATE TABLE IF NOT EXISTS customers (
                id SERIAL PRIMARY KEY,
                email TEXT UNIQUE NOT NULL,
                phone TEXT,
                pin_hash TEXT,
                points INTEGER DEFAULT 0,
                reset_token TEXT,
                reset_token_expiry TIMESTAMP,
                created_at TIMESTAMP DEFAULT NOW()
            );
            """)

            cur.execute("""
            ALTER TABLE customers
            ADD COLUMN IF NOT EXISTS phone TEXT,
            ADD COLUMN IF NOT EXISTS pin_hash TEXT,
            ADD COLUMN IF NOT EXISTS points INTEGER DEFAULT 0,
            ADD COLUMN IF NOT EXISTS reset_token TEXT,
            ADD COLUMN IF NOT EXISTS reset_token_expiry TIMESTAMP,
            ADD COLUMN IF NOT EXISTS created_at TIMESTAMP DEFAULT NOW();
            """)

init_customers()

@app.route("/points", methods=["POST"])
def get_points():
    data = request.json
    email = data.get("email")

    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT points FROM customers WHERE email = %s", (email,))
            row = cur.fetchone()

    return jsonify({
        "points": row[0] if row else 0
    })

@app.route("/pending-orders", methods=["GET"])
def pending_orders():
    limit = request.args.get("limit", 20, type=int)

    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
            SELECT * FROM orders
            WHERE printed = FALSE
            ORDER BY id ASC
            LIMIT %s
            """, (limit,))
            rows = cur.fetchall()

    result = []
    for row in rows:
        items = row[8]
        if isinstance(items, str):
            items = json.loads(items)

        result.append({
            "id": row[0],
            "date": row[1],
            "nom": row[2],
            "prenom": row[3],
            "tel": row[4],
            "adresse": row[5],
            "pickup_time": row[6],
            "note": row[7],
            "items": items,
            "total": row[9],
            "printed": row[10]
        })

    return jsonify(result)

# =========================
# STOCK API
# =========================

@app.route("/admin/stock/products", methods=["GET"])
def stock_products():

    if not check_admin(request):
        return jsonify({"error": "unauthorized"}), 403

    with get_conn() as conn:
        with conn.cursor() as cur:

            cur.execute("""
            SELECT id,
                name,
                supplier,

                stock_quantity,

                ordered_quantity,

                stock_date,
                order_date,

                purchase_unit,
                stock_unit,
                conversion_factor,

                average_price,
                last_purchase_price,

                min_stock
            FROM stock_products
            ORDER BY name ASC
            """)

            rows = cur.fetchall()

    result = []

    for r in rows:
        result.append({

            "id": r[0],
            "name": r[1],
            "supplier": r[2],

            "stock_quantity": r[3],

            "ordered_quantity": r[4],

            "stock_date": str(r[5]) if r[5] else "",
            "order_date": str(r[6]) if r[6] else "",

            "purchase_unit": r[7],
            "stock_unit": r[8],
            "conversion_factor": r[9],

            "average_price": r[10],
            "last_purchase_price": r[11],
            "min_stock": r[12]

        })

    return jsonify(result)


def _create_stock_products(items, fail_if_exists=False):
    """
    Crée des produits : ligne dans l'onglet fournisseur de Google Sheet (source of truth),
    synchronisation, puis unité stock + conversion en base (pas de colonne dans Google Sheet).

    items : [{name, supplier, purchase_unit, stock_unit, conversion_factor,
              min_stock?, stock_quantity?}]
    Retourne (noms créés, message d'erreur ou None).
    Un produit qui existe déjà est ignoré (ou erreur si fail_if_exists).
    """
    prepared = []
    rows_by_supplier = {}

    with get_conn() as conn:
        with conn.cursor() as cur:
            for it in items:
                name = (it.get("name") or "").strip()
                purchase_unit = (it.get("purchase_unit") or "").strip()
                stock_unit = (it.get("stock_unit") or purchase_unit).strip()

                try:
                    factor = float(it.get("conversion_factor") or 1)
                except (TypeError, ValueError):
                    factor = 0

                if purchase_unit.lower() == stock_unit.lower():
                    factor = 1

                if not name or not (it.get("supplier") or "").strip():
                    return [], "Produit et fournisseur obligatoires"

                if not purchase_unit:
                    return [], f"« {name} » : unité achat obligatoire"

                if factor <= 0:
                    return [], f"« {name} » : conversion invalide (nombre > 0)"

                # nom exact de l'onglet (casse / accents ignorés)
                supplier, _ = _canonical_supplier(cur, it["supplier"].strip())

                cur.execute("""
                SELECT 1 FROM stock_products
                WHERE LOWER(TRIM(name)) = LOWER(TRIM(%s))
                AND LOWER(TRIM(supplier)) = LOWER(TRIM(%s))
                """, (name, supplier))

                if cur.fetchone():
                    if fail_if_exists:
                        return [], f"« {name} » existe déjà chez {supplier}"
                    continue

                prepared.append((name, supplier, stock_unit, factor))
                rows_by_supplier.setdefault(supplier, []).append([
                    name,                                       # Produit
                    purchase_unit,                              # Unité achat
                    float(it.get("min_stock") or 0),            # Stock min
                    float(it.get("stock_quantity") or 0),       # Stock Réel
                    "",                                         # Commander
                    "Créé depuis RestoPi"                       # Note
                ])

    if not prepared:
        return [], None

    try:
        sheets = {}
        for supplier in rows_by_supplier:
            try:
                sheets[supplier] = spreadsheet.worksheet(supplier)
            except gspread.exceptions.WorksheetNotFound:
                return [], (
                    f"Onglet « {supplier} » introuvable dans Google Sheet : "
                    f"créez-le (mêmes colonnes que les autres fournisseurs) puis réessayez"
                )

        for supplier, rows in rows_by_supplier.items():
            sheets[supplier].append_rows(rows)

        sync_stock()
    except Exception as e:
        return [], f"Création Google Sheet impossible : {e}"

    missing = []

    with get_conn() as conn:
        with conn.cursor() as cur:
            for name, supplier, stock_unit, factor in prepared:
                cur.execute("""
                UPDATE stock_products
                SET stock_unit = %s,
                    conversion_factor = %s,
                    updated_at = NOW()
                WHERE LOWER(TRIM(name)) = LOWER(TRIM(%s))
                AND LOWER(TRIM(supplier)) = LOWER(TRIM(%s))
                """, (stock_unit, factor, name, supplier))

                if cur.rowcount == 0:
                    missing.append(name)

    if missing:
        return [], (
            f"Ajouté(s) dans Google Sheet mais pas encore synchronisé(s) : {', '.join(missing)}. "
            f"Cliquez « 🔄 Actualiser Google Sheet » puis complétez unité stock / conversion"
        )

    return [name for name, _, _, _ in prepared], None


@app.route("/admin/stock/create", methods=["POST"])
def create_stock_product():

    if not check_admin(request):
        return jsonify({"error": "unauthorized"}), 403

    data = request.json or {}

    created, error = _create_stock_products([{
        "name": data.get("name"),
        "supplier": data.get("supplier"),
        "purchase_unit": data.get("purchase_unit") or data.get("unit"),
        "stock_unit": data.get("stock_unit"),
        "conversion_factor": data.get("conversion_factor"),
        "min_stock": data.get("min_stock"),
        "stock_quantity": data.get("stock_quantity"),
    }], fail_if_exists=True)

    if error:
        return jsonify({"error": error}), 400

    return jsonify({"success": True, "created": created})


@app.route("/admin/stock/rename", methods=["POST"])
def rename_stock_product():
    """
    Renomme un produit partout : Google Sheet (source of truth, sinon la prochaine
    synchro supprimerait le produit), puis toutes les tables qui référencent le nom.
    Body : {supplier, old_name, new_name}
    """

    if not check_admin(request):
        return jsonify({"error": "unauthorized"}), 403

    data = request.json or {}

    old_name = (data.get("old_name") or "").strip()
    new_name = (data.get("new_name") or "").strip()

    if not old_name or not new_name or not (data.get("supplier") or "").strip():
        return jsonify({"error": "Fournisseur, ancien et nouveau nom obligatoires"}), 400

    if old_name == new_name:
        return jsonify({"success": True, "new_name": new_name})

    conn = get_conn()
    conn.autocommit = False

    try:
        with conn.cursor() as cur:

            supplier, _ = _canonical_supplier(cur, data["supplier"].strip())

            cur.execute("""
            SELECT name FROM stock_products
            WHERE LOWER(TRIM(name)) = LOWER(TRIM(%s))
            AND LOWER(TRIM(supplier)) = LOWER(TRIM(%s))
            """, (old_name, supplier))

            row = cur.fetchone()

            if not row:
                return jsonify({"error": f"« {old_name} » introuvable chez {supplier}"}), 404

            old_db_name = row[0]

            # le nouveau nom ne doit pas exister (sauf simple changement de casse)
            if new_name.lower() != old_name.lower():
                cur.execute("""
                SELECT 1 FROM stock_products
                WHERE LOWER(TRIM(name)) = LOWER(TRIM(%s))
                AND LOWER(TRIM(supplier)) = LOWER(TRIM(%s))
                """, (new_name, supplier))

                if cur.fetchone():
                    return jsonify({"error": f"« {new_name} » existe déjà chez {supplier}"}), 400

            # 1. base (transaction non validée tant que Google Sheet n'est pas à jour)
            cur.execute("""
            UPDATE stock_products SET name = %s, updated_at = NOW()
            WHERE LOWER(TRIM(name)) = LOWER(TRIM(%s))
            AND LOWER(TRIM(supplier)) = LOWER(TRIM(%s))
            """, (new_name, old_db_name, supplier))

            for table in (
                "stock_movements",
                "stock_units",
                "stock_snapshots",
                "stock_reference_prices",
                "stock_supplier_codes",
                "recipe_ingredients",
                "preparation_ingredients",
            ):
                cur.execute(f"""
                UPDATE {table} SET product_name = %s
                WHERE LOWER(TRIM(product_name)) = LOWER(TRIM(%s))
                AND LOWER(TRIM(supplier)) = LOWER(TRIM(%s))
                """, (new_name, old_db_name, supplier))

        # 2. Google Sheet
        try:
            sheet = spreadsheet.worksheet(supplier)
        except gspread.exceptions.WorksheetNotFound:
            conn.rollback()
            return jsonify({"error": f"Onglet « {supplier} » introuvable dans Google Sheet"}), 400

        headers = [h.strip() for h in sheet.row_values(1)]

        if "Produit" not in headers:
            conn.rollback()
            return jsonify({"error": f"Colonne « Produit » introuvable dans l'onglet « {supplier} »"}), 400

        col = headers.index("Produit") + 1
        target = old_db_name.strip().lower()

        rows = [
            i + 1
            for i, value in enumerate(sheet.col_values(col))
            if i > 0 and str(value).strip().lower() == target
        ]

        if not rows:
            conn.rollback()
            return jsonify({
                "error": f"« {old_db_name} » introuvable dans l'onglet « {supplier} » de Google Sheet"
            }), 400

        for r in rows:
            sheet.update_cell(r, col, new_name)

        conn.commit()

    except Exception as e:
        conn.rollback()
        return jsonify({"error": f"Renommage impossible : {e}"}), 500

    finally:
        conn.close()

    return jsonify({"success": True, "new_name": new_name, "supplier": supplier})


def _sheet_number(v):
    v = float(v or 0)
    return int(v) if v.is_integer() else v


def _push_stock_to_sheet(changes):
    """
    Écrit dans Google Sheet les valeurs modifiées depuis la page stock.
    changes : [(supplier, product_name, {"Stock Réel": 12, "Stock min": 5})]
    Retourne la liste des erreurs (la base est déjà enregistrée).
    """
    errors = []
    by_supplier = {}

    for supplier, name, fields in changes:
        by_supplier.setdefault(supplier, []).append((name, fields))

    for supplier, items in by_supplier.items():
        try:
            try:
                sheet = spreadsheet.worksheet(supplier)
            except gspread.exceptions.WorksheetNotFound:
                errors.append(f"Onglet « {supplier} » introuvable dans Google Sheet")
                continue

            headers = [str(h).strip() for h in sheet.row_values(1)]

            if "Produit" not in headers:
                errors.append(f"Colonne « Produit » introuvable dans l'onglet « {supplier} »")
                continue

            # ligne Google Sheet de chaque produit (nom insensible à la casse)
            rows = {}
            for i, value in enumerate(sheet.col_values(headers.index("Produit") + 1)):
                key = str(value).strip().lower()
                if i > 0 and key:
                    rows.setdefault(key, i + 1)

            cells = []

            for name, fields in items:
                row = rows.get(name.strip().lower())

                if not row:
                    errors.append(f"« {name} » introuvable dans l'onglet « {supplier} »")
                    continue

                for header, value in fields.items():
                    if header not in headers:
                        errors.append(f"Colonne « {header} » introuvable dans l'onglet « {supplier} »")
                        continue

                    cells.append({
                        "range": gspread.utils.rowcol_to_a1(row, headers.index(header) + 1),
                        "values": [[_sheet_number(value)]]
                    })

            if cells:
                sheet.batch_update(cells, value_input_option="USER_ENTERED")

        except Exception as e:
            errors.append(f"Onglet « {supplier} » : {e}")

    return errors


def _save_stock_products(updates):
    """
    Enregistre les modifications du tableau Stock en base, puis reporte
    Stock Réel / Stock min dans Google Sheet (source of truth : sans cela,
    la prochaine synchro pourrait remettre l'ancienne valeur).
    Le nom et le fournisseur ne sont pas modifiés ici (renommage : /admin/stock/rename).
    """
    sheet_changes = []

    with get_conn() as conn:
        with conn.cursor() as cur:

            for p in updates:

                product_id = int(p.get("id"))
                stock_quantity = float(p.get("stock_quantity") or 0)
                min_stock = float(p.get("min_stock") or 0)

                cur.execute("""
                SELECT name, supplier, stock_quantity, min_stock
                FROM stock_products
                WHERE id = %s
                """, (product_id,))

                old = cur.fetchone()

                if not old:
                    continue

                cur.execute("""
                UPDATE stock_products
                SET stock_quantity=%s,
                    stock_unit=%s,
                    conversion_factor=%s,
                    min_stock=%s,
                    updated_at=NOW()
                WHERE id=%s
                """, (
                    stock_quantity,
                    p.get("stock_unit"),
                    float(p.get("conversion_factor") or 1),
                    min_stock,
                    product_id
                ))

                fields = {}

                if abs(float(old[2] or 0) - stock_quantity) > 1e-9:
                    fields["Stock Réel"] = stock_quantity

                if abs(float(old[3] or 0) - min_stock) > 1e-9:
                    fields["Stock min"] = min_stock

                if fields:
                    sheet_changes.append((old[1], old[0], fields))

    return _push_stock_to_sheet(sheet_changes) if sheet_changes else []


@app.route("/admin/stock/update", methods=["POST"])
def update_stock_product():

    if not check_admin(request):
        return jsonify({"error": "unauthorized"}), 403

    sheet_errors = _save_stock_products([request.json or {}])

    return jsonify({"success": True, "sheet_errors": sheet_errors})


@app.route(
    "/admin/stock/bulk-update",
    methods=["POST"]
)
def bulk_update_stock_products():

    if not check_admin(request):
        return jsonify({
            "error":"unauthorized"
        }), 403

    data = request.json or {}

    sheet_errors = _save_stock_products(data.get("updates", []))

    return jsonify({
        "success": True,
        "sheet_errors": sheet_errors
    })

@app.route("/admin/stock/delete", methods=["POST"])
def delete_stock_product():

    if not check_admin(request):
        return jsonify({"error": "unauthorized"}), 403

    data = request.json

    product_id = int(data.get("id"))

    with get_conn() as conn:
        with conn.cursor() as cur:

            # lấy produit
            cur.execute("""
            SELECT name,
                supplier,
                stock_quantity,
                purchase_unit,
                average_price
            FROM stock_products
            WHERE id=%s
            """, (product_id,))

            row = cur.fetchone()

            if not row:
                return jsonify({"error":"not found"}), 404

            name = row[0]
            supplier = row[1]
            sheet = spreadsheet.worksheet(supplier)

            all_rows = sheet.get_all_values()

            for idx, r in enumerate(all_rows):

                if len(r) == 0:
                    continue

                if r[0].strip() == name:

                    sheet.delete_rows(idx + 1)
                    sync_stock()

                    break
            qty = float(row[2])
            unit = row[3]
            unit_price = float(row[4])

            # historique annulation
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
                note
            )
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
            """, (
                name,
                supplier,
                "ANNULATION",
                -qty,
                unit,
                -(qty * unit_price),
                unit_price,
                "Suppression produit"
            ))

            # delete produit
            cur.execute("""
            DELETE FROM stock_products
            WHERE id=%s
            """, (product_id,))

    return jsonify({"success": True})


@app.route("/admin/stock/history", methods=["GET"])
def stock_history():

    if not check_admin(request):
        return jsonify({"error": "unauthorized"}), 403

    search = request.args.get("search", "").lower()

    with get_conn() as conn:
        with conn.cursor() as cur:

            cur.execute("""
            SELECT product_name,
                supplier,
                movement_type,
                quantity,
                purchase_unit,
                total_price,
                unit_price,
                note,
                purchase_date,
                created_at,
                expiry_date,
                COALESCE(lot_status, 'ACTIF'),
                id
            FROM stock_movements
            ORDER BY created_at DESC
            """)

            rows = cur.fetchall()

    result = []

    for r in rows:

        row_text = f"""
        {r[0]} {r[1]} {r[2]}
        {r[3]} {r[5]} {r[8]}
        """.lower()

        if search and search not in row_text:
            continue

        result.append({
            "product_name": r[0],
            "supplier": r[1],
            "movement_type": r[2],
            "quantity": r[3],
            "purchase_unit": r[4],
            "total_price": r[5],
            "unit_price": r[6],
            "note": r[7],
            "purchase_date": str(r[8]) if r[8] else None,
            "created_at": str(r[9]) if r[9] else None,
            "expiry_date": r[10].isoformat() if r[10] else None,
            "lot_status": r[11],
            "id": r[12]
        })

    return jsonify(result)


@app.route("/admin/stock/delete-movement", methods=["POST"])
def stock_delete_movement():
    """
    Supprime une ou plusieurs lignes d'historique (saisie erronée).
    Body : {"id": 12} ou {"ids": [12, 13, ...]}
    Pour les ACHAT, recalcule prix moyen + dernier prix d'achat des produits concernés.
    """

    if not check_admin(request):
        return jsonify({"error":"unauthorized"}), 403

    data = request.json or {}

    ids = data.get("ids")
    if ids is None and data.get("id") is not None:
        ids = [data.get("id")]

    try:
        ids = [int(i) for i in (ids or [])]
    except (TypeError, ValueError):
        return jsonify({"error":"invalid ids"}), 400

    if not ids:
        return jsonify({"error":"no ids"}), 400

    with get_conn() as conn:
        with conn.cursor() as cur:

            cur.execute("""
            DELETE FROM stock_movements
            WHERE id = ANY(%s)
            RETURNING product_name, supplier, movement_type
            """, (ids,))

            rows = cur.fetchall()

            if not rows:
                return jsonify({"error":"not found"}), 404

            # un seul recalcul par produit
            achat_products = {
                (r[0], r[1]) for r in rows if r[2] == "ACHAT"
            }

            for product_name, supplier in achat_products:
                _refresh_product_prices(cur, product_name, supplier)

    return jsonify({"success": True, "deleted": len(rows)})


def _refresh_product_prices(cur, product_name, supplier):
    """Recalcule prix moyen (12 mois) + dernier prix d'achat depuis l'historique ACHAT."""

    cur.execute("""
    SELECT stock_unit, conversion_factor
    FROM stock_products
    WHERE LOWER(TRIM(name)) = LOWER(TRIM(%s))
    AND LOWER(TRIM(supplier)) = LOWER(TRIM(%s))
    """, (product_name, supplier))

    product = cur.fetchone()

    if not product:
        return

    new_avg = _weighted_avg_price(
        cur,
        product_name,
        supplier,
        product[0],
        product[1]
    )

    cur.execute("""
    SELECT unit_price
    FROM stock_movements
    WHERE movement_type = 'ACHAT'
    AND LOWER(TRIM(product_name)) = LOWER(TRIM(%s))
    AND LOWER(TRIM(supplier)) = LOWER(TRIM(%s))
    AND COALESCE(unit_price, 0) > 0
    ORDER BY COALESCE(purchase_date, created_at::date) DESC, id DESC
    LIMIT 1
    """, (product_name, supplier))

    last = cur.fetchone()

    cur.execute("""
    UPDATE stock_products
    SET average_price=%s,
        last_purchase_price=%s,
        updated_at=NOW()
    WHERE LOWER(TRIM(name)) = LOWER(TRIM(%s))
    AND LOWER(TRIM(supplier)) = LOWER(TRIM(%s))
    """, (
        new_avg,
        float(last[0]) if last else 0,
        product_name,
        supplier
    ))

@app.route("/admin/stock/dashboard")
def stock_dashboard():

    if not check_admin(request):
        return jsonify({"error":"unauthorized"}), 403

    with get_conn() as conn:
        with conn.cursor() as cur:

            # total produits
            cur.execute("""
            SELECT COUNT(*)
            FROM stock_products
            """)

            total_products = cur.fetchone()[0]

            # stock faible
            cur.execute("""
            SELECT COUNT(*)
            FROM stock_products
            WHERE stock_quantity <= min_stock
            """)

            low_stock = cur.fetchone()[0]

            # valeur stock
            cur.execute("""
            SELECT COALESCE(

                SUM(

                    CASE

                        WHEN conversion_factor IS NOT NULL
                        AND conversion_factor > 0

                        THEN
                            stock_quantity
                            *
                            (
                                last_purchase_price
                                /
                                conversion_factor
                            )

                        ELSE
                            stock_quantity
                            *
                            last_purchase_price

                    END

                ),

            0)

            FROM stock_products
            """)

            stock_value = cur.fetchone()[0]

            # fournisseurs
            cur.execute("""
            SELECT COUNT(DISTINCT supplier)
            FROM stock_products
            """)

            suppliers = cur.fetchone()[0]

            # =========================
            # VALEUR STOCK PAR FOURNISSEUR
            # =========================

            cur.execute("""
            SELECT
                supplier,

                COALESCE(
                    SUM(
                        CASE

                            WHEN conversion_factor IS NOT NULL
                            AND conversion_factor > 0

                            THEN
                                stock_quantity
                                *
                                (
                                    last_purchase_price
                                    /
                                    conversion_factor
                                )

                            ELSE
                                stock_quantity
                                *
                                last_purchase_price

                        END
                    ),
                    0
                ) as supplier_value

            FROM stock_products

            GROUP BY supplier

            ORDER BY supplier_value DESC
            """)

            supplier_values_rows = cur.fetchall()

            supplier_values = []

            for r in supplier_values_rows:

                supplier_values.append({

                    "supplier": r[0],

                    "value": float(r[1] or 0)

                })
            # =========================
            # PRODUITS DORMANTS
            # =========================

            cur.execute("""
            SELECT COUNT(*)
            FROM stock_products sp

            WHERE NOT EXISTS (

                SELECT 1
                FROM stock_movements sm

                WHERE sm.product_name = sp.name
                AND sm.supplier = sp.supplier

                AND sm.created_at >=
                    NOW() - INTERVAL '30 days'

                AND sm.movement_type IN (
                    'CONSUMPTION',
                    'ACHAT'
                )
            )
            """)

            dormant_products =cur.fetchone()[0]

    return jsonify({

        "total_products": total_products,

        "low_stock": low_stock,

        "stock_value": float(stock_value),

        "suppliers": suppliers,

        "dormant_products":
            dormant_products,

        "supplier_values":
            supplier_values

    })

@app.route("/admin/stock/reference-prices")
def get_reference_prices():

    if not check_admin(request):
        return jsonify({
            "error":"unauthorized"
        }), 403

    year = int(
        request.args.get("year", 2025)
    )

    with get_conn() as conn:
        with conn.cursor() as cur:

            cur.execute("""
            SELECT
                product_name,
                supplier,
                reference_price
            FROM stock_reference_prices
            WHERE reference_year=%s
            """, (year,))

            rows = cur.fetchall()

    result = {}

    for r in rows:

        key = f"{r[0]}__{r[1]}"

        result[key] = r[2]

    return jsonify(result)

@app.route(
    "/admin/stock/save-reference",
    methods=["POST"]
)
def save_reference_price():

    if not check_admin(request):
        return jsonify({
            "error":"unauthorized"
        }), 403

    data = request.json

    with get_conn() as conn:
        with conn.cursor() as cur:

            cur.execute("""
            DELETE FROM stock_reference_prices
            WHERE product_name=%s
            AND supplier=%s
            AND reference_year=%s
            """, (
                data["product_name"],
                data["supplier"],
                int(data["year"])
            ))

            cur.execute("""
            INSERT INTO stock_reference_prices
            (
                product_name,
                supplier,
                reference_year,
                reference_price
            )
            VALUES (%s,%s,%s,%s)
            """, (
                data["product_name"],
                data["supplier"],
                int(data["year"]),
                float(data["price"])
            ))

    return jsonify({
        "success":True
    })

def _weighted_avg_price(cur, product_name, supplier, stock_unit, conversion_factor, months=12):
    """
    Prix moyen pondéré par la quantité, en unité stock,
    calculé sur les achats des `months` derniers mois.
    """
    cur.execute("""
    SELECT COALESCE(purchase_quantity, quantity),
           purchase_unit,
           unit_price
    FROM stock_movements
    WHERE movement_type = 'ACHAT'
    AND LOWER(TRIM(product_name)) = LOWER(TRIM(%s))
    AND LOWER(TRIM(supplier)) = LOWER(TRIM(%s))
    AND COALESCE(purchase_date, created_at::date) >= CURRENT_DATE - (%s * INTERVAL '1 month')
    AND COALESCE(unit_price, 0) > 0
    """, (product_name, supplier, months))

    total_cost = 0
    total_qty = 0

    for qty, unit, price in cur.fetchall():
        qty = float(qty or 0)
        if qty <= 0:
            continue
        factor = _lot_conversion_factor(unit, stock_unit, conversion_factor)
        total_cost += qty * float(price)
        total_qty += qty * factor

    if total_qty <= 0:
        return 0

    return total_cost / total_qty


def _invoice_already_imported(cur, supplier, invoice_ref):
    """
    Facture déjà enregistrée pour ce fournisseur ?
    - n° exact dans la colonne invoice_ref
    - anciennes lignes sans invoice_ref : « Facture <n°> » dans la note, n° complet
      (« Facture 12 » ne correspond plus à « Facture 123 »)
    """
    ref = (invoice_ref or "").strip()

    if not ref:
        return False

    # échappe les caractères spéciaux pour l'expression régulière PostgreSQL
    escaped = re.sub(r"([^0-9A-Za-z])", r"\\\1", ref)

    cur.execute("""
    SELECT 1
    FROM stock_movements
    WHERE LOWER(TRIM(supplier)) = LOWER(TRIM(%s))
    AND (
        TRIM(invoice_ref) = %s
        OR (
            COALESCE(TRIM(invoice_ref), '') = ''
            AND note ~* %s
        )
    )
    LIMIT 1
    """, (supplier, ref, r"Facture " + escaped + r"([^0-9A-Za-z]|$)"))

    return cur.fetchone() is not None


@app.route("/admin/stock/check-duplicates", methods=["POST"])
def check_purchase_duplicates():
    """
    Achats saisis à la main : y a-t-il déjà un achat du même produit
    (même fournisseur) à ± `days` jours ?
    Body : {deliveries: [{product_name, supplier, purchase_date}], days?: 3}
    Retour : {duplicates: [{index, matches: [{date, quantity, purchase_unit, unit_price, invoice_ref}]}]}
    """

    if not check_admin(request):
        return jsonify({"error": "unauthorized"}), 403

    data = request.json or {}
    days = int(data.get("days") or 3)
    duplicates = []

    with get_conn() as conn:
        with conn.cursor() as cur:
            for index, d in enumerate(data.get("deliveries") or []):
                name = (d.get("product_name") or "").strip()
                supplier = (d.get("supplier") or "").strip()
                date = d.get("purchase_date") or None

                if not name or not supplier or not date:
                    continue

                cur.execute("""
                SELECT COALESCE(purchase_date, created_at::date) AS d,
                       COALESCE(purchase_quantity, quantity),
                       purchase_unit,
                       unit_price,
                       invoice_ref
                FROM stock_movements
                WHERE movement_type = 'ACHAT'
                AND LOWER(TRIM(product_name)) = LOWER(TRIM(%s))
                AND LOWER(TRIM(supplier)) = LOWER(TRIM(%s))
                AND COALESCE(purchase_date, created_at::date)
                    BETWEEN %s::date - %s AND %s::date + %s
                ORDER BY d DESC
                LIMIT 5
                """, (name, supplier, date, days, date, days))

                matches = [
                    {
                        "date": r[0].isoformat() if r[0] else "",
                        "quantity": float(r[1] or 0),
                        "purchase_unit": r[2] or "",
                        "unit_price": float(r[3] or 0),
                        "invoice_ref": r[4] or ""
                    }
                    for r in cur.fetchall()
                ]

                if matches:
                    duplicates.append({"index": index, "matches": matches})

    return jsonify({"duplicates": duplicates})


@app.route("/admin/stock/add-deliveries", methods=["POST"])
def add_deliveries():

    if not check_admin(request):
        return jsonify({"error":"unauthorized"}), 403

    data = request.json

    deliveries = data.get("deliveries", [])

    saved = 0
    skipped = []
    skipped_invoices = []

    # nouveaux produits demandés depuis « Achats fournisseurs » (article de facture inconnu)
    to_create = {}

    for d in deliveries:
        cp = d.get("create_product")

        if not cp or not (d.get("product_name") or "").strip():
            continue

        key = (
            d["product_name"].strip().lower(),
            (d.get("supplier") or "").strip().lower()
        )

        to_create.setdefault(key, {
            "name": d["product_name"],
            "supplier": d.get("supplier"),
            "purchase_unit": d.get("purchase_unit"),
            "stock_unit": cp.get("stock_unit"),
            "conversion_factor": cp.get("conversion_factor"),
        })

    created_products = []

    if to_create:
        created_products, error = _create_stock_products(list(to_create.values()))

        if error:
            return jsonify({"success": False, "error": error}), 400

    with get_conn() as conn:
        with conn.cursor() as cur:

            # factures déjà enregistrées (import PDF ou achats) → ignorées entièrement
            already_imported = set()

            for supplier, ref in {
                ((d.get("supplier") or "").strip(), (d.get("invoice_ref") or "").strip())
                for d in deliveries
                if (d.get("invoice_ref") or "").strip()
            }:
                if _invoice_already_imported(cur, supplier, ref):
                    already_imported.add((supplier.lower(), ref))
                    skipped_invoices.append(ref)

            for d in deliveries:

                product_name = d.get("product_name")
                supplier = d.get("supplier")

                if (
                    (supplier or "").strip().lower(),
                    (d.get("invoice_ref") or "").strip()
                ) in already_imported:
                    continue

                quantity = float(
                    d.get("quantity") or 0
                )

                unit_price = float(
                    d.get("unit_price") or 0
                )

                purchase_date = d.get(
                    "purchase_date"
                )

                expiry_date = d.get("expiry_date") or None

                cur.execute("""
                SELECT stock_quantity,
                        average_price,
                        last_purchase_price,
                        purchase_unit,
                        stock_unit,
                        conversion_factor
                FROM stock_products
                WHERE LOWER(TRIM(name)) = LOWER(TRIM(%s))
                AND LOWER(TRIM(supplier)) = LOWER(TRIM(%s))
                """, (
                    product_name,
                    supplier
                ))

                row = cur.fetchone()

                if not row:
                    # produit/fournisseur introuvable → signalé au frontend
                    skipped.append(f"{product_name} ({supplier})")
                    continue

                last_price = float(row[2] or 0)

                price_variation = 0

                if last_price > 0:
                    price_variation = (
                        (
                            unit_price - last_price
                        ) / last_price
                    ) * 100
                purchase_unit = d.get("purchase_unit")
                invoice_ref = (d.get("invoice_ref") or "").strip()

                # quantité négative sur facture = retour / avoir
                movement_type = "RETOUR" if quantity < 0 else "ACHAT"

                stock_added = quantity

                unit = purchase_unit

                conversion_factor = row[5] or 1

                if purchase_unit == row[4]:
                    conversion_factor = 1

                # =========================
                # NO STOCK UPDATE
                # Livraison = historique achat uniquement
                # =========================

                cur.execute("""
                INSERT INTO stock_movements
                (
                    product_name,
                    supplier,

                    movement_type,

                    quantity,

                    purchase_quantity,
                    purchase_unit,

                    total_price,
                    unit_price,

                    purchase_date,
                    expiry_date,
                    invoice_ref,
                    note
                )
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                """, (

                    product_name,
                    supplier,

                    movement_type,

                    stock_added,

                    quantity,
                    purchase_unit,

                    quantity * unit_price,
                    unit_price,

                    purchase_date,
                    expiry_date,
                    invoice_ref or None,

                    f"""{"Facture " + invoice_ref + " (achats fournisseurs)" if invoice_ref else "Livraison ERP"}

Achat:
{quantity} {purchase_unit}

Ajout stock:
{stock_added} {unit}

Variation prix:
{round(price_variation, 2)}%
"""
                ))

                saved += 1

                # ligne de facture : mémorise code article → produit (+ facteur)
                # pour reconnaître automatiquement les prochaines factures
                invoice_code = (d.get("invoice_code") or "").strip()

                try:
                    invoice_qty = float(d.get("invoice_qty") or 0)
                except (TypeError, ValueError):
                    invoice_qty = 0

                if invoice_ref and invoice_code and invoice_qty and quantity / invoice_qty > 0:
                    cur.execute("""
                    INSERT INTO stock_supplier_codes
                    (supplier, code, description, product_name, factor, updated_at)
                    VALUES (%s,%s,%s,%s,%s,NOW())
                    ON CONFLICT (LOWER(supplier), code)
                    DO UPDATE SET
                        description = EXCLUDED.description,
                        product_name = EXCLUDED.product_name,
                        factor = EXCLUDED.factor,
                        updated_at = NOW()
                    """, (
                        supplier,
                        invoice_code,
                        d.get("invoice_desc") or "",
                        product_name,
                        round(quantity / invoice_qty, 6)
                    ))

                # TVA d'achat lue sur la facture → mémorisée pour le coût HT des plats
                try:
                    purchase_tva = float(d.get("purchase_tva"))
                except (TypeError, ValueError):
                    purchase_tva = None

                if purchase_tva is not None and 0 <= purchase_tva <= 30:
                    cur.execute("""
                    UPDATE stock_products SET purchase_tva = %s
                    WHERE LOWER(TRIM(name)) = LOWER(TRIM(%s))
                    AND LOWER(TRIM(supplier)) = LOWER(TRIM(%s))
                    """, (purchase_tva, product_name, supplier))

                if movement_type == "RETOUR":
                    continue  # un retour ne change pas le prix d'achat

                if invoice_ref:
                    # facture : plusieurs dates possibles dans le lot →
                    # dernier prix = achat le plus récent par date
                    _refresh_product_prices(cur, product_name, supplier)
                    continue

                # prix moyen pondéré (12 derniers mois, en unité stock)
                new_avg = _weighted_avg_price(
                    cur,
                    product_name,
                    supplier,
                    row[4],
                    row[5]
                )

                cur.execute("""
                UPDATE stock_products
                SET average_price=%s,
                    last_purchase_price=%s,
                    updated_at=NOW()
                WHERE LOWER(TRIM(name)) = LOWER(TRIM(%s))
                AND LOWER(TRIM(supplier)) = LOWER(TRIM(%s))
                """, (
                    new_avg,
                    unit_price,
                    product_name,
                    supplier
                ))

    return jsonify({
        "success": True,
        "saved": saved,
        "skipped": skipped,
        "skipped_invoices": sorted(set(skipped_invoices)),
        "created_products": created_products
    })

# =========================
# DLC / LOTS
# =========================

def _lot_conversion_factor(lot_unit, stock_unit, conversion_factor):
    # même règle que add_deliveries : unité achat == unité stock → facteur 1
    if (lot_unit or "").strip().lower() == (stock_unit or "").strip().lower():
        return 1
    return float(conversion_factor or 1) or 1


def _remaining_by_lot(lots, stock_quantity, stock_date, stock_unit, conversion_factor):
    """
    Estime la quantité restante de chaque lot (FIFO) à partir du dernier inventaire.

    - Lots achetés APRÈS le dernier inventaire : pas encore comptés → entiers.
    - Lots achetés AVANT/LE JOUR de l'inventaire : le stock compté est attribué
      aux lots les plus récents d'abord, les plus anciens sont considérés consommés.

    lots : liste de dict {id, purchase_date, purchase_quantity, purchase_unit}
    Retourne {lot_id: quantité restante en unité d'achat}
    """
    remaining = {}
    counted = []

    for lot in lots:
        factor = _lot_conversion_factor(lot["purchase_unit"], stock_unit, conversion_factor)
        qty_stock_unit = float(lot["purchase_quantity"] or 0) * factor

        if stock_date is None or (lot["purchase_date"] and lot["purchase_date"] > stock_date):
            remaining[lot["id"]] = qty_stock_unit / factor
        else:
            counted.append((lot, qty_stock_unit, factor))

    left = max(float(stock_quantity or 0), 0)

    # plus récent d'abord
    counted.sort(key=lambda x: (x[0]["purchase_date"] or datetime.min.date(), x[0]["id"]), reverse=True)

    for lot, qty_stock_unit, factor in counted:
        allocated = min(left, qty_stock_unit)
        left -= allocated
        remaining[lot["id"]] = allocated / factor

    return remaining


def _active_lots(cur, limit_date=None):
    """
    Lots d'achat ACTIFS ayant une DLC, avec quantité restante estimée (FIFO).
    limit_date : si fourni, seuls les lots dont la DLC <= limit_date sont retournés.
    """
    today = datetime.now(paris_tz).date()

    if limit_date is None:
        cur.execute("""
        SELECT DISTINCT LOWER(TRIM(product_name)), LOWER(TRIM(supplier))
        FROM stock_movements
        WHERE movement_type = 'ACHAT'
        AND COALESCE(lot_status, 'ACTIF') = 'ACTIF'
        AND expiry_date IS NOT NULL
        """)
    else:
        cur.execute("""
        SELECT DISTINCT LOWER(TRIM(product_name)), LOWER(TRIM(supplier))
        FROM stock_movements
        WHERE movement_type = 'ACHAT'
        AND COALESCE(lot_status, 'ACTIF') = 'ACTIF'
        AND expiry_date IS NOT NULL
        AND expiry_date <= %s
        """, (limit_date,))

    candidates = cur.fetchall()

    result = []

    for name_key, supplier_key in candidates:

        cur.execute("""
        SELECT stock_quantity,
               stock_date,
               stock_unit,
               conversion_factor
        FROM stock_products
        WHERE LOWER(TRIM(name)) = %s
        AND LOWER(TRIM(supplier)) = %s
        """, (name_key, supplier_key))

        product = cur.fetchone()

        cur.execute("""
        SELECT id,
               product_name,
               supplier,
               COALESCE(purchase_date, created_at::date),
               COALESCE(purchase_quantity, quantity),
               purchase_unit,
               expiry_date,
               unit_price
        FROM stock_movements
        WHERE movement_type = 'ACHAT'
        AND COALESCE(lot_status, 'ACTIF') = 'ACTIF'
        AND LOWER(TRIM(product_name)) = %s
        AND LOWER(TRIM(supplier)) = %s
        """, (name_key, supplier_key))

        lots = [
            {
                "id": r[0],
                "product_name": r[1],
                "supplier": r[2],
                "purchase_date": r[3],
                "purchase_quantity": r[4],
                "purchase_unit": r[5],
                "expiry_date": r[6],
                "unit_price": r[7],
            }
            for r in cur.fetchall()
        ]

        if product:
            remaining = _remaining_by_lot(
                lots,
                product[0],
                product[1],
                product[2],
                product[3]
            )
            stock_date = product[1]
        else:
            remaining = {
                lot["id"]: float(lot["purchase_quantity"] or 0)
                for lot in lots
            }
            stock_date = None

        for lot in lots:

            if not lot["expiry_date"]:
                continue

            if limit_date is not None and lot["expiry_date"] > limit_date:
                continue

            left = remaining.get(lot["id"], 0)

            result.append({
                "id": lot["id"],
                "product_name": lot["product_name"],
                "supplier": lot["supplier"],
                "purchase_date": lot["purchase_date"].isoformat() if lot["purchase_date"] else None,
                "purchase_quantity": float(lot["purchase_quantity"] or 0),
                "remaining_quantity": round(left, 2),
                "purchase_unit": lot["purchase_unit"],
                "unit_price": float(lot["unit_price"] or 0),
                "expiry_date": lot["expiry_date"].isoformat(),
                "days_left": (lot["expiry_date"] - today).days,
                "stock_date": stock_date.isoformat() if stock_date else None,
            })

    result.sort(key=lambda x: x["expiry_date"])

    return result


@app.route("/admin/stock/expiring", methods=["GET"])
def stock_expiring():

    if not check_admin(request):
        return jsonify({"error":"unauthorized"}), 403

    days = int(request.args.get("days", 3))

    limit_date = datetime.now(paris_tz).date() + timedelta(days=days)

    with get_conn() as conn:
        with conn.cursor() as cur:
            lots = _active_lots(cur, limit_date)

    # lots considérés déjà consommés (FIFO) → pas d'alerte
    return jsonify([l for l in lots if l["remaining_quantity"] > 0])


@app.route("/admin/stock/lots", methods=["GET"])
def stock_lots():
    """Tous les lots actifs avec DLC (y compris ceux estimés épuisés)."""

    if not check_admin(request):
        return jsonify({"error":"unauthorized"}), 403

    with get_conn() as conn:
        with conn.cursor() as cur:
            lots = _active_lots(cur)

    return jsonify(lots)


@app.route("/admin/stock/price-history", methods=["GET"])
def stock_price_history():
    """
    Historique des prix d'achat par produit (24 derniers mois),
    normalisés en € par unité stock pour être comparables entre livraisons.
    Clé : "name__supplier" (même format que reference-prices).
    """

    if not check_admin(request):
        return jsonify({"error":"unauthorized"}), 403

    months = int(request.args.get("months", 24))

    today = datetime.now(paris_tz).date()
    avg_from = today - timedelta(days=365)

    with get_conn() as conn:
        with conn.cursor() as cur:

            cur.execute("""
            SELECT LOWER(TRIM(name)),
                   LOWER(TRIM(supplier)),
                   name,
                   supplier,
                   stock_unit,
                   conversion_factor
            FROM stock_products
            """)

            products = {
                (r[0], r[1]): {
                    "name": r[2],
                    "supplier": r[3],
                    "stock_unit": r[4],
                    "conversion_factor": r[5],
                }
                for r in cur.fetchall()
            }

            cur.execute("""
            SELECT LOWER(TRIM(product_name)),
                   LOWER(TRIM(supplier)),
                   COALESCE(purchase_date, created_at::date) AS d,
                   COALESCE(purchase_quantity, quantity),
                   purchase_unit,
                   unit_price
            FROM stock_movements
            WHERE movement_type = 'ACHAT'
            AND COALESCE(unit_price, 0) > 0
            AND COALESCE(purchase_date, created_at::date) >= CURRENT_DATE - (%s * INTERVAL '1 month')
            ORDER BY d ASC, id ASC
            """, (months,))

            rows = cur.fetchall()

    result = {}

    for name_key, supplier_key, d, qty, unit, price in rows:

        product = products.get((name_key, supplier_key))

        if not product:
            continue

        factor = _lot_conversion_factor(
            unit,
            product["stock_unit"],
            product["conversion_factor"]
        )

        key = f"{product['name']}__{product['supplier']}"

        entry = result.setdefault(key, {
            "stock_unit": product["stock_unit"] or "",
            "points": [],
            "_cost": 0.0,
            "_qty": 0.0,
        })

        qty = float(qty or 0)
        price = float(price)

        entry["points"].append({
            "date": d.isoformat(),
            "unit_price": round(price, 4),
            "purchase_unit": unit,
            "quantity": qty,
            "price_per_stock_unit": round(price / factor, 4),
        })

        if d >= avg_from and qty > 0:
            entry["_cost"] += qty * price
            entry["_qty"] += qty * factor

    for entry in result.values():
        entry["avg_12m"] = (
            round(entry["_cost"] / entry["_qty"], 4)
            if entry["_qty"] > 0 else None
        )
        del entry["_cost"]
        del entry["_qty"]

    return jsonify(result)


@app.route("/admin/stock/lot-status", methods=["POST"])
def stock_lot_status():

    if not check_admin(request):
        return jsonify({"error":"unauthorized"}), 403

    data = request.json

    lot_id = int(data.get("id"))
    status = (data.get("status") or "").upper()

    if status not in ("ACTIF", "CONSOMME", "JETE"):
        return jsonify({"error":"invalid status"}), 400

    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
            UPDATE stock_movements
            SET lot_status = %s
            WHERE id = %s
            AND movement_type = 'ACHAT'
            """, (status, lot_id))

    return jsonify({"success":True})

@app.route("/admin/stock/parse-invoice", methods=["POST"])
def parse_invoice():

    if not check_admin(request):
        return jsonify({"error": "unauthorized"}), 403

    if 'file' not in request.files:
        return jsonify({"error": "Aucun fichier fourni"}), 400

    file = request.files['file']
    if not file.filename.lower().endswith('.pdf'):
        return jsonify({"error": "Le fichier doit être un PDF"}), 400

    try:
        import pdfplumber
        pdf_bytes = file.read()
        result = _parse_invoice_pdf(pdf_bytes)
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": f"Erreur parsing: {str(e)}"}), 500


def _parse_invoice_pdf(pdf_bytes):
    import pdfplumber

    result = {
        "supplier": "",
        "invoice_number": "",
        "invoice_date": "",
        "total": 0.0,
        "items": []
    }

    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        full_text = ""
        all_tables = []
        for page in pdf.pages:
            text = page.extract_text() or ""
            full_text += text + "\n"
            tables = page.extract_tables()
            if tables:
                all_tables.extend(tables)

    # Supplier name: line before "Adresse:"
    m = re.search(r'([^\n]+)\nAdresse\s*:', full_text)
    if m:
        result['supplier'] = m.group(1).strip()

    # Invoice number
    m = re.search(r'Facture\s+(E?\w+)', full_text)
    if m:
        result['invoice_number'] = m.group(1)

    # Date DD/MM/YYYY → YYYY-MM-DD
    m = re.search(r'Date\s*[:\s]+(\d{2}/\d{2}/\d{4})', full_text)
    if m:
        d = m.group(1).split('/')
        result['invoice_date'] = f"{d[2]}-{d[1]}-{d[0]}"

    # Total TTC
    m = re.search(r'Total\s+T\.T\.C\s+€?([\d]+[,\.][\d]+)', full_text)
    if m:
        result['total'] = float(m.group(1).replace(',', '.'))

    # Try table-based item extraction first
    items = _parse_items_from_tables(all_tables)
    if not items:
        items = _parse_items_from_text(full_text)

    result['items'] = items
    return result


def _parse_items_from_tables(tables):
    items = []
    for table in tables:
        for row in table:
            if not row or not row[0]:
                continue
            cell = str(row[0]).strip()
            if 'SKU:' not in cell:
                continue

            name_parts = []
            sku = ''
            for line in cell.split('\n'):
                line = line.strip()
                sku_m = re.match(r'SKU:\s*(\d+)', line)
                if sku_m:
                    sku = sku_m.group(1)
                elif line.startswith('Variante:') or not line:
                    pass
                else:
                    name_parts.append(line)

            name = ' '.join(name_parts).strip()
            qty = 0
            unit_price = 0.0

            for val in row[1:]:
                if not val:
                    continue
                clean = str(val).strip().replace('€', '').replace(',', '.').strip()
                try:
                    num = float(clean)
                    if not qty and float(clean) == int(float(clean)):
                        qty = int(float(clean))
                    elif qty and not unit_price:
                        unit_price = num
                except (ValueError, TypeError):
                    pass

            if name and qty:
                items.append({
                    'name': name,
                    'sku': sku,
                    'qty': qty,
                    'unit_price': unit_price,
                    'total': round(qty * unit_price, 2)
                })
    return items


def _parse_items_from_text(full_text):
    items = []
    lines = [l.strip() for l in full_text.split('\n')]
    in_items = False
    current = None

    for line in lines:
        if not line:
            continue

        if re.match(r'Articles\s+Quantit', line, re.IGNORECASE):
            in_items = True
            current = {'name_parts': []}
            continue

        if not in_items:
            continue

        if re.match(r'(Remise|Total Produits|Expédition|Total T)', line, re.IGNORECASE):
            break

        sku_m = re.match(r'SKU:\s*(\d+)', line)
        if sku_m and current is not None:
            current['sku'] = sku_m.group(1)
            current['name'] = ' '.join(current['name_parts']).strip()
            continue

        if line.startswith('Variante:'):
            continue

        qp_m = re.match(r'^(\d+)\s+€?([\d\.]+)\s+€?([\d\.]+)$', line)
        if qp_m and current is not None and current.get('sku'):
            items.append({
                'name': current.get('name', ''),
                'sku': current.get('sku', ''),
                'qty': int(qp_m.group(1)),
                'unit_price': float(qp_m.group(2)),
                'total': float(qp_m.group(3))
            })
            current = {'name_parts': []}
            continue

        if current is not None and 'sku' not in current:
            current['name_parts'].append(line)

    return items


# =========================
# IMPORT FACTURES PDF (historique achats)
# =========================

def _norm_product_name(s):
    s = str(s or "").replace("Œ", "OE").replace("œ", "oe")
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c)).upper()
    s = re.sub(r"[^A-Z0-9/.,%]+", " ", s)
    s = re.sub(r"\b(\d+)GR\b", r"\1G", s)
    s = re.sub(r"\b(\d+(?:[.,]\d+)?)K\b", r"\1KG", s)   # Metro : "10K" = 10 kg
    # "800 ML" → "800ML" pour comparer les contenances
    s = re.sub(r"\b(\d+(?:[.,]\d+)?)\s+(ML|CL|L|G|KG|MM|CM|M)\b", r"\1\2", s)
    return re.sub(r"\s+", " ", s).strip(" .")


# mots trop courants pour départager deux produits
_MATCH_STOPWORDS = {
    "DE", "DU", "DES", "LA", "LE", "LES", "AU", "AUX", "ET", "EN", "A", "L", "D",
    "MC", "C1", "FR", "FRANCE", "ES", "ESPAGNE", "ALLEMAGNE", "DEALLEMAGNE", "ITALIE", "CHINE",
    "VN", "CH", "PRIX", "KG", "LOT", "PCE", "PC",
}


def _match_tokens(norm):
    return {
        t for t in re.split(r"[\s/.,]+", norm)
        if t and t not in _MATCH_STOPWORDS and (len(t) > 1 or t.isdigit())
    }


def _similarity(a, b):
    """a, b : noms normalisés. Mélange ressemblance de la chaîne et mots communs."""
    seq = difflib.SequenceMatcher(None, a, b).ratio()

    ta, tb = _match_tokens(a), _match_tokens(b)
    if not ta or not tb:
        return seq

    common = ta & tb
    # mots partiellement communs ("CREV" ↔ "CREVETTES", "PLT" ↔ "POULET" non)
    partial = sum(
        1 for x in ta - common
        if len(x) >= 4 and any(y.startswith(x) or x.startswith(y) for y in tb if len(y) >= 4)
    )
    overlap = (len(common) + 0.7 * partial) / min(len(ta), len(tb))

    score = max(seq, 0.85 * min(overlap, 1.0))

    # contenances / poids différents (800ML ≠ 250ML) → produit différent
    measure = re.compile(r"^\d+(?:\.\d+)?(?:ML|CL|L|G|KG)$")
    na = {t for t in ta if measure.match(t)}
    nb = {t for t in tb if measure.match(t)}
    if na and nb:
        score *= 0.6 + 0.4 * len(na & nb) / min(len(na), len(nb))

    return score


def _pack_factor(invoice_desc, product_name, conversion_factor=1):
    """
    Devine le nb d'unités achat du produit pour 1 article facturé
    à partir des conditionnements "6/50P" :
      facture "NEM CREVETTE 6/50P"  / produit "NEM CREVETTE 50P"      → 6
      facture "SILACHA 516G"        / produit "SILACHA 12/516G"       → 1/12
        (seulement si le produit est acheté au carton : conversion = 12)
    """
    inv = _norm_product_name(invoice_desc)
    prod = _norm_product_name(product_name)
    inv_tokens = set(inv.split())
    prod_tokens = set(prod.split())

    for n, inner in re.findall(r"\b(\d+)/(\d+[A-Z]*)\b", inv):
        if inner in prod_tokens and f"{n}/{inner}" not in prod_tokens and int(n) > 1:
            return float(n)

    for n, inner in re.findall(r"\b(\d+)/(\d+[A-Z]*)\b", prod):
        if (
            inner in inv_tokens
            and f"{n}/{inner}" not in inv_tokens
            and int(n) > 1
            and float(conversion_factor or 1) == int(n)
        ):
            return round(1 / int(n), 6)

    return 1.0


def _canonical_supplier(cur, supplier):
    """Nom du fournisseur tel qu'il existe déjà (accents / casse ignorés : « Emballage futé »)."""
    cur.execute("SELECT DISTINCT supplier FROM stock_products")

    target = _norm_product_name(supplier)

    for (name,) in cur.fetchall():
        if name and _norm_product_name(name) == target:
            return name, True

    return supplier, False


def _match_article(code, description, products, codes, kind="product"):
    """
    products : {nom_normalisé: produit}
    codes    : {code: (product_name, factor)} pour ce fournisseur
    """
    if kind == "discount":
        return {"status": "discount", "product_name": None, "factor": 1, "candidates": []}

    if code in codes:
        product_name, factor = codes[code]

        if product_name is None:
            return {"status": "ignored", "product_name": None, "factor": 1, "candidates": []}

        if _norm_product_name(product_name) in products:
            return {"status": "code", "product_name": product_name, "factor": factor or 1, "candidates": []}

    norm = _norm_product_name(description)

    if norm in products:
        return {"status": "exact", "product_name": products[norm]["name"], "factor": 1, "candidates": []}

    scored = sorted(
        ((_similarity(norm, key), key) for key in products),
        reverse=True
    )
    candidates = [key for score, key in scored[:3] if score >= 0.55]
    names = [products[c]["name"] for c in candidates]

    if names:
        return {
            "status": "fuzzy",
            "product_name": names[0],
            "factor": _pack_factor(
                description,
                names[0],
                products[candidates[0]]["conversion_factor"]
            ),
            "candidates": names
        }

    return {"status": "none", "product_name": None, "factor": 1, "candidates": []}


def _guess_quantity_from_history(line, art, products):
    """
    Ligne de facture sans quantité ni prix unitaire lisibles :
    quantité ≈ montant ÷ (dernier prix d'achat du produit × facteur), arrondie (≥ 1).
    Sans produit associé ou sans historique → la quantité reste vide (à saisir).
    """
    product = products.get(_norm_product_name(art.get("product_name") or ""))
    last_price = float((product or {}).get("last_purchase_price") or 0)
    total = float(line.get("total") or 0)

    if last_price <= 0 or total <= 0:
        return

    invoice_unit_price = last_price * float(art.get("factor") or 1)
    qty = max(1, round(total / invoice_unit_price))

    line["quantity"] = float(qty)
    line["unit_price"] = round(total / qty, 4)
    line["quantity_source"] = "history"


@app.route("/admin/stock/invoices/parse", methods=["POST"])
def stock_invoices_parse():
    """
    Lit plusieurs factures PDF et propose l'association article → produit.
    Rien n'est enregistré ici : l'utilisateur vérifie puis appelle /invoices/import.
    """

    if not check_admin(request):
        return jsonify({"error": "unauthorized"}), 403

    files = request.files.getlist("files")

    if not files:
        return jsonify({"error": "Aucun fichier fourni"}), 400

    invoices = []

    for f in files:
        try:
            parsed = parse_supplier_invoice(f.read())
        except Exception as e:
            parsed = {
                "format": None, "supplier": "", "invoice_number": "",
                "invoice_date": "", "delivery_number": "", "delivery_date": "",
                "total_ttc": None, "lines": [], "lines_total": 0,
                "warnings": [f"Lecture PDF impossible : {e}"]
            }

        parsed["file"] = f.filename
        invoices.append(parsed)

    articles = {}

    with get_conn() as conn:
        with conn.cursor() as cur:

            supplier_cache = {}

            for inv in invoices:

                if not inv["format"]:
                    inv["duplicate"] = False
                    inv["nearby_manual"] = []
                    inv["purchase_date"] = ""
                    continue

                supplier, known = _canonical_supplier(cur, inv["supplier"])
                inv["supplier"] = supplier
                inv["supplier_known"] = known

                if not known:
                    inv["warnings"].append(
                        f"Fournisseur « {supplier} » absent du stock : créez l'onglet "
                        f"« {supplier} » dans Google Sheet avant d'importer"
                    )

                # date d'achat = date de livraison (BL), sinon date facture
                inv["purchase_date"] = inv["delivery_date"] or inv["invoice_date"]

                # facture déjà importée ?
                inv["duplicate"] = _invoice_already_imported(cur, supplier, inv["invoice_number"])

                # achats saisis à la main autour de cette date → risque de doublon
                inv["nearby_manual"] = []

                if inv["purchase_date"]:
                    cur.execute("""
                    SELECT COALESCE(purchase_date, created_at::date) AS d, COUNT(*)
                    FROM stock_movements
                    WHERE movement_type = 'ACHAT'
                    AND invoice_ref IS NULL
                    AND LOWER(TRIM(supplier)) = LOWER(TRIM(%s))
                    AND COALESCE(purchase_date, created_at::date)
                        BETWEEN %s::date - 3 AND %s::date + 3
                    GROUP BY d
                    ORDER BY d
                    """, (supplier, inv["purchase_date"], inv["purchase_date"]))

                    inv["nearby_manual"] = [
                        {"date": r[0].isoformat(), "count": r[1]}
                        for r in cur.fetchall()
                    ]

                # produits + codes mémorisés du fournisseur
                if supplier.lower() not in supplier_cache:

                    cur.execute("""
                    SELECT name, purchase_unit, stock_unit, conversion_factor, last_purchase_price
                    FROM stock_products
                    WHERE LOWER(TRIM(supplier)) = LOWER(TRIM(%s))
                    """, (supplier,))

                    products = {
                        _norm_product_name(r[0]): {
                            "name": r[0],
                            "conversion_factor": r[3],
                            "last_purchase_price": r[4]
                        }
                        for r in cur.fetchall()
                    }

                    cur.execute("""
                    SELECT code, product_name, factor
                    FROM stock_supplier_codes
                    WHERE LOWER(supplier) = LOWER(%s)
                    """, (supplier,))

                    codes = {r[0]: (r[1], r[2]) for r in cur.fetchall()}

                    supplier_cache[supplier.lower()] = (products, codes)

                products, codes = supplier_cache[supplier.lower()]

                for line in inv["lines"]:

                    key = f"{supplier.lower()}__{line['code']}"

                    art = articles.get(key)

                    if not art:
                        art = articles[key] = {
                            "key": key,
                            "supplier": supplier,
                            "code": line["code"],
                            "description": line["description"],
                            "occurrences": 0,
                            "total_quantity": 0,
                            "last_unit_price": line["unit_price"] or 0,
                            "last_date": "",
                            "kind": line.get("kind", "product"),
                            "unit_hint": line.get("unit_hint", ""),
                            **_match_article(
                                line["code"], line["description"], products, codes,
                                line.get("kind", "product")
                            )
                        }

                    # quantité illisible sur la facture → estimée d'après le dernier prix d'achat
                    if line.get("quantity") is None:
                        _guess_quantity_from_history(line, art, products)

                    art["occurrences"] += 1
                    art["total_quantity"] += line["quantity"] or 0

                    if (line["quantity"] or 0) > 0 and inv["purchase_date"] >= art["last_date"]:
                        art["last_date"] = inv["purchase_date"]
                        art["last_unit_price"] = line["unit_price"]

    return jsonify({
        "invoices": invoices,
        "articles": list(articles.values())
    })


@app.route("/admin/stock/invoices/import", methods=["POST"])
def stock_invoices_import():
    """
    Body :
    {
      "invoices": [{supplier, invoice_number, purchase_date, lines: [{code, description, quantity, unit_price, total}]}],
      "articles": [{supplier, code, description, action: map|create|ignore,
                    product_name, factor, purchase_unit, stock_unit, conversion_factor}]
    }
    """

    if not check_admin(request):
        return jsonify({"error": "unauthorized"}), 403

    data = request.json or {}

    invoices = data.get("invoices") or []
    articles = {
        f"{(a.get('supplier') or '').strip().lower()}__{a.get('code')}": a
        for a in (data.get("articles") or [])
    }

    # toutes les lignes doivent avoir une décision
    errors = []

    for inv in invoices:
        for line in inv.get("lines") or []:
            key = f"{(inv.get('supplier') or '').strip().lower()}__{line.get('code')}"
            a = articles.get(key)

            if not a or a.get("action") not in ("map", "create", "ignore"):
                errors.append(f"{inv.get('invoice_number')} : article {line.get('code')} sans décision")
            elif a["action"] != "ignore" and not (a.get("product_name") or "").strip():
                errors.append(f"Article {line.get('code')} : produit manquant")
            elif a["action"] != "ignore" and not float(a.get("factor") or 0) > 0:
                errors.append(f"Article {line.get('code')} : facteur invalide")
            elif a["action"] != "ignore" and line.get("quantity") is None:
                errors.append(
                    f"{inv.get('invoice_number')} : quantité illisible pour {line.get('code')} "
                    f"{line.get('description', '')} — importez cette facture depuis « Achats fournisseurs » "
                    f"pour saisir la quantité"
                )

    if errors:
        return jsonify({"error": "Données incomplètes", "details": sorted(set(errors))}), 400

    # =========================
    # 1. nouveaux produits → Google Sheet (source of truth) puis sync
    # =========================

    to_create = [a for a in articles.values() if a.get("action") == "create"]
    created = []

    if to_create:

        with get_conn() as conn:
            with conn.cursor() as cur:

                rows_by_supplier = {}

                for a in to_create:
                    name = a["product_name"].strip()

                    cur.execute("""
                    SELECT 1 FROM stock_products
                    WHERE LOWER(TRIM(name)) = LOWER(TRIM(%s))
                    AND LOWER(TRIM(supplier)) = LOWER(TRIM(%s))
                    """, (name, a["supplier"]))

                    if cur.fetchone():
                        continue  # existe déjà → simple association

                    rows_by_supplier.setdefault(a["supplier"], []).append([
                        name,                               # Produit
                        (a.get("purchase_unit") or "").strip(),  # Unité achat
                        0,                                  # Stock min
                        0,                                  # Stock Réel
                        "",                                 # Commander
                        "Créé par import facture"           # Note
                    ])
                    created.append(a)

        try:
            for supplier, rows in rows_by_supplier.items():
                try:
                    sheet = spreadsheet.worksheet(supplier)
                except gspread.exceptions.WorksheetNotFound:
                    return jsonify({
                        "error": f"Onglet « {supplier} » introuvable dans Google Sheet : "
                                 f"créez-le (mêmes colonnes que les autres fournisseurs) puis relancez l'import"
                    }), 400

                sheet.append_rows(rows)

            if rows_by_supplier:
                sync_stock()
        except Exception as e:
            return jsonify({"error": f"Création produits Google Sheet impossible : {e}"}), 500

        with get_conn() as conn:
            with conn.cursor() as cur:
                for a in created:
                    cur.execute("""
                    UPDATE stock_products
                    SET stock_unit = %s,
                        conversion_factor = %s
                    WHERE LOWER(TRIM(name)) = LOWER(TRIM(%s))
                    AND LOWER(TRIM(supplier)) = LOWER(TRIM(%s))
                    """, (
                        (a.get("stock_unit") or a.get("purchase_unit") or "").strip(),
                        float(a.get("conversion_factor") or 1) or 1,
                        a["product_name"].strip(),
                        a["supplier"]
                    ))

    # =========================
    # 2. lignes d'achat (transaction unique)
    # =========================

    conn = get_conn()
    conn.autocommit = False

    imported = []
    skipped = []
    line_count = 0
    touched = set()

    try:
        with conn.cursor() as cur:

            product_cache = {}

            def get_product(name, supplier):
                k = (name.strip().lower(), supplier.strip().lower())
                if k not in product_cache:
                    cur.execute("""
                    SELECT name, supplier, purchase_unit
                    FROM stock_products
                    WHERE LOWER(TRIM(name)) = %s
                    AND LOWER(TRIM(supplier)) = %s
                    """, k)
                    product_cache[k] = cur.fetchone()
                return product_cache[k]

            # codes mémorisés
            for a in articles.values():
                product_name = None if a["action"] == "ignore" else a["product_name"].strip()

                cur.execute("""
                INSERT INTO stock_supplier_codes
                (supplier, code, description, product_name, factor, updated_at)
                VALUES (%s,%s,%s,%s,%s,NOW())
                ON CONFLICT (LOWER(supplier), code)
                DO UPDATE SET
                    description = EXCLUDED.description,
                    product_name = EXCLUDED.product_name,
                    factor = EXCLUDED.factor,
                    updated_at = NOW()
                """, (
                    a["supplier"],
                    a["code"],
                    a.get("description"),
                    product_name,
                    float(a.get("factor") or 1)
                ))

            for inv in invoices:

                supplier = inv["supplier"]
                number = inv["invoice_number"]

                if _invoice_already_imported(cur, supplier, number):
                    skipped.append(number)
                    continue

                for line in inv["lines"]:

                    a = articles[f"{supplier.strip().lower()}__{line['code']}"]

                    if a["action"] == "ignore":
                        continue

                    product = get_product(a["product_name"], supplier)

                    if not product:
                        raise ValueError(f"Produit introuvable : {a['product_name']} ({supplier})")

                    factor = float(a.get("factor") or 1)
                    qty_invoice = float(line["quantity"])
                    qty = qty_invoice * factor
                    unit_price = float(line["unit_price"]) / factor
                    movement_type = "ACHAT" if qty > 0 else "RETOUR"

                    cur.execute("""
                    INSERT INTO stock_movements
                    (
                        product_name,
                        supplier,
                        movement_type,
                        quantity,
                        purchase_quantity,
                        purchase_unit,
                        total_price,
                        unit_price,
                        purchase_date,
                        invoice_ref,
                        note
                    )
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    """, (
                        product[0],
                        product[1],
                        movement_type,
                        qty,
                        qty,
                        product[2],
                        float(line["total"]),
                        round(unit_price, 4),
                        inv["purchase_date"],
                        number,
                        f"Facture {number} (import PDF)\n"
                        f"{line['code']} {line['description']}\n"
                        f"{qty_invoice:g} × {float(line['unit_price']):.2f} €"
                    ))

                    line_count += 1

                    if movement_type == "ACHAT":
                        touched.add((product[0], product[1]))

                        if line.get("tva") is not None and 0 <= float(line["tva"]) <= 30:
                            cur.execute("""
                            UPDATE stock_products SET purchase_tva = %s
                            WHERE LOWER(TRIM(name)) = LOWER(TRIM(%s))
                            AND LOWER(TRIM(supplier)) = LOWER(TRIM(%s))
                            """, (float(line["tva"]), product[0], product[1]))

                imported.append(number)

            for name, supplier in touched:
                _refresh_product_prices(cur, name, supplier)

        conn.commit()

    except Exception as e:
        conn.rollback()
        return jsonify({"error": f"Import annulé : {e}"}), 500

    finally:
        conn.close()

    return jsonify({
        "success": True,
        "imported": imported,
        "skipped": skipped,
        "lines": line_count,
        "created_products": [a["product_name"] for a in created]
    })


@app.route("/admin/stock/refresh", methods=["POST"])
def refresh_stock():

    if not check_admin(request):
        return jsonify({
            "error":"unauthorized"
        }), 403

    result = sync_stock()

    return jsonify(result)


# =========================
# COÛT DES PLATS
# =========================

@app.route("/admin/costing", methods=["GET"])
def costing_data():
    """
    Tout ce qu'il faut pour calculer le coût des plats côté page :
    plats du menu, fiches techniques, produits stock avec prix.
    Les calculs (HT, conversion, food cost) sont faits dans cout.html.
    """

    if not check_admin(request):
        return jsonify({"error": "unauthorized"}), 403

    with get_conn() as conn:
        with conn.cursor() as cur:

            cur.execute("""
            SELECT id, name, price, category, tva, active
            FROM products
            ORDER BY category, name
            """)
            menu = [
                {
                    "id": r[0], "name": r[1], "price": float(r[2] or 0),
                    "category": r[3] or "", "tva": float(r[4] if r[4] is not None else 10),
                    "active": r[5] is not False
                }
                for r in cur.fetchall()
            ]

            cur.execute("""
            SELECT name, supplier, purchase_unit, stock_unit, conversion_factor,
                   last_purchase_price, average_price, COALESCE(purchase_tva, 5.5),
                   content_qty, content_unit
            FROM stock_products
            ORDER BY supplier, name
            """)
            stock = [
                {
                    "name": r[0], "supplier": r[1],
                    "purchase_unit": r[2] or "", "stock_unit": r[3] or "",
                    "conversion_factor": float(r[4] or 1),
                    "last_purchase_price": float(r[5] or 0),
                    "average_price": float(r[6] or 0),
                    "purchase_tva": float(r[7]),
                    "content_qty": float(r[8]) if r[8] else None,
                    "content_unit": r[9] or "",
                }
                for r in cur.fetchall()
            ]

            cur.execute("SELECT menu_product_id, portions, extra_cost, note, updated_at FROM recipes")
            recipes = {
                r[0]: {
                    "portions": float(r[1] or 1), "extra_cost": float(r[2] or 0),
                    "note": r[3] or "", "updated_at": r[4].isoformat() if r[4] else "",
                    "ingredients": []
                }
                for r in cur.fetchall()
            }

            cur.execute("""
            SELECT menu_product_id, product_name, supplier, quantity, note, preparation_id, unit
            FROM recipe_ingredients
            ORDER BY id
            """)
            for r in cur.fetchall():
                recipes.setdefault(r[0], {
                    "portions": 1, "extra_cost": 0, "note": "", "updated_at": "", "ingredients": []
                })["ingredients"].append(_ingredient_json(r[1], r[2], r[5], r[3], r[6], r[4]))

            cur.execute("""
            SELECT id, name, yield_qty, yield_unit, extra_cost, note, updated_at
            FROM preparations
            ORDER BY name
            """)
            preparations = {
                r[0]: {
                    "id": r[0], "name": r[1], "yield_qty": float(r[2] or 1),
                    "yield_unit": r[3] or "L", "extra_cost": float(r[4] or 0),
                    "note": r[5] or "", "updated_at": r[6].isoformat() if r[6] else "",
                    "ingredients": []
                }
                for r in cur.fetchall()
            }

            cur.execute("""
            SELECT preparation_id, product_name, supplier, quantity, note, sub_preparation_id, unit
            FROM preparation_ingredients
            ORDER BY id
            """)
            for r in cur.fetchall():
                if r[0] in preparations:
                    preparations[r[0]]["ingredients"].append(
                        _ingredient_json(r[1], r[2], r[5], r[3], r[6], r[4])
                    )

            cur.execute("SELECT id, name, note FROM competitors ORDER BY id")
            competitors = [{"id": r[0], "name": r[1], "note": r[2] or ""} for r in cur.fetchall()]

            cur.execute("""
            SELECT menu_product_id, competitor_id, price, updated_at, quantity
            FROM competitor_prices
            """)
            competitor_prices = [
                {
                    "menu_product_id": r[0], "competitor_id": r[1], "price": float(r[2]),
                    "updated_at": r[3].isoformat() if r[3] else "",
                    "quantity": float(r[4]) if r[4] is not None else None
                }
                for r in cur.fetchall()
            ]

            cur.execute("SELECT menu_product_id, quantity, unit FROM menu_portions")
            portions = [
                {"menu_product_id": r[0], "quantity": float(r[1]), "unit": r[2] or "pce"}
                for r in cur.fetchall()
            ]

    return jsonify({
        "menu": menu,
        "stock": stock,
        "recipes": {str(k): v for k, v in recipes.items()},
        "preparations": list(preparations.values()),
        "competitors": competitors,
        "competitor_prices": competitor_prices,
        "portions": portions
    })


def _ingredient_json(product_name, supplier, preparation_id, quantity, unit, note):
    return {
        "product_name": product_name or "",
        "supplier": supplier or "",
        "preparation_id": preparation_id,
        "quantity": float(quantity or 0),
        "unit": unit or "",
        "note": note or ""
    }


def _parse_ingredient_lines(lines):
    """
    → (liste [(product_name, supplier, preparation_id, qty, unit, note)], erreur)
    Une ligne = produit stock (nom + fournisseur) OU préparation maison (preparation_id).
    """
    result = []

    for ing in lines or []:
        name = (ing.get("product_name") or "").strip()
        supplier = (ing.get("supplier") or "").strip()
        prep_id = ing.get("preparation_id")

        try:
            prep_id = int(prep_id) if prep_id not in (None, "") else None
        except (TypeError, ValueError):
            prep_id = None

        if not prep_id and (not name or not supplier):
            continue

        try:
            qty = float(ing.get("quantity") or 0)
        except (TypeError, ValueError):
            qty = 0

        if qty <= 0:
            return None, f"Quantité invalide pour « {name or 'préparation'} »"

        result.append((
            "" if prep_id else name,
            "" if prep_id else supplier,
            prep_id,
            qty,
            (ing.get("unit") or "").strip(),
            (ing.get("note") or "").strip()
        ))

    return result, None


@app.route("/admin/costing/recipe", methods=["POST"])
def costing_save_recipe():
    """
    Body : {menu_product_id, portions, extra_cost, note,
            ingredients: [{product_name, supplier, quantity, note}]}
    Remplace entièrement la fiche du plat.
    """

    if not check_admin(request):
        return jsonify({"error": "unauthorized"}), 403

    data = request.json or {}

    try:
        menu_id = int(data.get("menu_product_id"))
        portions = float(data.get("portions") or 1)
        extra_cost = float(data.get("extra_cost") or 0)
    except (TypeError, ValueError):
        return jsonify({"error": "Données invalides"}), 400

    if portions <= 0:
        return jsonify({"error": "Nombre de portions invalide (> 0)"}), 400

    ingredients, error = _parse_ingredient_lines(data.get("ingredients"))

    if error:
        return jsonify({"error": error}), 400

    with get_conn() as conn:
        with conn.cursor() as cur:

            cur.execute("SELECT 1 FROM products WHERE id = %s", (menu_id,))
            if not cur.fetchone():
                return jsonify({"error": "Plat introuvable"}), 404

            cur.execute("DELETE FROM recipe_ingredients WHERE menu_product_id = %s", (menu_id,))

            if not ingredients and extra_cost == 0:
                cur.execute("DELETE FROM recipes WHERE menu_product_id = %s", (menu_id,))
                return jsonify({"success": True, "deleted": True})

            cur.execute("""
            INSERT INTO recipes (menu_product_id, portions, extra_cost, note, updated_at)
            VALUES (%s,%s,%s,%s,NOW())
            ON CONFLICT (menu_product_id)
            DO UPDATE SET portions = EXCLUDED.portions,
                          extra_cost = EXCLUDED.extra_cost,
                          note = EXCLUDED.note,
                          updated_at = NOW()
            """, (menu_id, portions, extra_cost, (data.get("note") or "").strip()))

            for name, supplier, prep_id, qty, unit, note in ingredients:
                cur.execute("""
                INSERT INTO recipe_ingredients
                (menu_product_id, product_name, supplier, preparation_id, quantity, unit, note)
                VALUES (%s,%s,%s,%s,%s,%s,%s)
                """, (menu_id, name, supplier, prep_id, qty, unit, note))

    return jsonify({"success": True})


@app.route("/admin/costing/preparation", methods=["POST"])
def costing_save_preparation():
    """
    Body : {id?, name, yield_qty, yield_unit, extra_cost, note,
            ingredients: [{product_name, supplier | preparation_id, quantity, unit, note}]}
    Crée (sans id) ou remplace une préparation maison. Retour : {success, id}
    """

    if not check_admin(request):
        return jsonify({"error": "unauthorized"}), 403

    data = request.json or {}
    name = (data.get("name") or "").strip()
    yield_unit = (data.get("yield_unit") or "").strip()

    try:
        yield_qty = float(data.get("yield_qty") or 0)
        extra_cost = float(data.get("extra_cost") or 0)
        prep_id = int(data["id"]) if data.get("id") not in (None, "") else None
    except (TypeError, ValueError):
        return jsonify({"error": "Données invalides"}), 400

    if not name:
        return jsonify({"error": "Nom de la préparation obligatoire"}), 400

    if yield_qty <= 0 or not yield_unit:
        return jsonify({"error": "Rendement obligatoire (ex : 8 L)"}), 400

    ingredients, error = _parse_ingredient_lines(data.get("ingredients"))

    if error:
        return jsonify({"error": error}), 400

    if prep_id and any(line[2] == prep_id for line in ingredients):
        return jsonify({"error": "Une préparation ne peut pas s'utiliser elle-même"}), 400

    with get_conn() as conn:
        with conn.cursor() as cur:

            if prep_id:
                cur.execute("""
                UPDATE preparations
                SET name=%s, yield_qty=%s, yield_unit=%s, extra_cost=%s, note=%s, updated_at=NOW()
                WHERE id=%s
                """, (name, yield_qty, yield_unit, extra_cost, (data.get("note") or "").strip(), prep_id))

                if cur.rowcount == 0:
                    return jsonify({"error": "Préparation introuvable"}), 404

                cur.execute("DELETE FROM preparation_ingredients WHERE preparation_id = %s", (prep_id,))
            else:
                cur.execute("""
                INSERT INTO preparations (name, yield_qty, yield_unit, extra_cost, note)
                VALUES (%s,%s,%s,%s,%s)
                RETURNING id
                """, (name, yield_qty, yield_unit, extra_cost, (data.get("note") or "").strip()))
                prep_id = cur.fetchone()[0]

            for pname, supplier, sub_id, qty, unit, note in ingredients:
                cur.execute("""
                INSERT INTO preparation_ingredients
                (preparation_id, product_name, supplier, sub_preparation_id, quantity, unit, note)
                VALUES (%s,%s,%s,%s,%s,%s,%s)
                """, (prep_id, pname, supplier, sub_id, qty, unit, note))

    return jsonify({"success": True, "id": prep_id})


@app.route("/admin/costing/preparation/delete", methods=["POST"])
def costing_delete_preparation():
    """Body : {id}. Refusé si la préparation est utilisée par un plat ou une autre préparation."""

    if not check_admin(request):
        return jsonify({"error": "unauthorized"}), 403

    try:
        prep_id = int((request.json or {}).get("id"))
    except (TypeError, ValueError):
        return jsonify({"error": "Préparation invalide"}), 400

    with get_conn() as conn:
        with conn.cursor() as cur:

            cur.execute("""
            SELECT DISTINCT p.name
            FROM recipe_ingredients ri
            JOIN products p ON p.id = ri.menu_product_id
            WHERE ri.preparation_id = %s
            """, (prep_id,))
            used = [r[0] for r in cur.fetchall()]

            cur.execute("""
            SELECT DISTINCT pr.name
            FROM preparation_ingredients pi
            JOIN preparations pr ON pr.id = pi.preparation_id
            WHERE pi.sub_preparation_id = %s
            """, (prep_id,))
            used += [f"🥣 {r[0]}" for r in cur.fetchall()]

            if used:
                return jsonify({"error": "Utilisée dans : " + ", ".join(used)}), 400

            cur.execute("DELETE FROM preparation_ingredients WHERE preparation_id = %s", (prep_id,))
            cur.execute("DELETE FROM preparations WHERE id = %s", (prep_id,))

    return jsonify({"success": True})


@app.route("/admin/costing/competitor", methods=["POST"])
def costing_save_competitor():
    """Body : {id?, name, note} → crée ou renomme un concurrent. Retour : {success, id}"""

    if not check_admin(request):
        return jsonify({"error": "unauthorized"}), 403

    data = request.json or {}
    name = (data.get("name") or "").strip()
    note = (data.get("note") or "").strip()

    if not name:
        return jsonify({"error": "Nom du concurrent obligatoire"}), 400

    try:
        comp_id = int(data["id"]) if data.get("id") not in (None, "") else None
    except (TypeError, ValueError):
        return jsonify({"error": "Concurrent invalide"}), 400

    with get_conn() as conn:
        with conn.cursor() as cur:

            cur.execute("""
            SELECT id FROM competitors
            WHERE LOWER(name) = LOWER(%s) AND (%s IS NULL OR id <> %s)
            """, (name, comp_id, comp_id))

            if cur.fetchone():
                return jsonify({"error": f"« {name} » existe déjà"}), 400

            if comp_id:
                cur.execute("UPDATE competitors SET name=%s, note=%s WHERE id=%s", (name, note, comp_id))
                if cur.rowcount == 0:
                    return jsonify({"error": "Concurrent introuvable"}), 404
            else:
                cur.execute(
                    "INSERT INTO competitors (name, note) VALUES (%s,%s) RETURNING id",
                    (name, note)
                )
                comp_id = cur.fetchone()[0]

    return jsonify({"success": True, "id": comp_id})


@app.route("/admin/costing/competitor/delete", methods=["POST"])
def costing_delete_competitor():
    """Body : {id} → supprime le concurrent et tous ses prix."""

    if not check_admin(request):
        return jsonify({"error": "unauthorized"}), 403

    try:
        comp_id = int((request.json or {}).get("id"))
    except (TypeError, ValueError):
        return jsonify({"error": "Concurrent invalide"}), 400

    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM competitor_prices WHERE competitor_id = %s", (comp_id,))
            cur.execute("DELETE FROM competitors WHERE id = %s", (comp_id,))

    return jsonify({"success": True})


@app.route("/admin/costing/competitor-price", methods=["POST"])
def costing_save_competitor_price():
    """
    Body : {menu_product_id, competitor_id, price}
    price vide / null → le prix est supprimé (le concurrent ne vend pas ce plat).
    """

    if not check_admin(request):
        return jsonify({"error": "unauthorized"}), 403

    data = request.json or {}

    try:
        menu_id = int(data.get("menu_product_id"))
        comp_id = int(data.get("competitor_id"))
    except (TypeError, ValueError):
        return jsonify({"error": "Données invalides"}), 400

    raw = data.get("price")
    price = None
    quantity = None

    if data.get("quantity") not in (None, ""):
        try:
            quantity = float(str(data.get("quantity")).replace(",", "."))
        except ValueError:
            return jsonify({"error": "Quantité invalide"}), 400

        if quantity <= 0:
            return jsonify({"error": "Quantité invalide"}), 400

    if raw not in (None, ""):
        try:
            price = float(str(raw).replace(",", "."))
        except ValueError:
            return jsonify({"error": "Prix invalide"}), 400

        if price <= 0 or price > 1000:
            return jsonify({"error": "Prix invalide"}), 400

    with get_conn() as conn:
        with conn.cursor() as cur:

            if price is None:
                cur.execute("""
                DELETE FROM competitor_prices
                WHERE menu_product_id = %s AND competitor_id = %s
                """, (menu_id, comp_id))
                return jsonify({"success": True, "deleted": True})

            cur.execute("""
            INSERT INTO competitor_prices (menu_product_id, competitor_id, price, quantity, updated_at)
            VALUES (%s,%s,%s,%s,NOW())
            ON CONFLICT (menu_product_id, competitor_id)
            DO UPDATE SET price = EXCLUDED.price, quantity = EXCLUDED.quantity, updated_at = NOW()
            RETURNING updated_at
            """, (menu_id, comp_id, price, quantity))

            updated_at = cur.fetchone()[0]

    return jsonify({
        "success": True, "price": price, "quantity": quantity,
        "updated_at": updated_at.isoformat()
    })


@app.route("/admin/costing/portion", methods=["POST"])
def costing_save_portion():
    """Body : {menu_product_id, quantity, unit} — quantité vide → portion supprimée."""

    if not check_admin(request):
        return jsonify({"error": "unauthorized"}), 403

    data = request.json or {}

    try:
        menu_id = int(data.get("menu_product_id"))
    except (TypeError, ValueError):
        return jsonify({"error": "Plat invalide"}), 400

    unit = (data.get("unit") or "pce").strip()[:20] or "pce"
    raw = data.get("quantity")

    with get_conn() as conn:
        with conn.cursor() as cur:

            if raw in (None, ""):
                cur.execute("DELETE FROM menu_portions WHERE menu_product_id = %s", (menu_id,))
                return jsonify({"success": True, "deleted": True})

            try:
                quantity = float(str(raw).replace(",", "."))
            except ValueError:
                return jsonify({"error": "Quantité invalide"}), 400

            if quantity <= 0:
                return jsonify({"error": "Quantité invalide"}), 400

            cur.execute("""
            INSERT INTO menu_portions (menu_product_id, quantity, unit, updated_at)
            VALUES (%s,%s,%s,NOW())
            ON CONFLICT (menu_product_id)
            DO UPDATE SET quantity = EXCLUDED.quantity, unit = EXCLUDED.unit, updated_at = NOW()
            """, (menu_id, quantity, unit))

    return jsonify({"success": True, "quantity": quantity, "unit": unit})


@app.route("/admin/costing/purchase-tva", methods=["POST"])
def costing_purchase_tva():
    """Body : {product_name, supplier, purchase_tva} (correction manuelle)."""

    if not check_admin(request):
        return jsonify({"error": "unauthorized"}), 403

    data = request.json or {}

    try:
        tva = float(data.get("purchase_tva"))
    except (TypeError, ValueError):
        return jsonify({"error": "TVA invalide"}), 400

    if not 0 <= tva <= 30:
        return jsonify({"error": "TVA invalide (0 à 30 %)"}), 400

    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
            UPDATE stock_products SET purchase_tva = %s
            WHERE LOWER(TRIM(name)) = LOWER(TRIM(%s))
            AND LOWER(TRIM(supplier)) = LOWER(TRIM(%s))
            """, (tva, data.get("product_name") or "", data.get("supplier") or ""))

            if cur.rowcount == 0:
                return jsonify({"error": "Produit introuvable"}), 404

    return jsonify({"success": True})


_CONTENT_UNITS = {"g": "g", "kg": "kg", "ml": "ml", "cl": "cl", "l": "L", "pce": "pce"}


@app.route("/admin/costing/content", methods=["POST"])
def costing_content():
    """
    Body : {product_name, supplier, content_qty, content_unit}
    Contenu d'une unité stock (ex : 1 chai = 907 g). content_qty vide / 0 → effacé.
    """

    if not check_admin(request):
        return jsonify({"error": "unauthorized"}), 403

    data = request.json or {}
    raw_qty = str(data.get("content_qty") or "").strip().replace(",", ".")

    if raw_qty in ("", "0"):
        qty, unit = None, None
    else:
        try:
            qty = float(raw_qty)
        except ValueError:
            return jsonify({"error": "Contenu invalide"}), 400

        unit = _CONTENT_UNITS.get(str(data.get("content_unit") or "").strip().lower())

        if qty <= 0:
            return jsonify({"error": "Contenu invalide (nombre > 0)"}), 400
        if not unit:
            return jsonify({"error": "Unité de contenu invalide (g, kg, ml, cl, L, pce)"}), 400

    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
            UPDATE stock_products SET content_qty = %s, content_unit = %s, updated_at = NOW()
            WHERE LOWER(TRIM(name)) = LOWER(TRIM(%s))
            AND LOWER(TRIM(supplier)) = LOWER(TRIM(%s))
            """, (qty, unit, data.get("product_name") or "", data.get("supplier") or ""))

            if cur.rowcount == 0:
                return jsonify({"error": "Produit introuvable"}), 404

    return jsonify({"success": True, "content_qty": qty, "content_unit": unit or ""})


# =========================
# RUN SERVER
# =========================
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)