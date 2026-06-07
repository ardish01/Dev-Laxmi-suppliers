"""
recommendation.py — Dev Laxmi Suppliers
========================================
Standalone training script to compute TF-IDF cosine similarity across all products.
Run this script independently whenever new products are added to retrain the model:

    python recommendation.py

It connects to MySQL, loads all products, computes a cosine similarity matrix
from style_tags + category fields, and pickles the result to:
    models/similarity_matrix.pkl

The app.py server loads this pickle at startup for fast recommendations.
"""

import os
import sys
import pickle
import logging

import pandas as pd
from dotenv import load_dotenv
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics.pairwise import cosine_similarity

# ── Optional MySQL import ─────────────────────────────────────────────────────
try:
    import mysql.connector
    from mysql.connector import Error as MySQLError
except ImportError:
    print("[ERROR] mysql-connector-python is not installed. Run: pip install mysql-connector-python")
    sys.exit(1)

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [RECO] %(levelname)s — %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

# ── Configuration ─────────────────────────────────────────────────────────────
load_dotenv()

DB_CONFIG = {
    "host":     os.getenv("DB_HOST", "localhost"),
    "port":     int(os.getenv("DB_PORT", 3306)),
    "user":     os.getenv("DB_USER", "root"),
    "password": os.getenv("DB_PASSWORD", ""),
    "database": os.getenv("DB_NAME", "devlaxmi_db"),
}

MODEL_DIR = os.path.join(os.path.dirname(__file__), "models")
PICKLE_PATH = os.path.join(MODEL_DIR, "similarity_matrix.pkl")
MIN_PRODUCTS = 2  # Need at least 2 products to compute meaningful similarity


# ── Database helpers ──────────────────────────────────────────────────────────

def get_connection():
    """Return a MySQL connection, raising a clear error on failure."""
    try:
        conn = mysql.connector.connect(**DB_CONFIG)
        log.info("Connected to MySQL database '%s'.", DB_CONFIG["database"])
        return conn
    except MySQLError as exc:
        log.error("Cannot connect to MySQL: %s", exc)
        raise


def fetch_products(conn) -> pd.DataFrame:
    """Load all products from the database into a DataFrame."""
    query = "SELECT id, name, category, style_tags FROM products ORDER BY id"
    cursor = conn.cursor(dictionary=True)
    cursor.execute(query)
    rows = cursor.fetchall()
    cursor.close()
    df = pd.DataFrame(rows)
    log.info("Fetched %d products from database.", len(df))
    return df


# ── Feature engineering ───────────────────────────────────────────────────────

def build_feature_string(row: pd.Series) -> str:
    """
    Combine category and style_tags into a single descriptive string for TF-IDF.
    Example: "carpet floral traditional handwoven persian wool"
    """
    category = str(row.get("category", "")).strip().lower()
    tags = str(row.get("style_tags", "")).strip().lower()
    # Repeat category twice to give it extra weight in TF-IDF
    return f"{category} {category} {tags}"


# ── Model training ────────────────────────────────────────────────────────────

def train_and_save(df: pd.DataFrame) -> None:
    """
    Build the TF-IDF matrix, compute pairwise cosine similarity,
    and pickle both the similarity matrix and the product ID index map.
    """
    if len(df) < MIN_PRODUCTS:
        log.warning(
            "Only %d product(s) found — need at least %d to build a meaningful "
            "similarity matrix. Add more products and re-run.",
            len(df), MIN_PRODUCTS,
        )
        return

    # Build feature strings
    df["features"] = df.apply(build_feature_string, axis=1)

    # TF-IDF vectorization
    vectorizer = TfidfVectorizer(
        stop_words="english",
        ngram_range=(1, 2),   # unigrams + bigrams for richer matching
        min_df=1,
        sublinear_tf=True,    # Apply log(tf) scaling
    )
    tfidf_matrix = vectorizer.fit_transform(df["features"])
    log.info(
        "TF-IDF matrix shape: %d products × %d features.",
        tfidf_matrix.shape[0], tfidf_matrix.shape[1],
    )

    # Cosine similarity
    similarity_matrix = cosine_similarity(tfidf_matrix, tfidf_matrix)
    log.info("Cosine similarity matrix computed: %s.", similarity_matrix.shape)

    # Build index map: product_id → row index in the matrix
    id_to_index = {int(row["id"]): idx for idx, row in df.iterrows()}
    index_to_id = {v: k for k, v in id_to_index.items()}

    # Package everything into a single payload
    payload = {
        "similarity_matrix": similarity_matrix,
        "id_to_index":        id_to_index,
        "index_to_id":        index_to_id,
        "product_ids":        df["id"].tolist(),
        "categories":         dict(zip(df["id"].tolist(), df["category"].tolist())),
        "num_products":       len(df),
    }

    # Ensure output directory exists
    os.makedirs(MODEL_DIR, exist_ok=True)

    with open(PICKLE_PATH, "wb") as fh:
        pickle.dump(payload, fh, protocol=pickle.HIGHEST_PROTOCOL)

    log.info("Model saved to '%s'. (%d products indexed)", PICKLE_PATH, len(df))


# ── Entry point ───────────────────────────────────────────────────────────────

def main():
    log.info("=== Dev Laxmi Suppliers — Recommendation Engine Trainer ===")
    log.info("Target pickle: %s", PICKLE_PATH)

    conn = get_connection()
    try:
        df = fetch_products(conn)
        if df.empty:
            log.warning("No products found in database. Populate the products table first.")
            return
        train_and_save(df)
        log.info("Training complete. Run the Flask server to use recommendations.")
    finally:
        conn.close()
        log.info("Database connection closed.")


if __name__ == "__main__":
    main()
