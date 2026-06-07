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

# ── Optional psycopg2 import ──────────────────────────────────────────────────
try:
    import psycopg2
    import psycopg2.extras          # RealDictCursor
    import psycopg2.pool
    import psycopg2.errors
    from psycopg2 import OperationalError as PGOperationalError
except ImportError as exc:
    raise RuntimeError(
        "psycopg2 is required. Run: pip install psycopg2-binary"
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
    "port":     int(os.getenv("DB_PORT", 5432)),
    "user":     os.getenv("DB_USER", "postgres"),
    "password": os.getenv("DB_PASSWORD", ""),
    "dbname":   os.getenv("DB_NAME", "devlaxmi_db"),
}

# ── Database Connection Pool ──────────────────────────────────────────────────
_pool: psycopg2.pool.ThreadedConnectionPool | None = None


def get_pool() -> psycopg2.pool.ThreadedConnectionPool:
    """Return (or lazily create) the PostgreSQL connection pool."""
    global _pool
    if _pool is None:
        _pool = psycopg2.pool.ThreadedConnectionPool(
            minconn=1,
            maxconn=5,
            **DB_CONFIG,
        )
        log.info("PostgreSQL connection pool created (maxconn=5).")
    return _pool


def get_db():
    """Get a connection from the pool."""
    return get_pool().getconn()


def release_db(conn) -> None:
    """Return a connection to the pool."""
    get_pool().putconn(conn)


# ── Database Initialization ───────────────────────────────────────────────────
DEMO_PRODUCTS = []


def init_database() -> None:
    """
    Ensure tables and seed data exist.
    Called once at application startup.
    The database itself is assumed to already exist (e.g. provisioned by Render).
    """
    _ensure_upload_folder()

    conn = psycopg2.connect(**DB_CONFIG)
    conn.autocommit = True
    cur = conn.cursor()

    # ── products table ────────────────────────────────────────────────────────
    cur.execute("""
        CREATE TABLE IF NOT EXISTS products (
            id           SERIAL PRIMARY KEY,
            name         VARCHAR(255)   NOT NULL,
            category     VARCHAR(20)    NOT NULL
                             CHECK (category IN ('carpet','curtain','doormat','chair','table','hanger')),
            brand        VARCHAR(100)   NOT NULL DEFAULT 'Unknown',
            description  TEXT           NOT NULL,
            price        NUMERIC(10, 2) NOT NULL DEFAULT 0.00,
            image_url    VARCHAR(512)   NOT NULL DEFAULT '',
            style_tags   TEXT           NOT NULL DEFAULT '',
            created_at   TIMESTAMP      NOT NULL DEFAULT NOW(),
            updated_at   TIMESTAMP      NOT NULL DEFAULT NOW()
        )
    """)
    cur.execute("CREATE INDEX IF NOT EXISTS idx_products_category ON products(category)")

    # Add brand column if upgrading from an older schema — must run before the brand index
    cur.execute("""
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM information_schema.columns
                WHERE table_name='products' AND column_name='brand'
            ) THEN
                ALTER TABLE products ADD COLUMN brand VARCHAR(100) NOT NULL DEFAULT 'Unknown';
            END IF;
        END;
        $$
    """)
    cur.execute("CREATE INDEX IF NOT EXISTS idx_products_brand ON products(brand)")

    # Trigger to keep updated_at current (equivalent to ON UPDATE CURRENT_TIMESTAMP)
    cur.execute("""
        CREATE OR REPLACE FUNCTION set_updated_at()
        RETURNS TRIGGER LANGUAGE plpgsql AS $$
        BEGIN
            NEW.updated_at = NOW();
            RETURN NEW;
        END;
        $$
    """)
    cur.execute("""
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM pg_trigger
                WHERE tgname = 'trg_products_updated_at'
            ) THEN
                CREATE TRIGGER trg_products_updated_at
                BEFORE UPDATE ON products
                FOR EACH ROW EXECUTE FUNCTION set_updated_at();
            END IF;
        END;
        $$
    """)

    # ── inquiries table ───────────────────────────────────────────────────────
    cur.execute("""
        CREATE TABLE IF NOT EXISTS inquiries (
            id           SERIAL PRIMARY KEY,
            product_id   INTEGER        NOT NULL
                             REFERENCES products(id) ON DELETE CASCADE ON UPDATE CASCADE,
            client_name  VARCHAR(255)   NOT NULL,
            phone        VARCHAR(20)    NOT NULL,
            message      TEXT,
            created_at   TIMESTAMP      NOT NULL DEFAULT NOW()
        )
    """)
    cur.execute("CREATE INDEX IF NOT EXISTS idx_inquiries_product   ON inquiries(product_id)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_inquiries_created   ON inquiries(created_at)")

    # ── Seed demo data if empty ───────────────────────────────────────────────
    cur.execute("SELECT COUNT(*) FROM products")
    (count,) = cur.fetchone()
    if count == 0 and DEMO_PRODUCTS:
        log.info("Seeding %d demo products…", len(DEMO_PRODUCTS))
        sql = """
            INSERT INTO products (name, category, brand, description, price, image_url, style_tags)
            VALUES (%(name)s, %(category)s, %(brand)s, %(description)s,
                    %(price)s, %(image_url)s, %(style_tags)s)
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
    """
    mapping = {
        "carpet":  "curtain",
        "curtain": "doormat",
        "doormat": "carpet",
        "chair":   "table",
        "table":   "chair",
        "hanger":  "chair",
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

    log.info("Using fallback recommendations for product %d.", product_id)
    return _fetch_random_products(opposite, top_n, exclude_id=product_id)


def _fetch_products_by_ids(ids: list[int]) -> list[dict]:
    """Fetch full product rows for a list of IDs, preserving order."""
    if not ids:
        return []
    # psycopg2 can bind a tuple directly for ANY(ARRAY[...]) or use IN with a tuple
    query = "SELECT * FROM products WHERE id = ANY(%s)"
    conn = get_db()
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(query, (list(ids),))
        rows = [dict(r) for r in cur.fetchall()]
        cur.close()
    finally:
        release_db(conn)
    order = {pid: i for i, pid in enumerate(ids)}
    rows.sort(key=lambda r: order.get(r["id"], 999))
    return rows


def _fetch_random_products(category: str, limit: int, exclude_id: int = 0) -> list[dict]:
    """Fetch random products of a given category, excluding a specific product ID."""
    query = "SELECT * FROM products WHERE category=%s AND id != %s ORDER BY RANDOM() LIMIT %s"
    conn = get_db()
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(query, (category, exclude_id, limit))
        rows = [dict(r) for r in cur.fetchall()]
        cur.close()
    finally:
        release_db(conn)
    return rows


# ── Admin Auth Guard ──────────────────────────────────────────────────────────
def admin_required(f):
    """
    Decorator that enforces admin authentication.
    CRITICAL DECEPTION RULE: Any unauthorized access triggers abort(404).
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
    return render_template("404.html"), 404


@app.errorhandler(500)
def internal_error(e):
    log.error("Internal server error: %s", e)
    return render_template("500.html"), 500


# ── Public Routes ─────────────────────────────────────────────────────────────

@app.route("/")
def home():
    category_filter = request.args.get("category", "all").lower()
    conn = get_db()
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

        if category_filter in ("carpet", "curtain", "doormat", "chair", "table", "hanger"):
            cur.execute(
                "SELECT * FROM products WHERE category = %s ORDER BY created_at DESC",
                (category_filter,)
            )
        else:
            cur.execute("SELECT * FROM products ORDER BY category, created_at DESC")

        products = [dict(r) for r in cur.fetchall()]

        for p in products:
            p["price"] = float(p["price"])

        carpet_count  = sum(1 for p in products if p["category"] == "carpet")
        curtain_count = sum(1 for p in products if p["category"] == "curtain")
        doormat_count = sum(1 for p in products if p["category"] == "doormat")
        chair_count   = sum(1 for p in products if p["category"] == "chair")
        table_count   = sum(1 for p in products if p["category"] == "table")
        hanger_count  = sum(1 for p in products if p["category"] == "hanger")
        furniture_products = [p for p in products if p["category"] in ("chair", "table", "hanger")]

        cur.execute("SELECT DISTINCT brand FROM products WHERE brand != 'Unknown' ORDER BY brand")
        brands = [row["brand"] for row in cur.fetchall()]

        cur.close()
    finally:
        release_db(conn)

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
    conn = get_db()
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("SELECT * FROM products WHERE id = %s", (product_id,))
        product = cur.fetchone()
        product = dict(product) if product else None
        cur.close()
    finally:
        release_db(conn)

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
    data = request.get_json(silent=True) or {}

    product_id  = data.get("product_id")
    client_name = str(data.get("client_name", "")).strip()
    phone       = str(data.get("phone", "")).strip()
    message     = str(data.get("message", "")).strip()

    if not product_id or not client_name or not phone:
        return jsonify({"success": False, "error": "Product ID, name, and phone are required."}), 400

    if not isinstance(product_id, int) or product_id < 1:
        return jsonify({"success": False, "error": "Invalid product ID."}), 400

    if len(client_name) > 255:
        return jsonify({"success": False, "error": "Name is too long."}), 400

    import re
    if not re.match(r"^[\d\s\+\-\(\)]{7,20}$", phone):
        return jsonify({"success": False, "error": "Invalid phone number format."}), 400

    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute("SELECT id FROM products WHERE id = %s", (product_id,))
        if not cur.fetchone():
            cur.close()
            return jsonify({"success": False, "error": "Product not found."}), 404

        cur.execute(
            "INSERT INTO inquiries (product_id, client_name, phone, message) VALUES (%s, %s, %s, %s)",
            (product_id, client_name, phone, message or None),
        )
        conn.commit()
        cur.close()
    finally:
        release_db(conn)

    log.info("New inquiry from '%s' (%s) for product #%d.", client_name, phone, product_id)
    return jsonify({"success": True, "message": "Your inquiry has been received. We'll contact you shortly!"}), 201


# ── Admin Routes ──────────────────────────────────────────────────────────────

@app.route("/admin", methods=["GET", "POST"])
def admin_gate():
    if request.method == "POST":
        entered = request.form.get("passcode", "")
        if entered == ADMIN_PASSCODE:
            session.clear()
            session["admin_authenticated"] = True
            session.permanent = True
            return redirect(url_for("admin_dashboard"))
        else:
            abort(404)

    if session.get("admin_authenticated"):
        return redirect(url_for("admin_dashboard"))

    return render_template("admin_gate.html")


@app.route("/admin/dashboard")
@admin_required
def admin_dashboard():
    conn = get_db()
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

        cur.execute("SELECT id, name, category, price FROM products ORDER BY created_at DESC")
        products = [dict(r) for r in cur.fetchall()]

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
        inquiries = [dict(r) for r in cur.fetchall()]

        stats_cur = conn.cursor()

        def _count(query, *args):
            stats_cur.execute(query, args)
            return stats_cur.fetchone()[0]

        total_inquiries = _count("SELECT COUNT(*) FROM inquiries")
        total_products  = _count("SELECT COUNT(*) FROM products")
        carpet_count    = _count("SELECT COUNT(*) FROM products WHERE category='carpet'")
        curtain_count   = _count("SELECT COUNT(*) FROM products WHERE category='curtain'")
        doormat_count   = _count("SELECT COUNT(*) FROM products WHERE category='doormat'")
        chair_count     = _count("SELECT COUNT(*) FROM products WHERE category='chair'")
        table_count     = _count("SELECT COUNT(*) FROM products WHERE category='table'")
        hanger_count    = _count("SELECT COUNT(*) FROM products WHERE category='hanger'")

        cur.execute("SELECT DISTINCT brand FROM products WHERE brand != 'Unknown' ORDER BY brand")
        brands = [row["brand"] for row in cur.fetchall()]

        stats_cur.close()
        cur.close()
    finally:
        release_db(conn)

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
    session.clear()
    return redirect(url_for("home"))


# ── Admin API — Products ──────────────────────────────────────────────────────

@app.route("/api/admin/products", methods=["POST"])
@admin_required
def api_admin_create_product():
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
        safe_name   = secure_filename(photo.filename)
        stamp       = datetime.utcnow().strftime("%Y%m%d%H%M%S%f")
        stored_name = f"{stamp}_{safe_name}"
        photo_path  = os.path.join(UPLOAD_FOLDER, stored_name)
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
    try:
        cur = conn.cursor()
        cur.execute(
            """INSERT INTO products (name, category, brand, description, price, image_url, style_tags)
               VALUES (%s, %s, %s, %s, %s, %s, %s)
               RETURNING id""",
            (name, category, brand, description, price, image_url, style_tags),
        )
        (new_id,) = cur.fetchone()
        conn.commit()
        cur.close()
    finally:
        release_db(conn)

    reload_reco_model()

    log.info("Admin created new product #%d: '%s' (%s).", new_id, name, category)
    return jsonify({
        "success": True,
        "product_id": new_id,
        "message": f"Product '{name}' created.",
    }), 201


@app.route("/api/admin/products/<int:product_id>", methods=["DELETE"])
@admin_required
def api_admin_delete_product(product_id: int):
    conn = get_db()
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("SELECT id FROM products WHERE id = %s", (product_id,))
        product = cur.fetchone()
        if not product:
            cur.close()
            return jsonify({"success": False, "error": "Product not found."}), 404

        cur.execute("DELETE FROM products WHERE id = %s", (product_id,))
        conn.commit()
        cur.close()
    finally:
        release_db(conn)

    reload_reco_model()
    return jsonify({"success": True, "message": "Product deleted."}), 200


@app.route("/api/admin/inquiries", methods=["GET"])
@admin_required
def api_admin_get_inquiries():
    conn = get_db()
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("""
            SELECT i.id, i.client_name, i.phone, i.message,
                   i.created_at, p.name AS product_name, p.category AS product_category
            FROM inquiries i
            JOIN products p ON i.product_id = p.id
            ORDER BY i.created_at DESC
        """)
        rows = [dict(r) for r in cur.fetchall()]
        cur.close()
    finally:
        release_db(conn)

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
    app.config["SESSION_COOKIE_SECURE"] = False
    app.run(
        host="0.0.0.0",
        port=5000,
        debug=os.getenv("FLASK_DEBUG", "True").lower() == "true",
    )
