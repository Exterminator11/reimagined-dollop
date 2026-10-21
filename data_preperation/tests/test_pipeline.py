"""Synthetic integration tests: row preservation, geography and temporal leakage."""
import sys
import unittest
from pathlib import Path
from unittest.mock import patch
import tempfile
import json

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import geopandas as gpd
import pandas as pd
from shapely.geometry import Polygon
from pipeline import merge_sources, normalize_bbl, download_table, fetch_json
from publish_kaggle import publish, ARTIFACTS


class MergeTests(unittest.TestCase):
    def test_long_kaggle_title_fails_before_staging_or_upload(self):
        with patch("publish_kaggle.kaggle_command") as command:
            with self.assertRaisesRegex(ValueError, "6–50"):
                publish({"kaggle_dataset": "owner/housing", "kaggle_title": "x" * 51}, Path("unused"), "create")
            command.assert_not_called()

    def test_missing_census_key_reports_setup_without_leaking_credentials(self):
        from unittest.mock import Mock
        source = Mock()
        source.get.return_value.status_code = 200
        source.get.return_value.text = "<title>Missing Key</title>"
        with self.assertRaisesRegex(RuntimeError, "CENSUS_API_KEY"):
            fetch_json(source, "https://api.census.gov/data/2022/acs/acs5", {"key": "secret"})

    def fixture(self):
        rows = []
        for key, created, closed, bbl, lon in [
            ("prior", "2023-12-10", "2023-12-12", "1000010001", -74.0),
            ("a", "2024-01-01", "2024-01-05", "1000010001", -74.0),
            ("repeat", "2024-01-20", "2024-01-22", "1000010001", -74.0),
            ("unmatched", "2024-01-01", "2024-01-05", None, None),
            ("late", "2024-03-28", "2024-03-30", "1000010001", -74.0),
            ("open", "2024-01-01", None, "1000010001", -74.0),
            ("bad", "2024-01-03", "2024-01-01", "1000010001", -74.0),
        ]:
            rows.append(dict(unique_key=key, created_date=created, closed_date=closed,
                             complaint_type="Plumbing", descriptor="test", bbl=bbl,
                             longitude=lon, latitude=40.7))
        requests = pd.DataFrame(rows)
        pluto = pd.DataFrame([dict(bbl="1000010001.0", yearbuilt="1920", unitsres="10", unitstotal="11", bldgclass="C1", landuse="02", numbldgs="1", numfloors="5", version="test")])
        acs = pd.DataFrame([dict(tract_geoid="36061000100", median_household_income=50000, acs_year=2022)])
        tracts = gpd.GeoDataFrame({"geoid": ["36061000100"]}, geometry=[Polygon([(-74.1,40.6),(-73.9,40.6),(-73.9,40.8),(-74.1,40.8)])], crs=4326)
        violations = pd.DataFrame({"violationid": ["v1", "future", "unapproved"], "bbl": ["1000010001"]*3, "class": ["C"]*3,
                                   "issued_at": pd.to_datetime(["2024-01-02", "2024-01-10", "2024-01-02"]),
                                   "approved_at": pd.to_datetime(["2024-01-02", "2024-01-10", "2024-01-10"])})
        config = dict(start_date="2024-01-01", end_date="2024-04-01", history_days=365, acs_available_date="2023-12-07", pluto_available_date=None)
        return requests, pluto, acs, tracts, violations, config, pd.Timestamp("2024-04-01")

    def test_join_preserves_rows_and_excludes_future_information(self):
        result = merge_sources(*self.fixture()).set_index("request_id")
        self.assertEqual(len(result), 6)
        self.assertTrue(result.index.is_unique)
        self.assertEqual(result.loc["a", "tract_geoid"], "36061000100")
        self.assertEqual(result.loc["a", "residential_units"], 10)
        self.assertEqual(result.loc["a", "prior_violations_365d"], 1)
        self.assertEqual(result.loc["a", "prior_property_complaints_365d"], 2)  # prior + open
        self.assertEqual(result.loc["a", "recurrence_30d"], 1)
        self.assertTrue(pd.isna(result.loc["late", "recurrence_30d"]))
        self.assertTrue(pd.isna(result.loc["unmatched", "recurrence_30d"]))
        self.assertTrue(pd.isna(result.loc["open", "prior_violations_365d"]))
        self.assertFalse(result.loc["bad", "has_valid_timestamps"])
        self.assertFalse(result.loc["a", "pluto_available_at_closure"])

    def test_duplicate_property_keys_fail_instead_of_multiplying_rows(self):
        args = list(self.fixture())
        args[1] = pd.concat([args[1], args[1]], ignore_index=True)
        with self.assertRaises(pd.errors.MergeError):
            merge_sources(*args)

    def test_ambiguous_boundary_is_not_assigned_arbitrarily(self):
        args = list(self.fixture())
        args[3] = pd.concat([args[3], args[3]], ignore_index=True)
        result = merge_sources(*args).set_index("request_id")
        self.assertTrue(result.loc["a", "tract_match_ambiguous"])
        self.assertTrue(pd.isna(result.loc["a", "tract_geoid"]))

    def test_bbl_normalization_preserves_nulls(self):
        values = normalize_bbl(pd.Series(["1000010001.0", "0", None, "6000010001"]))
        self.assertEqual(values.iloc[0], "1000010001")
        self.assertTrue(values.iloc[1:].isna().all())

    def test_cached_pages_resume_with_keyset_not_offset(self):
        with tempfile.TemporaryDirectory() as directory:
            pages = [[{"id": "1"}, {"id": "2"}], [{"id": "3"}], []]
            with patch("pipeline.fetch_json", side_effect=pages) as fetch:
                result = download_table(None, "test", "id", "id IS NOT NULL", "id", Path(directory), 2)
                self.assertEqual(len(result), 3)
                self.assertIn("id > '2'", fetch.call_args_list[1].args[2]["$where"])
            with patch("pipeline.fetch_json", side_effect=AssertionError("cache should be reused")):
                self.assertEqual(len(download_table(None, "test", "id", "id IS NOT NULL", "id", Path(directory), 2)), 3)

    def test_upload_is_private_and_only_stages_expected_artifacts(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            for name in ARTIFACTS:
                (path / name).write_text("fixture")
            (path / "quality_report.json").write_text(json.dumps({"rows": 1, "unique_requests": 1}))
            (path / "secret.txt").write_text("must not upload")
            def inspect(*args):
                self.assertNotIn("--public", args)
                stage = Path(args[args.index("-p") + 1])
                self.assertEqual({p.name for p in stage.iterdir()}, set(ARTIFACTS) | {"dataset-metadata.json"})
            with patch("publish_kaggle.kaggle_command", side_effect=inspect):
                publish({"kaggle_dataset": "owner/housing", "kaggle_title": "Housing"}, path, "create")


if __name__ == "__main__":
    unittest.main()
