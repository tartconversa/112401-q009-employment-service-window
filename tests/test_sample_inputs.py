"""检查演练资料的读取契约和关联锚点。"""
import csv
from pathlib import Path
import unittest

INPUTS = Path(__file__).resolve().parent.parent / "samples"

HEADERS = {'policy': ('service', 'limit_days', 'segments', 'qualification', 'can_suspend', 'hold_days'), 'calendar': ('date', 'service_day'), 'intakes': ('case', 'service', 'station', 'alternates', 'accepted', 'prior', 'restart'), 'events': ('event', 'case', 'kind', 'at', 'site', 'receipt'), 'slots': ('slot', 'site', 'day', 'start', 'end', 'qualification', 'shared_staff', 'status'), 'appointments': ('appointment', 'case', 'slot', 'start', 'end', 'notified'), 'scenarios': ('scenario', 'service', 'day', 'resuming_low', 'resuming_high', 'arrivals_low', 'arrivals_high')}
REFERENCES = [('events', 'case', 'intakes', 'case'), ('appointments', 'slot', 'slots', 'slot'), ('intakes', 'service', 'policy', 'service'), ('scenarios', 'day', 'calendar', 'date')]
EXAMPLES = [['events', 'receipt', 'EV', 2], ['events', 'receipt', 'EG', 2], ['slots', 'shared_staff', 'P3', 5]]


class SampleInputContract(unittest.TestCase):
    def read(self, table):
        with (INPUTS / f"{table}.csv").open(encoding="utf-8", newline="") as source:
            reader = csv.DictReader(source, strict=True)
            self.assertEqual(tuple(reader.fieldnames), HEADERS[table])
            rows = list(reader)
        self.assertTrue(rows, table)
        for row in rows:
            self.assertNotIn(None, row, table)
            self.assertNotIn(None, row.values(), table)
        return rows

    def test_csv_readable_with_documented_columns(self):
        for table in HEADERS:
            with self.subTest(table=table):
                self.read(table)

    def test_cross_source_example_references(self):
        for source, source_field, target, target_field in REFERENCES:
            with self.subTest(source=source, field=source_field, target=target):
                available = {row[target_field] for row in self.read(target)}
                actual = {value for row in self.read(source)
                          for value in row[source_field].split("|") if value}
                self.assertTrue(actual)
                self.assertEqual(actual - available, set())

    def test_example_revisions_and_overlaps_are_present(self):
        for table, field, example, expected in EXAMPLES:
            with self.subTest(table=table, field=field, example=example):
                found = sum((row[field] == example if "|" in example else example in row[field].split("|")) for row in self.read(table))
                self.assertEqual(found, expected)


if __name__ == "__main__":
    unittest.main()
