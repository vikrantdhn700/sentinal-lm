"""Opt-in read-only checks against MongoDB: set RUN_ATLAS_TESTS=1 first."""
import os
import unittest
from collections import defaultdict
from datetime import datetime, timedelta, timezone

from pymongo import MongoClient

from main import fraud_pipeline, top_customers_pipeline


@unittest.skipUnless(os.getenv("RUN_ATLAS_TESTS") == "1", "Set RUN_ATLAS_TESTS=1 to query Atlas")
class AtlasAggregationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = MongoClient(os.environ["MONGODB_URI"], serverSelectionTimeoutMS=10000, tz_aware=True)
        cls.db = cls.client[os.getenv("MONGODB_DATABASE", "sentinel_mongodb")]
        cls.rows = list(cls.db.transactions.find({"is_demo": {"$ne": True}}, {"_id": 0}))

    @classmethod
    def tearDownClass(cls):
        cls.client.close()

    def test_loaded_documents_have_required_bson_types_and_references(self):
        self.assertGreaterEqual(len(self.rows), 5000)
        customers = {r["customer_id"]: r for r in self.db.customers.find({})}
        self.assertEqual(len({r["transaction_id"] for r in self.rows}), len(self.rows))
        self.assertEqual(self.db.transactions.count_documents({"is_demo": True}), 0)
        for row in self.rows:
            self.assertIs(type(row["fraud_label"]), int)
            self.assertIn(row["fraud_label"], (0, 1))
            self.assertIsInstance(row["transaction_time"], datetime)
            self.assertIsInstance(row["amount_paise"], int)
            self.assertIn(row["customer_id"], customers)
            self.assertEqual(row["merchant"]["merchant_id"], row["merchant_id"])
            if row.get("device_id"):
                self.assertIn(row["device_id"], {d["device_id"] for d in customers[row["customer_id"]]["devices"]})

    def test_fraud_pipeline_matches_independent_python_calculation(self):
        expected = defaultdict(lambda: [0, 0])
        for row in self.rows:
            expected[row["merchant_category"]][0] += 1
            expected[row["merchant_category"]][1] += row["fraud_label"]
        actual = list(self.db.transactions.aggregate(fraud_pipeline()))
        self.assertEqual(len(actual), len(expected))
        self.assertEqual(sum(r["total_transactions"] for r in actual), len(self.rows))
        for row in actual:
            total, fraud = expected[row["_id"]]
            self.assertEqual(row["total_transactions"], total)
            self.assertEqual(row["fraud_transactions"], fraud)
            self.assertAlmostEqual(row["fraud_rate"], fraud / total)

    def test_customer_pipeline_matches_independent_python_windows(self):
        groups = defaultdict(list)
        for row in self.rows:
            groups[row["customer_id"]].append(row)
        expected = []
        for cid, rows in groups.items():
            latest = max(r["transaction_time"] for r in rows)
            window = [r for r in rows if r["transaction_time"] >= latest - timedelta(hours=24)]
            expected.append((cid, sum(r["amount_paise"] for r in window), len(window), latest))
        expected.sort(key=lambda row: (-row[1], row[0]))
        actual = list(self.db.transactions.aggregate(top_customers_pipeline()))
        self.assertEqual([(r["_id"], r["total_amount_paise"], r["transaction_count"], r["last_activity"])
                          for r in actual], expected[:10])

    def test_exact_24_hour_boundary_and_different_customer_activity_dates(self):
        end = datetime(2026, 2, 1, tzinfo=timezone.utc)
        fixtures = [
            {"customer_id": "A", "transaction_time": end, "amount_paise": 100},
            {"customer_id": "A", "transaction_time": end - timedelta(hours=24), "amount_paise": 200},
            {"customer_id": "A", "transaction_time": end - timedelta(hours=24, milliseconds=1), "amount_paise": 99999},
            {"customer_id": "B", "transaction_time": end - timedelta(days=10), "amount_paise": 400},
            {"customer_id": "C", "transaction_time": end, "amount_paise": 99999, "is_demo": True},
        ]
        # $documents feeds fixtures to the real server without inserting any records.
        actual = list(self.db.aggregate([{"$documents": fixtures}, *top_customers_pipeline()]))
        self.assertEqual([(r["_id"], r["total_amount_paise"], r["transaction_count"])
                          for r in actual], [("B", 400, 1), ("A", 300, 2)])


if __name__ == "__main__":
    unittest.main()
