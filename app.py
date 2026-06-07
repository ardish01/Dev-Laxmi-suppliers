"""
app.py — Dev Laxmi Suppliers
==============================
Main Flask application server.

Features:
  - Public catalog for carpets & curtains with AI recommendations.
  - Customer inquiry lead system (no login required).
  - Hardened admin gate with fake-404 for unauthorized access.
  - Pre-computed TF-IDF similarity matrix loaded from pickle at startup.
  - Auto-creates database tables and seeds demo products on first run.
"""

import os
import pickle
import logging
import functools
from datetime import datetime
from werkzeug.utils import secure_filename

from flask import (
    Flask, render_template, request, jsonify,
    session, redirect, url_for, abort, make_response,
)
from dotenv import load_dotenv

# ── Optional MySQL import ─────────────────────────────────────────────────────
try:
    import mysql.connector
    from mysql.connector import Error as MySQLError, pooling as mysql_pooling
except ImportError as exc:
    raise RuntimeError(
        "mysql-connector-python is required. Run: pip install mysql-connector-python"
    ) from exc

# ── Bootstrap ─────────────────────────────────────────────────────────────────
load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [APP] %(levelname)s — %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

# ── Flask App ─────────────────────────────────────────────────────────────────
app = Flask(__name__)

app.secret_key = os.getenv("FLASK_SECRET_KEY", "fallback-insecure-key-change-in-prod")

# Hardened session cookie policy
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SECURE=True,          # Requires HTTPS in production; use False locally
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_NAME="dls_session",
    PERMANENT_SESSION_LIFETIME=3600,     # 1 hour
)

ADMIN_PASSCODE = os.getenv("ADMIN_PASSCODE", "devlaxmi@admin2024")
UPLOAD_FOLDER = os.path.join(os.path.dirname(__file__), "static", "uploads")
ALLOWED_IMAGE_EXTENSIONS = {"png", "jpg", "jpeg", "gif", "webp"}


def _ensure_upload_folder() -> None:
    os.makedirs(UPLOAD_FOLDER, exist_ok=True)


def _allowed_image_filename(filename: str) -> bool:
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_IMAGE_EXTENSIONS

# ── Database Configuration ────────────────────────────────────────────────────
DB_CONFIG = {
    "host":     os.getenv("DB_HOST", "localhost"),
    "port":     int(os.getenv("DB_PORT", 3306)),
    "user":     os.getenv("DB_USER", "root"),
    "password": os.getenv("DB_PASSWORD", ""),
    "database": os.getenv("DB_NAME", "devlaxmi_db"),
    "autocommit": True,
    "connection_timeout": 10,
}

DB_NAME = os.getenv("DB_NAME", "devlaxmi_db")

# ── Database Connection Pool ──────────────────────────────────────────────────
_pool: mysql_pooling.MySQLConnectionPool | None = None


def get_pool() -> mysql_pooling.MySQLConnectionPool:
    """Return (or lazily create) the MySQL connection pool."""
    global _pool
    if _pool is None:
        _pool = mysql_pooling.MySQLConnectionPool(
            pool_name="devlaxmi_pool",
            pool_size=5,
            **DB_CONFIG,
        )
        log.info("MySQL connection pool created (size=5).")
    return _pool


def get_db():
    """Get a connection from the pool."""
    return get_pool().get_connection()


# ── Database Initialization ───────────────────────────────────────────────────
DEMO_PRODUCTS = []


def init_database() -> None:
    """
    Ensure the database, tables, and seed data exist.
    Called once at application startup.
    """
    _ensure_upload_folder()

    # --- Step 1: Create database if it doesn't exist ---
    root_cfg = {k: v for k, v in DB_CONFIG.items() if k != "database"}
    root_cfg.pop("autocommit", None)
    root_cfg.pop("connection_timeout", None)
    try:
        conn = mysql.connector.connect(**root_cfg)
        cur = conn.cursor()
        cur.execute(f"CREATE DATABASE IF NOT EXISTS `{DB_NAME}` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci")
        cur.close()
        conn.close()
        log.info("Database '%s' is ready.", DB_NAME)
    except MySQLError as exc:
        log.error("Failed to create database: %s", exc)
        raise

    # --- Step 2: Create tables ---
    conn = mysql.connector.connect(**{k: v for k, v in DB_CONFIG.items() if k not in ("autocommit",)},
                                   autocommit=True)
    cur = conn.cursor()

    cur.execute("""
        CREATE TABLE IF NOT EXISTS products (
            id            INT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
            name          VARCHAR(255)     NOT NULL,
            category      ENUM('carpet','curtain','doormat','chair','table','hanger') NOT NULL,
            brand         VARCHAR(100)     NOT NULL DEFAULT 'Unknown',
            description   TEXT             NOT NULL,
            price         DECIMAL(10, 2)   NOT NULL DEFAULT 0.00,
            image_url     VARCHAR(512)     NOT NULL DEFAULT '',
            style_tags    TEXT             NOT NULL DEFAULT '',
            created_at    DATETIME         NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at    DATETIME         NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
            INDEX idx_category (category),
            INDEX idx_brand (brand)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """)

    # Attempt to add the brand column if the table already existed before this update
    try:
        cur.execute("ALTER TABLE products ADD COLUMN brand VARCHAR(100) NOT NULL DEFAULT 'Unknown' AFTER category")
        cur.execute("CREATE INDEX idx_brand ON products(brand)")
        log.info("Successfully added 'brand' column to existing products table.")
    except MySQLError as exc:
        if exc.errno == 1060: # Duplicate column name
            pass
        else:
            log.warning("Could not alter table for brand: %s", exc)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS inquiries (
            id            INT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
            product_id    INT UNSIGNED     NOT NULL,
            client_name   VARCHAR(255)     NOT NULL,
            phone         VARCHAR(20)      NOT NULL,
            message       TEXT             NULL,
            created_at    DATETIME         NOT NULL DEFAULT CURRENT_TIMESTAMP,
            CONSTRAINT fk_inquiry_product
                FOREIGN KEY (product_id) REFERENCES products(id)
                ON DELETE CASCADE ON UPDATE CASCADE,
            INDEX idx_product (product_id),
            INDEX idx_created (created_at)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """)

    # --- Step 3: Seed demo data if empty ---
    cur.execute("SELECT COUNT(*) FROM products")
    (count,) = cur.fetchone()
    if count == 0:
        log.info("Seeding %d demo products…", len(DEMO_PRODUCTS))
        sql = """
            INSERT INTO products (name, category, brand, description, price, image_url, style_tags)
            VALUES (%(name)s, %(category)s, %(brand)s, %(description)s, %(price)s, %(image_url)s, %(style_tags)s)
        """
        cur.executemany(sql, DEMO_PRODUCTS)
        log.info("Demo products seeded successfully.")
    else:
        log.info("Found %d existing products — skipping seed.", count)

    cur.close()
    conn.close()


# ── Recommendation Engine ─────────────────────────────────────────────────────
PICKLE_PATH = os.path.join(os.path.dirname(__file__), "models", "similarity_matrix.pkl")
_reco_payload: dict | None = None


def load_reco_model() -> dict | None:
    """
    Load the pickled TF-IDF similarity payload from disk.
    Returns None if the pickle file does not exist yet (graceful fallback).
    """
    global _reco_payload
    if _reco_payload is not None:
        return _reco_payload
    if not os.path.exists(PICKLE_PATH):
        log.warning(
            "Similarity pickle not found at '%s'. "
            "Run `python recommendation.py` to train the model. "
            "Falling back to category-based recommendations.",
            PICKLE_PATH,
        )
        return None
    try:
        with open(PICKLE_PATH, "rb") as fh:
            _reco_payload = pickle.load(fh)
        log.info(
            "Recommendation model loaded: %d products indexed.",
            _reco_payload.get("num_products", 0),
        )
        return _reco_payload
    except Exception as exc:
        log.error("Failed to load recommendation model: %s", exc)
        return None


def reload_reco_model() -> None:
    """Force re-load the pickle (call after training new model)."""
    global _reco_payload
    _reco_payload = None
    load_reco_model()


def get_recommendations(product_id: int, product_category: str, top_n: int = 4) -> list[dict]:
    """
    Return up to `top_n` cross-category product recommendations for the given product.

    Strategy:
      1. Use cosine similarity from the pre-computed TF-IDF matrix.
      2. Filter to only return products from the *opposite* category.
      3. If the pickle model is unavailable, fall back to a simple DB query
         returning random products from the opposite category.
    """
    mapping = {
        "carpet": "curtain",
        "curtain": "doormat",
        "doormat": "carpet",
        "chair": "table",
        "table": "chair",
        "hanger": "chair"
    }
    opposite = mapping.get(product_category, "carpet")

    payload = load_reco_model()
    if payload:
        sim_matrix = payload["similarity_matrix"]
        id_to_idx  = payload["id_to_index"]
        idx_to_id  = payload["index_to_id"]
        categories = payload["categories"]

        if product_id in id_to_idx:
            row_idx = id_to_idx[product_id]
            sim_scores = list(enumerate(sim_matrix[row_idx]))
            # Sort by similarity descending, skip self
            sim_scores.sort(key=lambda x: x[1], reverse=True)

            candidate_ids = []
            for idx, score in sim_scores:
                pid = idx_to_id.get(idx)
                if pid and pid != product_id and categories.get(pid) == opposite:
                    candidate_ids.append(pid)
                if len(candidate_ids) >= top_n:
                    break

            if candidate_ids:
                return _fetch_products_by_ids(candidate_ids)

    # ── Fallback: random opposite-category products ───────────────────────────
    log.info("Using fallback recommendations for product %d.", product_id)
    return _fetch_random_products(opposite, top_n, exclude_id=product_id)


def _fetch_products_by_ids(ids: list[int]) -> list[dict]:
    """Fetch full product rows for a list of IDs, preserving order."""
    if not ids:
        return []
    placeholders = ", ".join(["%s"] * len(ids))
    query = f"SELECT * FROM products WHERE id IN ({placeholders})"
    conn = get_db()
    cur = conn.cursor(dictionary=True)
    cur.execute(query, ids)
    rows = cur.fetchall()
    cur.close()
    conn.close()
    # Preserve the similarity-ranked order
    order = {pid: i for i, pid in enumerate(ids)}
    rows.sort(key=lambda r: order.get(r["id"], 999))
    return rows


def _fetch_random_products(category: str, limit: int, exclude_id: int = 0) -> list[dict]:
    """Fetch random products of a given category, excluding a specific product ID."""
    query = "SELECT * FROM products WHERE category=%s AND id != %s ORDER BY RAND() LIMIT %s"
    conn = get_db()
    cur = conn.cursor(dictionary=True)
    cur.execute(query, (category, exclude_id, limit))
    rows = cur.fetchall()
    cur.close()
    conn.close()
    return rows


# ── Admin Auth Guard ──────────────────────────────────────────────────────────
def admin_required(f):
    """
    Decorator that enforces admin authentication.
    CRITICAL DECEPTION RULE: Any unauthorized access triggers abort(404).
    No redirect, no "access denied" — the admin area simply does not exist.
    """
    @functools.wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("admin_authenticated"):
            abort(404)
        return f(*args, **kwargs)
    return decorated


# ── Error Handlers ────────────────────────────────────────────────────────────
@app.errorhandler(404)
def page_not_found(e):
    """Render a clean, generic 404 page for all 404 errors."""
    return render_template("404.html"), 404


@app.errorhandler(500)
def internal_error(e):
    """Render a clean 500 error page."""
    log.error("Internal server error: %s", e)
    return render_template("500.html"), 500


# ── Public Routes ─────────────────────────────────────────────────────────────

@app.route("/")
def home():
    """
    Main catalog page. Loads all products and passes them to the template.
    Client-side JS handles category filter toggling without page reload.
    """
    category_filter = request.args.get("category", "all").lower()
    conn = get_db()
    cur = conn.cursor(dictionary=True)

    if category_filter in ("carpet", "curtain", "doormat", "chair", "table", "hanger"):
        cur.execute(
            "SELECT * FROM products WHERE category = %s ORDER BY created_at DESC",
            (category_filter,)
        )
    else:
        cur.execute("SELECT * FROM products ORDER BY category, created_at DESC")

    products = cur.fetchall()

    # Convert Decimal price to float for JSON serialisation in template
    for p in products:
        p["price"] = float(p["price"])

    carpet_count = sum(1 for p in products if p["category"] == "carpet")
    curtain_count = sum(1 for p in products if p["category"] == "curtain")
    doormat_count = sum(1 for p in products if p["category"] == "doormat")
    chair_count = sum(1 for p in products if p["category"] == "chair")
    table_count = sum(1 for p in products if p["category"] == "table")
    hanger_count = sum(1 for p in products if p["category"] == "hanger")
    furniture_products = [p for p in products if p["category"] in ("chair", "table", "hanger")]

    cur.execute("SELECT DISTINCT brand FROM products WHERE brand != 'Unknown' ORDER BY brand")
    brands = [row["brand"] for row in cur.fetchall()]

    cur.close()
    conn.close()

    return render_template(
        "home.html",
        products=products,
        active_filter=category_filter,
        carpet_count=carpet_count,
        curtain_count=curtain_count,
        doormat_count=doormat_count,
        chair_count=chair_count,
        table_count=table_count,
        hanger_count=hanger_count,
        furniture_products=furniture_products,
        total_count=len(products),
        brands=brands,
    )


@app.route("/product/<int:product_id>")
def product_detail(product_id: int):
    """
    Product detail page. Shows item details, inquiry form,
    and AI 'Complete the Look' cross-category recommendations.
    """
    conn = get_db()
    cur = conn.cursor(dictionary=True)
    cur.execute("SELECT * FROM products WHERE id = %s", (product_id,))
    product = cur.fetchone()
    cur.close()
    conn.close()

    if not product:
        abort(404)

    product["price"] = float(product["price"])
    recommendations = get_recommendations(product_id, product["category"])
    for r in recommendations:
        r["price"] = float(r["price"])

    reco_category = "curtain" if product["category"] == "carpet" else "carpet"
    return render_template(
        "product.html",
        product=product,
        recommendations=recommendations,
        reco_category=reco_category,
    )


# ── Public API — Inquiry ──────────────────────────────────────────────────────

@app.route("/api/inquire", methods=["POST"])
def api_inquire():
    """
    Submit an inquiry lead for a product.
    Expects JSON: { product_id, client_name, phone, message? }
    """
    data = request.get_json(silent=True) or {}

    product_id  = data.get("product_id")
    client_name = str(data.get("client_name", "")).strip()
    phone       = str(data.get("phone", "")).strip()
    message     = str(data.get("message", "")).strip()

    # ── Input validation ─────────────────────────────────────────────────────
    if not product_id or not client_name or not phone:
        return jsonify({"success": False, "error": "Product ID, name, and phone are required."}), 400

    if not isinstance(product_id, int) or product_id < 1:
        return jsonify({"success": False, "error": "Invalid product ID."}), 400

    if len(client_name) > 255:
        return jsonify({"success": False, "error": "Name is too long."}), 400

    # Basic phone validation: digits, spaces, +, -, ()
    import re
    if not re.match(r"^[\d\s\+\-\(\)]{7,20}$", phone):
        return jsonify({"success": False, "error": "Invalid phone number format."}), 400

    # ── Check product exists ──────────────────────────────────────────────────
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT id FROM products WHERE id = %s", (product_id,))
    if not cur.fetchone():
        cur.close()
        conn.close()
        return jsonify({"success": False, "error": "Product not found."}), 404

    # ── Insert inquiry ────────────────────────────────────────────────────────
    cur.execute(
        "INSERT INTO inquiries (product_id, client_name, phone, message) VALUES (%s, %s, %s, %s)",
        (product_id, client_name, phone, message or None),
    )
    cur.close()
    conn.close()

    log.info("New inquiry from '%s' (%s) for product #%d.", client_name, phone, product_id)
    return jsonify({"success": True, "message": "Your inquiry has been received. We'll contact you shortly!"}), 201


# ── Admin Routes ──────────────────────────────────────────────────────────────

@app.route("/admin", methods=["GET", "POST"])
def admin_gate():
    """
    Admin authentication gate.
    GET:  Render plain single-field passcode form.
    POST: Verify passcode.
      - Correct → set session flag, redirect to dashboard.
      - Wrong   → abort(404) immediately (FAKE 404 DECEPTION).
    """
    if request.method == "POST":
        entered = request.form.get("passcode", "")
        if entered == ADMIN_PASSCODE:
            session.clear()
            session["admin_authenticated"] = True
            session.permanent = True
            return redirect(url_for("admin_dashboard"))
        else:
            # CRITICAL DECEPTION: wrong passcode → fake 404
            abort(404)

    # If already authenticated, skip the gate
    if session.get("admin_authenticated"):
        return redirect(url_for("admin_dashboard"))

    return render_template("admin_gate.html")


@app.route("/admin/dashboard")
@admin_required
def admin_dashboard():
    """
    Private admin dashboard.
    Shows all inquiries and allows new product creation.
    Protected by @admin_required which triggers abort(404) for unauthorized access.
    """
    conn = get_db()
    cur = conn.cursor(dictionary=True)

    cur.execute("SELECT id, name, category, price FROM products ORDER BY created_at DESC")
    products = cur.fetchall()

    # Fetch all inquiries with product details, newest first
    cur.execute("""
        SELECT
            i.id,
            i.client_name,
            i.phone,
            i.message,
            i.created_at,
            p.id        AS product_id,
            p.name      AS product_name,
            p.category  AS product_category
        FROM inquiries i
        JOIN products p ON i.product_id = p.id
        ORDER BY i.created_at DESC
    """)
    inquiries = cur.fetchall()

    # Summary stats
    stats_cur = conn.cursor()

    stats_cur.execute("SELECT COUNT(*) FROM inquiries")
    (total_inquiries,) = stats_cur.fetchone()

    stats_cur.execute("SELECT COUNT(*) FROM products")
    (total_products,) = stats_cur.fetchone()

    stats_cur.execute("SELECT COUNT(*) FROM products WHERE category='carpet'")
    (carpet_count,) = stats_cur.fetchone()

    stats_cur.execute("SELECT COUNT(*) FROM products WHERE category='curtain'")
    (curtain_count,) = stats_cur.fetchone()

    stats_cur.execute("SELECT COUNT(*) FROM products WHERE category='doormat'")
    (doormat_count,) = stats_cur.fetchone()

    stats_cur.execute("SELECT COUNT(*) FROM products WHERE category='chair'")
    (chair_count,) = stats_cur.fetchone()

    stats_cur.execute("SELECT COUNT(*) FROM products WHERE category='table'")
    (table_count,) = stats_cur.fetchone()

    stats_cur.execute("SELECT COUNT(*) FROM products WHERE category='hanger'")
    (hanger_count,) = stats_cur.fetchone()
    
    cur.execute("SELECT DISTINCT brand FROM products WHERE brand != 'Unknown' ORDER BY brand")
    brands = [row["brand"] for row in cur.fetchall()]

    stats_cur.close()
    cur.close()
    conn.close()

    return render_template(
        "admin.html",
        inquiries=inquiries,
        total_inquiries=total_inquiries,
        total_products=total_products,
        carpet_count=carpet_count,
        curtain_count=curtain_count,
        doormat_count=doormat_count,
        chair_count=chair_count,
        table_count=table_count,
        hanger_count=hanger_count,
        brands=brands,
        products=products,
    )


@app.route("/admin/logout")
def admin_logout():
    """Clear admin session and redirect to home."""
    session.clear()
    return redirect(url_for("home"))


# ── Admin API — Products ──────────────────────────────────────────────────────

@app.route("/api/admin/products", methods=["POST"])
@admin_required
def api_admin_create_product():
    """
    Create a new product.
    Expects JSON or multipart form data with: name, category, description, price, image_url?, style_tags?, photo?
    After creation, invalidates the cached recommendation model so it's re-loaded
    on next request (the admin should also manually re-run recommendation.py).
    """
    data = request.get_json(silent=True) or request.form

    name        = str(data.get("name", "")).strip()
    category    = str(data.get("category", "")).strip().lower()
    brand       = str(data.get("brand", "Unknown")).strip()
    description = str(data.get("description", "")).strip()
    style_tags  = str(data.get("style_tags", "")).strip()
    image_url   = str(data.get("image_url", "")).strip()

    photo = request.files.get("photo")
    if photo and photo.filename:
        if not _allowed_image_filename(photo.filename):
            return jsonify({"success": False, "error": "Photo must be png, jpg, jpeg, gif, or webp."}), 400
        _ensure_upload_folder()
        safe_name = secure_filename(photo.filename)
        stamp = datetime.utcnow().strftime("%Y%m%d%H%M%S%f")
        stored_name = f"{stamp}_{safe_name}"
        photo_path = os.path.join(UPLOAD_FOLDER, stored_name)
        photo.save(photo_path)
        image_url = url_for("static", filename=f"uploads/{stored_name}")

    try:
        price = float(data.get("price", 0))
        assert price >= 0
    except (ValueError, AssertionError, TypeError):
        return jsonify({"success": False, "error": "Invalid price."}), 400

    if not name or not category or not description:
        return jsonify({"success": False, "error": "Name, category, and description are required."}), 400

    if category not in ("carpet", "curtain", "doormat", "chair", "table", "hanger"):
        return jsonify({"success": False, "error": "Category must be 'carpet', 'curtain', 'doormat', 'chair', 'table', or 'hanger'."}), 400

    conn = get_db()
    cur = conn.cursor()
    cur.execute(
        """INSERT INTO products (name, category, brand, description, price, image_url, style_tags)
           VALUES (%s, %s, %s, %s, %s, %s, %s)""",
        (name, category, brand, description, price, image_url, style_tags),
    )
    new_id = cur.lastrowid
    cur.close()
    conn.close()

    # Rebuild recommendations immediately so new products are available live.
    try:
        from recommendation import fetch_products, train_and_save

        reco_conn = get_db()
        try:
            df = fetch_products(reco_conn)
            train_and_save(df)
        finally:
            reco_conn.close()

        reload_reco_model()
    except Exception as exc:
        log.warning("Created product %d, but auto-training recommendations failed: %s", new_id, exc)

    log.info("Admin created new product #%d: '%s' (%s).", new_id, name, category)
    return jsonify({
        "success": True,
        "product_id": new_id,
        "message": f"Product '{name}' created. AI recommendations updated.",
    }), 201


@app.route("/api/admin/products/<int:product_id>", methods=["DELETE"])
@admin_required
def api_admin_delete_product(product_id: int):
    """Delete a product and refresh recommendations."""
    conn = get_db()
    cur = conn.cursor(dictionary=True)
    cur.execute("SELECT id FROM products WHERE id = %s", (product_id,))
    product = cur.fetchone()
    if not product:
        cur.close()
        conn.close()
        return jsonify({"success": False, "error": "Product not found."}), 404

    cur.execute("DELETE FROM products WHERE id = %s", (product_id,))
    cur.close()
    conn.close()

    try:
        from recommendation import fetch_products, train_and_save

        reco_conn = get_db()
        try:
            df = fetch_products(reco_conn)
            train_and_save(df)
        finally:
            reco_conn.close()

        reload_reco_model()
    except Exception as exc:
        log.warning("Deleted product %d, but auto-training recommendations failed: %s", product_id, exc)

    return jsonify({"success": True, "message": "Product deleted."}), 200


@app.route("/api/admin/inquiries", methods=["GET"])
@admin_required
def api_admin_get_inquiries():
    """Return all inquiries as JSON (for AJAX dashboard refresh)."""
    conn = get_db()
    cur = conn.cursor(dictionary=True)
    cur.execute("""
        SELECT i.id, i.client_name, i.phone, i.message,
               i.created_at, p.name AS product_name, p.category AS product_category
        FROM inquiries i
        JOIN products p ON i.product_id = p.id
        ORDER BY i.created_at DESC
    """)
    rows = cur.fetchall()
    cur.close()
    conn.close()

    # Serialize datetimes
    for row in rows:
        if isinstance(row.get("created_at"), datetime):
            row["created_at"] = row["created_at"].strftime("%d %b %Y, %I:%M %p")

    return jsonify({"success": True, "inquiries": rows})


# ── Application Startup ───────────────────────────────────────────────────────
def create_app():
    """Initialize DB and load recommendation model before serving requests."""
    with app.app_context():
        try:
            init_database()
        except Exception as exc:
            log.critical("Database initialization failed: %s", exc)
            raise
        load_reco_model()
    return app


if __name__ == "__main__":
    create_app()
    # Note: In development, SESSION_COOKIE_SECURE should be False.
    # Override here for local testing:
    app.config["SESSION_COOKIE_SECURE"] = False
    app.run(
        host="0.0.0.0",
        port=5000,
        debug=os.getenv("FLASK_DEBUG", "True").lower() == "true",
    )
