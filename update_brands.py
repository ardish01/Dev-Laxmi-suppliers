import os
import mysql.connector
from dotenv import load_dotenv

load_dotenv()
conn = mysql.connector.connect(
    host=os.getenv('DB_HOST', 'localhost'),
    port=int(os.getenv('DB_PORT', 3306)),
    user=os.getenv('DB_USER', 'root'),
    password=os.getenv('DB_PASSWORD', ''),
    database=os.getenv('DB_NAME', 'devlaxmi_db')
)
cur = conn.cursor()

# Update carpets to Heritage and Yeti
cur.execute("UPDATE products SET brand='Heritage' WHERE id IN (1, 2, 5, 6)")
cur.execute("UPDATE products SET brand='Yeti' WHERE id IN (3, 4, 7, 8)")
cur.execute("UPDATE products SET brand='Unknown' WHERE brand='' OR brand IS NULL")
conn.commit()
print('Brands updated for existing products')
