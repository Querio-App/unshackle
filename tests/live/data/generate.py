"""Write a small synthetic Dunder Mifflin dataset to tests/live/data/dunder_mifflin/*.csv.

Deterministic (seeded, stdlib only). Follows the distributions in the Dunder Mifflin
data maintainer spec: H1 2025, ~2.7 orders a day with seasonal multipliers, order
status 84/10/6, five paper types, shipping mode and region splits, reviews on ~67% of
completed orders 7-30 days after purchase.

    python tests/live/data/generate.py
"""

from __future__ import annotations

import csv
import random
from datetime import date, datetime, timedelta
from pathlib import Path

OUT = Path(__file__).parent / "dunder_mifflin"
START, END = date(2025, 1, 1), date(2025, 7, 1)  # half-open
SEASON = {1: 1.0, 2: 1.0, 3: 1.0, 4: 0.9, 5: 1.1, 6: 1.1, 7: 1.1, 8: 1.0, 9: 0.8, 10: 1.2, 11: 1.2, 12: 1.2}

MANAGERS = {
    "Dwight Schrute": 22, "Jim Halpert": 12, "Phyllis Vance": 9, "Stanley Hudson": 8, "Andy Bernard": 7,
    "Michael Scott": 6, "Karen Filippelli": 5, "Clark Green": 5, "Pete Miller": 5, "Ryan Howard": 5,
    "Todd Packer": 5, "Danny Cordray": 5, "Holly Flax": 3, "Ed Truck": 3,
}
PAYMENT_CYCLES = {"ANNUAL": 34, "BIWEEK": 25, "WEEK": 25, "MONTH": 16}
PAPER_TYPES = {"Multipurpose": 39, "Printer": 19, "Cardstock": 14, "Kraft Roll": 14, "Copy": 14}
STATUSES = {"COMPLETED": 84, "CANCELLED": 10, "RETURNED": 6}
MODES = {"EXPRESS": 51, "REGULAR": 49}
REGIONS = {"West": 34, "South": 31, "Northeast": 22, "Midwest": 12}
RATINGS = {5: 29, 4: 32, 3: 31, 2: 5, 1: 3}
CITIES = {
    "West": [("Los Angeles", "CA"), ("Seattle", "WA"), ("Denver", "CO"), ("Phoenix", "AZ")],
    "South": [("Austin", "TX"), ("Atlanta", "GA"), ("Nashville", "TN"), ("Miami", "FL")],
    "Northeast": [("Scranton", "PA"), ("Stamford", "CT"), ("Nashua", "NH"), ("Buffalo", "NY")],
    "Midwest": [("Chicago", "IL"), ("Columbus", "OH"), ("Detroit", "MI"), ("Akron", "OH")],
}
COMPANIES = [
    "Blue Cross of Pennsylvania", "Lackawanna County", "Harper Collins", "Apex Technology", "Scranton White Pages",
    "Pinnacle Insurance", "Stamford Medical", "Vance Refrigeration", "Poor Richard's Pub", "Cugini's Restaurant",
    "Utica Paper Mills", "Hammermill Supply", "Nashua Logistics", "Buffalo Office Depot", "Akron Print Shop",
    "Schrute Farms", "Serenity by Jan", "Sabre Corporation", "Wuphf Media", "Athlead",
    "Mega Desk Supply", "Penn Paper", "Keystone Law", "Valley Dental", "Steamtown Mall",
    "Wilkes University", "Electric City Energy", "Montrose Diner", "Dickson City Library", "Carbondale Credit Union",
    "Clarks Summit Clinic", "Moosic Motors", "Old Forge Pizza", "Taylor Builders", "Dunmore Schools",
    "Pittston Tool Co", "Hazleton Packaging", "Allentown Accounting", "Bethlehem Steelworks", "Erie Lakeside Hotel",
]
PRODUCTS = [  # (name, paper type, unit price)
    ("Multipurpose 20lb Letter", "Multipurpose", 42.00), ("Multipurpose 24lb Legal", "Multipurpose", 55.50),
    ("Multipurpose Recycled", "Multipurpose", 47.25), ("Laser Printer Bright White", "Printer", 61.00),
    ("Inkjet Printer Premium", "Printer", 73.80), ("Cardstock 65lb Cover", "Cardstock", 88.40),
    ("Cardstock 110lb Index", "Cardstock", 112.00), ("Kraft Roll 30in", "Kraft Roll", 96.75),
    ("Kraft Roll 48in Heavy", "Kraft Roll", 134.90), ("Copy Paper Value Case", "Copy", 34.99),
    ("Copy Paper Colored", "Copy", 39.60),
]


def pick(rng: random.Random, weights: dict):
    return rng.choices(list(weights), weights=list(weights.values()))[0]


def ts(d: datetime) -> str:
    return d.strftime("%Y-%m-%d %H:%M:%S")


def generate(seed: int = 2025) -> dict[str, list[dict]]:
    rng = random.Random(seed)
    accounts = []
    for i, name in enumerate(COMPANIES, 1):
        size_band = rng.choices(["S", "M", "L"], weights=[40, 40, 20])[0]
        lo, hi = {"S": (15, 200), "M": (200, 1200), "L": (1200, 4632)}[size_band]
        accounts.append({
            "account_id": i, "account_name": name, "account_manager": pick(rng, MANAGERS),
            "payment_cycle": pick(rng, PAYMENT_CYCLES), "business_size": rng.randint(lo, hi),
            "created_at": ts(datetime(2023, 1, 1) + timedelta(days=rng.randint(0, 700), seconds=rng.randint(0, 86399))),
        })
    products = [{"product_id": i, "product_name": n, "paper_type": t, "price": f"{p:.2f}"} for i, (n, t, p) in enumerate(PRODUCTS, 1)]
    by_type: dict[str, list[dict]] = {}
    for p in products:
        by_type.setdefault(p["paper_type"], []).append(p)

    orders, shippings, reviews = [], [], []
    day = START
    while day < END:
        n = round(2.7 * SEASON[day.month] + rng.uniform(-0.5, 0.5))
        for _ in range(max(n, 0)):
            oid = len(orders) + 1
            acct = rng.choice(accounts)
            prod = rng.choice(by_type[pick(rng, PAPER_TYPES)])
            qty = min(228, max(9, round(rng.gauss(110, 35))))
            pre = qty * float(prod["price"])
            disc = min(0.5, max(0.0035, rng.betavariate(2.2, 22)))
            purchased = datetime.combine(day, datetime.min.time()) + timedelta(seconds=rng.randint(8 * 3600, 18 * 3600))
            status = pick(rng, STATUSES)
            close = purchased + timedelta(days=min(90, max(50, round(rng.gauss(66, 8)))))
            orders.append({
                "order_id": oid, "account_id": acct["account_id"], "product_id": prod["product_id"], "status": status,
                "quantity": qty, "pre_discount_revenue": f"{pre:.2f}", "discount_percentage": f"{disc * 100:.2f}",
                "post_discount_revenue": f"{pre * (1 - disc):.2f}", "purchased_at": ts(purchased),
                "cancelled_at": ts(close) if status == "CANCELLED" else "",
                "returned_at": ts(close) if status == "RETURNED" else "",
            })
            region = pick(rng, REGIONS)
            city, state = rng.choice(CITIES[region])
            ship_days = rng.randint(3, 7)
            shippings.append({
                "shipping_id": oid, "order_id": oid, "shipping_mode": pick(rng, MODES), "region": region, "city": city,
                "state": state, "days_to_ship": ship_days, "shipped_at": ts(purchased + timedelta(days=ship_days)),
            })
            if status == "COMPLETED" and rng.random() < 0.67:
                reviews.append({
                    "review_id": len(reviews) + 1, "order_id": oid, "rating": pick(rng, RATINGS),
                    "reviewed_at": ts(purchased + timedelta(days=rng.randint(7, 30), seconds=rng.randint(0, 86399))),
                })
        day += timedelta(days=1)
    return {"accounts": accounts, "products": products, "orders": orders, "shippings": shippings, "reviews": reviews}


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    for table, rows in generate().items():
        with (OUT / f"{table}.csv").open("w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(rows[0]))
            w.writeheader()
            w.writerows(rows)
        print(f"{table}: {len(rows)} rows")


if __name__ == "__main__":
    main()
