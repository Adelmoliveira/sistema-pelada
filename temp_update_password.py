import sqlite3
from werkzeug.security import generate_password_hash

conn = sqlite3.connect('bar.db')
new_password = 'BarPeladeiros2026!'
conn.execute("UPDATE users SET password_hash=?, password_required=1 WHERE LOWER(username)=LOWER('adelmoliveira')", (generate_password_hash(new_password),))
conn.commit()
print(new_password)
conn.close()
