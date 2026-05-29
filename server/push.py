import json
import os
from server.database import get_db

def send_push_to(person_name: str, title: str, body: str):
    db = get_db()
    subscriptions = db.execute(
        "SELECT subscription FROM push_subscriptions WHERE person_name = ?",
        (person_name,)
    ).fetchall()
    db.close()

    for row in subscriptions:
        _send(json.loads(row["subscription"]), title, body)

def _send(subscription: dict, title: str, body: str):
    try:
        from pywebpush import webpush
        private_key = os.environ.get("VAPID_PRIVATE_KEY", "")
        email = os.environ.get("VAPID_EMAIL", "mailto:example@example.com")
        if not private_key:
            return
        webpush(
            subscription_info=subscription,
            data=json.dumps({"title": title, "body": body}),
            vapid_private_key=private_key,
            vapid_claims={"sub": email},
        )
    except Exception:
        pass
