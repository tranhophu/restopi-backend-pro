from flask import Flask, request, jsonify
from flask_cors import CORS
import stripe
import os
import resend
import json
from datetime import datetime, timedelta
import pytz
import psycopg2
import random
from dotenv import load_dotenv
from flask import send_from_directory
from werkzeug.security import generate_password_hash, check_password_hash
import secrets

paris_tz = pytz.timezone("Europe/Paris")

load_dotenv()

if os.environ.get("RAILWAY_ENVIRONMENT"):
    # chạy trên Railway → dùng internal
    DATABASE_URL = os.environ.get("DATABASE_URL")
else:
    # chạy local → dùng public
    DATABASE_URL = os.environ.get("DATABASE_PUBLIC_URL")

print("Using DB:", DATABASE_URL)

conn = psycopg2.connect(DATABASE_URL)
conn.autocommit = True

def get_customer_by_email(email):
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

    session = stripe.checkout.Session.create(
        payment_method_types=["card"],
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

    with conn.cursor() as cur:
        cur.execute("SELECT points FROM customers WHERE email = %s", (email,))
        row = cur.fetchone()

    return jsonify({
        "points": row[0] if row else 0
    })

@app.route("/pending-orders", methods=["GET"])
def pending_orders():
    limit = request.args.get("limit", 20, type=int)

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
# RUN SERVER
# =========================
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)