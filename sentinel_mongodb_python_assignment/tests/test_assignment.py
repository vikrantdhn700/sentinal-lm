"""Offline regressions. Run with: python -m unittest discover -s tests -v"""
import csv
import gzip
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from main import iso_datetime, parse_value, prepare_documents, read_table


class SourceParsingTests(unittest.TestCase):
    def test_schema_types_keep_ids_but_convert_labels_and_money(self):
        self.assertEqual(parse_value("000123", "transaction_id"), "000123")
        self.assertIs(type(parse_value("1", "fraud_label")), int)
        self.assertEqual(parse_value("12345", "amount_paise"), 12345)
        self.assertEqual(parse_value("123.45", "transaction_amount"), 123.45)
        self.assertEqual(parse_value('{"city":"Jaipur"}', "customer_location"), {"city": "Jaipur"})
        self.assertIs(parse_value("true", "is_synthetic"), True)
        self.assertIsNone(parse_value("", "device_id"))

    def test_timestamps_normalize_to_utc_and_reject_invalid_dates(self):
        expected = datetime(2026, 1, 1, tzinfo=timezone.utc)
        self.assertEqual(iso_datetime("2026-01-01T05:30:00+05:30"), expected)
        self.assertEqual(parse_value("2026-01-01T00:00:00Z", "transaction_time"), expected)
        with self.assertRaises(ValueError):
            parse_value("bad-date", "transaction_time")

    def test_gzip_csv_reader_uses_schema_and_reports_missing_tables(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            shard = root / "tables" / "labels" / "train" / "part.csv.gz"
            shard.parent.mkdir(parents=True)
            with gzip.open(shard, "wt", encoding="utf-8", newline="") as handle:
                writer = csv.writer(handle)
                writer.writerow(["transaction_id", "fraud_label"])
                writer.writerow(["TX_01", "1"])
            self.assertEqual(list(read_table("labels", root)), [{"transaction_id": "TX_01", "fraud_label": 1}])
            with self.assertRaises(FileNotFoundError):
                list(read_table("missing", root))


class DocumentPreparationTests(unittest.TestCase):
    def table(self, name, _data_dir):
        if name == "transactions":
            return iter({
                "transaction_id": f"TX_{i}", "customer_id": "C_1", "merchant_id": "M_1",
                "device_id": "D_1", "customer_age": 35, "customer_location": {"city": "Jaipur"},
                "merchant_location": {"city": "Jaipur"}, "merchant_category": "grocery",
                "transaction_time": datetime(2026, 1, 1, tzinfo=timezone.utc),
                "amount_paise": 100, "transaction_amount": 1.0,
            } for i in range(5000))
        return iter({
            "customers": [{"customer_id": "C_1", "customer_age": 35}],
            "merchants": [{"merchant_id": "M_1", "synthetic_name": "Test merchant"}],
            "devices": [{"device_id": "D_1", "device_kind": "browser"}],
            "labels": [{"transaction_id": f"TX_{i}", "fraud_label": i % 2} for i in range(5000)],
        }[name])

    def test_customer_device_deduplication_and_transaction_joins(self):
        with patch("main.read_table", side_effect=self.table):
            customers, transactions = prepare_documents(Path("unused"), 5000)
        self.assertEqual(len(transactions), 5000)
        self.assertEqual(customers[0]["devices"], [{"device_id": "D_1", "device_kind": "browser"}])
        self.assertEqual(transactions[1]["fraud_label"], 1)
        self.assertEqual(transactions[0]["merchant"]["name"], "Test merchant")
        self.assertEqual(transactions[0]["customer"]["customer_id"], customers[0]["customer_id"])

    def test_missing_labels_fail_before_any_database_write(self):
        def missing_labels(name, data_dir):
            return iter([]) if name == "labels" else self.table(name, data_dir)
        with patch("main.read_table", side_effect=missing_labels), self.assertRaisesRegex(ValueError, "fraud label"):
            prepare_documents(Path("unused"), 5000)

    def test_minimum_sample_and_short_source_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "at least 5000"):
            prepare_documents(Path("unused"), 100)
        with patch("main.read_table", return_value=iter([])), self.assertRaisesRegex(ValueError, "found only 0"):
            prepare_documents(Path("unused"), 5000)


if __name__ == "__main__":
    unittest.main()
