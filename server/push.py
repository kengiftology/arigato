import json, os
from server.database import get_db

def send_push_to(person_name: str, title: str, body: str):
    db = get_db()
    docs = (db.collection("push_subscriptions")
              .where("person_name", "==", person_name)
              .stream())
    for doc in docs:
        _send(doc.to_dict()["subscription"], title, body)

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
