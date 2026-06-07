from app import get_db, app
with app.app_context():
    conn = get_db()
    cur = conn.cursor()
    cur.execute("ALTER TABLE products MODIFY COLUMN category ENUM('carpet','curtain','doormat','chair','table','hanger') NOT NULL;")
    cur.close()
    conn.close()
    print('DB altered for chair, table, hanger')
