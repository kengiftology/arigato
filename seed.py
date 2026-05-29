import sys, io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')

from server.database import get_db, init_db

init_db()
db = get_db()

zones = [
    ('kitchen',  'キッチン'),
    ('bathroom', 'バスルーム'),
    ('common',   '共有スペース'),
    ('entrance', '入り口'),
    ('trash',    'ゴミ捨て場'),
]

for z in zones:
    try:
        db.execute('INSERT INTO zones (id, name) VALUES (?,?)', z)
    except Exception:
        pass

db.commit()
db.close()
print("ゾーン追加完了")
