import json
from app.core import store
from app.adapters import mailer

class OrderBook:
    def add(self, order):
        store.save(order)
        return _audit(order)

def approve(order_id):
    row = store.load(order_id)
    mailer.send(row)
    return row

def _audit(order):
    return {"ok": True}
