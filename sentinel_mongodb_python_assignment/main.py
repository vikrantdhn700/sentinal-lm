"""Load Project SENTINEL data and demonstrate MongoDB building blocks."""
from __future__ import annotations

import argparse
import csv
import gzip
import json
import os
from datetime import datetime, timedelta, timezone
from itertools import islice
from pathlib import Path
from typing import Any, Iterator

from dotenv import load_dotenv
from pymongo import ASCENDING, DESCENDING, MongoClient, UpdateOne
from pymongo.errors import ConfigurationError, OperationFailure, PyMongoError

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env")
INTEGER_FIELDS = {
    "fraud_label", "customer_age", "account_age", "previous_transactions",
    "failed_transactions", "amount_paise",
}
DECIMAL_FIELDS = {"transaction_amount", "average_transaction_value"}
DATE_FIELDS = {"created_at", "transaction_time", "label_available_at"}


def iso_datetime(value: str) -> datetime:
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return (dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)).astimezone(timezone.utc)


def parse_value(value: str | None, field: str = "") -> Any:
    """Convert by schema: IDs stay strings, labels become integers, dates BSON dates."""
    if value is None or value == "":
        return None
    if field in INTEGER_FIELDS:
        return int(value)
    if field in DECIMAL_FIELDS:
        return float(value)
    if field in DATE_FIELDS:
        return iso_datetime(value)
    if value in ("true", "false"):
        return value == "true"
    if value.startswith(("{", "[")):
        return json.loads(value)
    return value


def read_table(table: str, data_dir: Path) -> Iterator[dict[str, Any]]:
    """Stream the original Project kit's CSV shards in deterministic path order."""
    paths = sorted((data_dir / "tables" / table).glob("**/*.csv.gz"))
    if not paths:
        raise FileNotFoundError(f"No CSV shards for {table}. Check SENTINEL_DATA_DIR.")
    for path in paths:
        with gzip.open(path, "rt", encoding="utf-8", newline="") as handle:
            for row in csv.DictReader(handle):
                yield {key: parse_value(value, key) for key, value in row.items()}


def prepare_documents(data_dir: Path, limit: int) -> tuple[list[dict], list[dict]]:
    """Join only the sampled IDs; validate the entire batch before database writes."""
    if limit < 5000:
        raise ValueError("TRANSACTION_LIMIT must be at least 5000 for this assignment.")
    rows = list(islice(read_table("transactions", data_dir), limit))
    if len(rows) < limit:
        raise ValueError(f"Requested {limit} transactions, but found only {len(rows)}.")
    tx_ids = {row["transaction_id"] for row in rows}
    if len(tx_ids) != len(rows):
        raise ValueError("Duplicate transaction IDs in the selected source sample.")
    customer_ids = {row["customer_id"] for row in rows}
    merchant_ids = {row["merchant_id"] for row in rows}
    device_ids = {row["device_id"] for row in rows if row.get("device_id")}
    customers = {r["customer_id"]: r for r in read_table("customers", data_dir)
                 if r["customer_id"] in customer_ids}
    merchants = {r["merchant_id"]: r for r in read_table("merchants", data_dir)
                 if r["merchant_id"] in merchant_ids}
    devices = {r["device_id"]: r for r in read_table("devices", data_dir)
               if r["device_id"] in device_ids}
    labels = {r["transaction_id"]: r for r in read_table("labels", data_dir)
              if r["transaction_id"] in tx_ids}
    if customer_ids - customers.keys() or merchant_ids - merchants.keys() or device_ids - devices.keys():
        raise ValueError("Some sampled transactions have missing customer, merchant or device records.")

    customer_devices: dict[str, dict[str, dict]] = {}
    transactions = []
    for row in rows:
        cid, did = row["customer_id"], row.get("device_id")
        merchant = merchants[row["merchant_id"]]
        label = labels.get(row["transaction_id"], {})
        if type(label.get("fraud_label")) is not int or label["fraud_label"] not in (0, 1):
            raise ValueError(f"Missing or invalid fraud label for {row['transaction_id']}.")
        if not isinstance(row["transaction_time"], datetime):
            raise ValueError("Every transaction must have a valid UTC timestamp.")
        if did:
            customer_devices.setdefault(cid, {})[did] = {
                "device_id": did, "device_kind": devices[did]["device_kind"],
            }
        transactions.append({
            **row,
            "customer": {"customer_id": cid, "age": row["customer_age"],
                         "location": row["customer_location"]},
            "merchant": {"merchant_id": row["merchant_id"], "name": merchant["synthetic_name"],
                         "category": row["merchant_category"], "location": row["merchant_location"]},
            "device": {"device_id": did, "kind": devices[did]["device_kind"]} if did else None,
            "fraud_label": label["fraud_label"], "label_source": label.get("label_source"),
            "label_available_at": label.get("label_available_at"),
        })
    customer_docs = [
        {**customers[cid], "devices": list(customer_devices.get(cid, {}).values())}
        for cid in sorted(customer_ids)
    ]
    return customer_docs, transactions


def load_dataset(db, data_dir: Path, limit: int) -> dict:
    customers, transactions = prepare_documents(data_dir, limit)
    # Enforce stable IDs before upserts, so repeated imports cannot add duplicates.
    db.customers.create_index("customer_id", unique=True, name="customer_id_unique")
    db.transactions.create_index("transaction_id", unique=True, name="transaction_id_unique")
    for collection, docs, key in (
        (db.customers, customers, "customer_id"),
        (db.transactions, transactions, "transaction_id"),
    ):
        for offset in range(0, len(docs), 1000):
            collection.bulk_write([
                UpdateOne({key: doc[key]}, {"$set": doc}, upsert=True)
                for doc in docs[offset:offset + 1000]
            ], ordered=False)
    result = {"sampled_customers": len(customers), "sampled_transactions": len(transactions)}
    print(f"Upserted {len(customers):,} customers and {len(transactions):,} transactions.")
    return result


def demonstrate_crud(db) -> dict:
    """Use independent dictionaries and always remove only this demo's records."""
    coll = db.transactions
    ids = [f"DEMO_CRUD_{i:03}" for i in (1, 2, 3)]
    owned = {"transaction_id": {"$in": ids}, "customer_id": "DEMO_CUSTOMER", "is_demo": True}
    coll.delete_many(owned)  # Also recovers demo records left by an interrupted run.

    def sample(index: int, paise: int) -> dict:
        return {
            "transaction_id": ids[index], "customer_id": "DEMO_CUSTOMER",
            "merchant_id": "DEMO_MERCHANT", "device_id": "DEMO_DEVICE",
            "transaction_time": datetime.now(timezone.utc),
            "transaction_amount": paise / 100, "amount_paise": paise,
            "merchant_category": "demo", "fraud_label": 0, "is_demo": True,
        }

    try:
        inserted = coll.insert_one(sample(0, 12345))
        print("insert_one:", inserted.inserted_id)
        many = coll.insert_many([sample(1, 2000), sample(2, 3000)])
        print("insert_many:", many.inserted_ids)
        found = list(coll.find(
            {**owned, "transaction_amount": {"$gte": 20}},
            {"_id": 0, "transaction_id": 1, "transaction_amount": 1, "merchant_category": 1},
        ).sort("transaction_id", ASCENDING))
        print("find with filter and projection:", json.dumps(found))
        updated = coll.update_many(owned, {"$set": {"demo_reviewed": True}})
        print(f"update_many: matched={updated.matched_count}, modified={updated.modified_count}")
        deleted = coll.delete_one({**owned, "transaction_id": ids[0]})
        print(f"delete_one: deleted={deleted.deleted_count}")
        return {"insert_one": 1, "insert_many": len(many.inserted_ids), "find": found,
                "update_many_matched": updated.matched_count,
                "update_many_modified": updated.modified_count, "delete_one": deleted.deleted_count}
    finally:
        coll.delete_many(owned)
        print("CRUD demo cleanup complete.")


def explain_summary(result: dict) -> dict:
    stats = result["executionStats"]
    return {"winningPlan": result["queryPlanner"]["winningPlan"],
            **{key: stats[key] for key in (
                "nReturned", "totalDocsExamined", "totalKeysExamined", "executionTimeMillis")}}


def show_explain(db) -> dict:
    """Use hints to compare the same query reproducibly without dropping indexes."""
    coll = db.transactions
    sample = coll.find_one({"is_demo": {"$ne": True}, "transaction_time": {"$type": "date"}})
    if not sample:
        raise ValueError("No dated transactions found; run the load action first.")
    query = {"customer_id": sample["customer_id"],
             "transaction_time": {"$gte": sample["transaction_time"] - timedelta(hours=24),
                                  "$lte": sample["transaction_time"]}}
    before = explain_summary(coll.find(query).hint([("$natural", 1)]).explain())
    coll.create_index("customer_id", name="customer_id_idx")
    coll.create_index([("transaction_time", DESCENDING)], name="transaction_time_idx")
    coll.create_index([("customer_id", ASCENDING), ("transaction_time", DESCENDING)],
                      name="customer_time_idx")
    after = explain_summary(coll.find(query).hint("customer_time_idx").explain())
    print("BEFORE: forced collection scan (baseline, even on reruns)")
    print(json.dumps(before, default=str, indent=2))
    print("AFTER: forced customer_time_idx for the same query")
    print(json.dumps(after, default=str, indent=2))
    return {"query": query, "before": before, "after": after}


def fraud_pipeline() -> list[dict]:
    return [
        {"$match": {"is_demo": {"$ne": True}, "fraud_label": {"$in": [0, 1]}}},
        {"$group": {"_id": "$merchant_category", "total_transactions": {"$sum": 1},
                     "fraud_transactions": {"$sum": "$fraud_label"}}},
        {"$set": {"fraud_rate": {"$divide": ["$fraud_transactions", "$total_transactions"]}}},
        {"$sort": {"fraud_rate": -1, "_id": 1}},
    ]


def top_customers_pipeline() -> list[dict]:
    """Each customer's own latest event anchors their inclusive 24-hour window."""
    return [
        {"$match": {"is_demo": {"$ne": True}, "transaction_time": {"$type": "date"}}},
        {"$setWindowFields": {
            "partitionBy": "$customer_id",
            "output": {"last_activity": {"$max": "$transaction_time",
                                         "window": {"documents": ["unbounded", "unbounded"]}}},
        }},
        {"$match": {"$expr": {"$gte": ["$transaction_time", {"$dateSubtract": {
            "startDate": "$last_activity", "unit": "hour", "amount": 24}}]}}},
        {"$group": {"_id": "$customer_id", "transaction_count": {"$sum": 1},
                     "total_amount_paise": {"$sum": "$amount_paise"},
                     "last_activity": {"$max": "$last_activity"}}},
        {"$sort": {"total_amount_paise": -1, "_id": 1}},
        {"$limit": 10},
        {"$set": {"total_amount_inr": {"$divide": ["$total_amount_paise", 100]},
                  "window_start": {"$dateSubtract": {
                      "startDate": "$last_activity", "unit": "hour", "amount": 24}}}},
    ]


def run_aggregations(db) -> dict:
    fraud = list(db.transactions.aggregate(fraud_pipeline()))
    top = list(db.transactions.aggregate(top_customers_pipeline()))
    print("Fraud rate per merchant_category (0 to 1; labelled transactions only):")
    for row in fraud:
        print(json.dumps(row))
    print("Top 10 customers: each customer's last 24 hours of activity (UTC; integer paise totals):")
    for row in top:
        print(json.dumps(row, default=str))
    return {"fraud_by_category": fraud, "top_customers_last_24h": top}


def run_assignment(db, action: str, data_dir: Path, limit: int) -> dict:
    report: dict[str, Any] = {"generated_at_utc": datetime.now(timezone.utc), "action": action}
    if action in ("all", "load"):
        print("\n--- Load SENTINEL source data ---")
        report["load"] = load_dataset(db, data_dir, limit)
        db.transactions.create_index("customer_id", name="customer_id_idx")
        db.transactions.create_index([("transaction_time", DESCENDING)], name="transaction_time_idx")
    if action in ("all", "crud"):
        print("\n--- CRUD demonstrations ---")
        report["crud"] = demonstrate_crud(db)
    if action in ("all", "explain"):
        print("\n--- explain() comparison ---")
        report["explain"] = show_explain(db)
    if action in ("all", "aggregate"):
        print("\n--- Aggregation pipelines ---")
        report["aggregations"] = run_aggregations(db)
    report["counts"] = {name: db[name].count_documents({}) for name in ("customers", "transactions")}
    report["remaining_demo_transactions"] = db.transactions.count_documents({"is_demo": True})
    print("\nCollection counts:", json.dumps(report["counts"]))
    print("Remaining demo transactions:", report["remaining_demo_transactions"])
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--action", choices=("all", "load", "crud", "explain", "aggregate"),
                        default=os.getenv("SENTINEL_ACTION", "all"))
    parser.add_argument("--limit", type=int, default=int(os.getenv("TRANSACTION_LIMIT", "5000")))
    parser.add_argument("--data-dir", type=Path, default=Path(os.getenv("SENTINEL_DATA_DIR", "./data")))
    parser.add_argument("--report", type=Path, help="Save credential-free JSON results to this path")
    args = parser.parse_args()
    if args.action not in ("all", "load", "crud", "explain", "aggregate"):
        parser.error("Invalid SENTINEL_ACTION.")
    data_dir = args.data_dir.expanduser()
    if not data_dir.is_absolute():
        data_dir = ROOT / data_dir
    uri = os.getenv("MONGODB_URI", "")
    if not uri or "<" in uri:
        raise SystemExit("Set MONGODB_URI in .env using your Atlas connection string.")
    try:
        with MongoClient(uri, serverSelectionTimeoutMS=10000, connectTimeoutMS=10000,
                         tz_aware=True, appname="sentinel-building-blocks") as client:
            client.admin.command("ping")
            print("Connected to MongoDB (ping successful).")
            db = client[os.getenv("MONGODB_DATABASE", "sentinel_mongodb")]
            report = run_assignment(db, args.action, data_dir, args.limit)
        if args.report:
            args.report.parent.mkdir(parents=True, exist_ok=True)
            args.report.write_text(json.dumps(report, default=str, indent=2) + "\n", encoding="utf-8")
            print(f"Saved results: {args.report}")
    except ConfigurationError:
        raise SystemExit("MongoDB configuration/DNS failed. Check the Atlas URI, DNS and cluster status.") from None
    except OperationFailure as exc:
        raise SystemExit(f"MongoDB operation failed (code {exc.code}). Check user rights, data and server version.") from None
    except PyMongoError as exc:
        # Driver messages can contain hosts/URI fragments; keep public output credential-free.
        raise SystemExit(f"MongoDB failed ({type(exc).__name__}). Check Atlas IP access and connectivity.") from None
    except (OSError, ValueError, KeyError) as exc:
        raise SystemExit(f"Assignment failed: {exc}") from None


if __name__ == "__main__":
    main()
