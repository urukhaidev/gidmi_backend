"""Regression checks for complete snapshots and refresh finalization (no network)."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import import_tripster_experiences as importer


CITY = {"id": 149, "slug": "Kaliningrad", "experience_count": 1}
TAG = {"id": 7, "citytag_id": 70, "name": "Море", "tag": {"is_common": False}}
EXPERIENCE = {"id": 1, "title": "Море", "status": "active", "type": "private", "tags": [{"id": 7}]}


class RefreshTests(unittest.TestCase):
    def import_city(self, db: Mock, *, dry_run: bool = False):
        return importer.import_city(db, CITY, "test-token", set(), set(), {149}, 100, 45, dry_run, False)

    @patch.object(importer, "fetch_citytags_page", return_value={"count": 1, "results": [TAG], "next": None})
    @patch.object(importer, "fetch_tripster_page", return_value={"count": 1, "results": [EXPERIENCE], "next": None})
    def test_import_uses_canonical_citytag_and_transactional_replacement(self, *_):
        db = Mock()
        db.select_all.return_value = [{"citytag_id": 70}, {"citytag_id": 99}]
        result = self.import_city(db)
        self.assertEqual(result[-1], {1})
        tables = {call.args[0] for call in db.upsert.call_args_list}
        self.assertTrue({"tripster_tags", "tripster_city_tags", "experiences"} <= tables)
        db.patch_ids.assert_called_once_with(
            "tripster_city_tags", "citytag_id", [99],
            {"is_hidden": True, "experience_count": 0, "excursions_count": 0, "tours_count": 0},
        )
        db.rpc.assert_any_call("replace_tripster_relations_v2", {
            "p_experience_ids": [1], "p_tag_links": [{"experience_id": 1, "citytag_id": 70}], "p_photos": [],
        })
        self.assertTrue(any(call.args[0] == "upsert_tripster_tag_bridge_v2" for call in db.rpc.call_args_list))
        db.delete_by_experience_ids.assert_not_called()

    @patch.object(importer, "fetch_citytags_page", return_value={"count": 0, "results": [], "next": None})
    @patch.object(importer, "fetch_tripster_page", return_value={"count": 0, "results": [], "next": None})
    def test_empty_city_is_a_successful_snapshot(self, *_):
        db = Mock()
        db.select_all.return_value = [{"citytag_id": 99}]
        self.assertEqual(self.import_city(db)[-1], set())
        self.assertEqual(db.patch_ids.call_args.args[2], [99])

    @patch.object(importer, "fetch_citytags_page", return_value={"count": 2, "results": [TAG, TAG], "next": None})
    @patch.object(importer, "fetch_tripster_page", return_value={"count": 1, "results": [EXPERIENCE], "next": None})
    def test_citytags_count_includes_duplicates_in_tripster_response(self, *_):
        db = Mock()
        db.select_all.return_value = []
        self.assertEqual(self.import_city(db)[-1], {1})
        written = [call.args[1] for call in db.upsert.call_args_list if call.args[0] == "tripster_city_tags"]
        self.assertEqual(len(written[0]), 1)

    @patch.object(importer, "fetch_citytags_page", return_value={"count": 1, "results": [TAG], "next": None})
    @patch.object(importer, "fetch_tripster_page", side_effect=[
        {"count": 2, "results": [EXPERIENCE], "next": "?offset=1"}, RuntimeError("network failure"),
    ])
    def test_page_failure_does_not_write_partial_city(self, *_):
        db = Mock()
        with self.assertRaisesRegex(RuntimeError, "network failure"):
            self.import_city(db)
        self.assertEqual(db.mock_calls, [])

    @patch.object(importer, "fetch_citytags_page", return_value={"count": 0, "results": [], "next": None})
    @patch.object(importer, "fetch_tripster_page", side_effect=[
        {"count": 2, "results": [EXPERIENCE], "next": None},
        {"count": 2, "results": [{**EXPERIENCE, "id": 2}], "next": None},
    ])
    def test_truncated_snapshot_does_not_write(self, *_):
        db = Mock()
        with self.assertRaisesRegex(RuntimeError, "incomplete/unstable"):
            self.import_city(db)
        self.assertEqual(db.mock_calls, [])

    @patch.object(importer, "fetch_citytags_page", return_value={"count": 1, "results": [TAG], "next": None})
    @patch.object(importer, "fetch_tripster_page", return_value={"count": 2, "results": [EXPERIENCE], "next": None})
    def test_inaccurate_count_requires_two_identical_full_snapshots(self, fetch, *_):
        db = Mock()
        db.select_all.return_value = []
        self.assertEqual(self.import_city(db)[-1], {1})
        self.assertEqual(fetch.call_count, 2)

    @patch.object(importer, "fetch_citytags_page", return_value={"count": 0, "results": [], "next": None})
    @patch.object(importer, "fetch_tripster_page", return_value={"count": 2, "results": [], "next": None})
    def test_nonzero_count_with_empty_response_never_triggers_writes(self, *_):
        db = Mock()
        with self.assertRaisesRegex(RuntimeError, "nonzero API count"):
            self.import_city(db)
        self.assertEqual(db.mock_calls, [])

    @patch.object(importer, "fetch_citytags_page", return_value={"count": 0, "results": [], "next": None})
    @patch.object(importer, "fetch_tripster_page", return_value={"results": [], "next": None})
    def test_response_without_count_cannot_be_treated_as_an_empty_catalog(self, *_):
        db = Mock()
        with self.assertRaisesRegex(RuntimeError, "no valid count"):
            self.import_city(db)
        self.assertEqual(db.mock_calls, [])

    @patch.object(importer, "fetch_citytags_page", return_value={"count": 1, "results": [TAG], "next": None})
    @patch.object(importer, "fetch_tripster_page", return_value={
        "count": 1, "results": [{"id": 1, "title": "Invalid tag payload"}], "next": None,
    })
    def test_missing_tags_do_not_erase_existing_links(self, *_):
        db = Mock()
        with self.assertRaisesRegex(RuntimeError, "invalid/missing tags"):
            self.import_city(db)
        self.assertEqual(db.mock_calls, [])

    @patch.object(importer, "fetch_citytags_page", return_value={"count": 1, "results": [TAG], "next": None})
    @patch.object(importer, "fetch_tripster_page", return_value={"count": 1, "results": [EXPERIENCE], "next": None})
    def test_dry_run_has_no_database_writes(self, *_):
        db = Mock()
        self.import_city(db, dry_run=True)
        self.assertEqual(db.mock_calls, [])

    def test_list_mapping_preserves_saved_detailed_fields_and_sets_sync_time(self):
        row = importer.map_experience(EXPERIENCE, 149, set(), set(), {149})
        self.assertNotIn("description_html", row)
        self.assertNotIn("schedule", row)
        self.assertIn("last_synced_at", row)
        self.assertEqual(row["status"], "active")

    def run_main(self, *, failure: bool = False, incomplete: bool = False, dry_run: bool = False,
                 checkpoint: str | None = None, resume: bool = False, cleanup_counts: list[int] | None = None):
        db = Mock()
        db.select_all.side_effect = lambda table, *_args, **_kwargs: [CITY] if table == "cities" else []
        db.rpc.return_value = 0
        if cleanup_counts is not None:
            cleanup_results = iter(cleanup_counts)
            db.rpc.side_effect = lambda name, params: (
                next(cleanup_results) if name == "finalize_tripster_catalog_refresh_v2" and params["p_city_ids"] else 0
            )
        db.select_all_keyset.return_value = []
        args = argparse.Namespace(
            workers=1, limit=100, timeout=45, max_cities=None, city_slug=None,
            photos_only=False, dry_run=dry_run, incomplete_only=incomplete,
            resume=resume, checkpoint=checkpoint,
        )
        with patch.object(importer, "parse_args", return_value=args), \
                patch.object(importer, "load_dotenv"), \
                patch.dict(os.environ, {"SUPABASE_URL": "https://test.invalid", "SUPABASE_SERVICE_ROLE_KEY": "test", "TRIPSTER_TOKEN": "test"}), \
                patch.object(importer, "SupabaseRest", return_value=db), \
                patch.object(importer, "import_city", side_effect=RuntimeError("failed") if failure else None,
                             return_value=(1, 1, 1, 0, {1})):
            if failure:
                with self.assertRaises(SystemExit) as error:
                    importer.main()
                self.assertEqual(error.exception.code, 1)
            else:
                importer.main()
        return db

    def test_failed_run_never_finalizes_a_nonempty_scope(self):
        db = self.run_main(failure=True)
        calls = [call for call in db.rpc.call_args_list if call.args[0] == "finalize_tripster_catalog_refresh_v2"]
        self.assertTrue(all(call.args[1]["p_city_ids"] == [] for call in calls))

    def test_full_run_finalizes_after_rebuilding_new_links(self):
        db = self.run_main()
        calls = db.rpc.call_args_list
        self.assertEqual(calls[-1].args[0], "finalize_tripster_catalog_refresh_v2")
        self.assertEqual(calls[-1].args[1]["p_city_ids"], [149])
        self.assertEqual(calls[-1].args[1]["p_seen_ids"], [1])
        self.assertEqual(calls[-2].args, ("sync_experience_catalog_links_v2", {"p_experience_ids": [1]}))
        city_filter = db.select_all.call_args_list[0].kwargs["filters"]
        self.assertNotIn("experience_count", city_filter)

    def test_incomplete_import_never_deactivates_missing(self):
        db = self.run_main(incomplete=True)
        calls = [call for call in db.rpc.call_args_list if call.args[0] == "finalize_tripster_catalog_refresh_v2"]
        self.assertTrue(all(not call.args[1]["p_city_ids"] for call in calls))

    def test_main_dry_run_does_not_call_any_rpc(self):
        self.run_main(dry_run=True).rpc.assert_not_called()

    def test_resume_uses_saved_presence_and_original_start_time(self):
        with tempfile.TemporaryDirectory() as directory:
            checkpoint = Path(directory) / "checkpoint.json"
            checkpoint.write_text(json.dumps({
                "city_ids": [149], "started_at": "2026-10-18T10:00:00+00:00",
                "completed": {"149": {"pages": 1, "imported": 1, "tags": 0, "photos": 0, "seen_ids": [42]}},
            }), encoding="utf-8")
            db = self.run_main(checkpoint=str(checkpoint), resume=True)
            self.assertEqual(db.rpc.call_args_list[-1].args[1], {
                "p_city_ids": [149], "p_seen_ids": [42], "p_started_at": "2026-10-18T10:00:00+00:00",
            })
            self.assertIn("finished_at", json.loads(checkpoint.read_text(encoding="utf-8")))

    def test_failed_city_is_not_saved_as_completed(self):
        with tempfile.TemporaryDirectory() as directory:
            checkpoint = Path(directory) / "checkpoint.json"
            self.run_main(checkpoint=str(checkpoint), failure=True)
            saved = json.loads(checkpoint.read_text(encoding="utf-8"))
            self.assertEqual(saved["completed"], {})
            self.assertNotIn("finished_at", saved)

    def test_resume_skips_finished_catalog_and_link_batches(self):
        with tempfile.TemporaryDirectory() as directory:
            checkpoint = Path(directory) / "checkpoint.json"
            checkpoint.write_text(json.dumps({
                "city_ids": [149], "started_at": "2026-10-18T10:00:00+00:00",
                "catalog_synced": True, "catalog_link_offset": 1,
                "completed": {"149": {"pages": 1, "imported": 2, "tags": 0, "photos": 0, "seen_ids": [1, 2]}},
            }), encoding="utf-8")
            db = self.run_main(checkpoint=str(checkpoint), resume=True)
            self.assertFalse(any(call.args[0] == "sync_tripster_local_catalog_v2" for call in db.rpc.call_args_list))
            link_calls = [call.args[1]["p_experience_ids"] for call in db.rpc.call_args_list
                          if call.args[0] == "sync_experience_catalog_links_v2" and call.args[1]["p_experience_ids"]]
            self.assertEqual(link_calls, [[2]])
            self.assertEqual(json.loads(checkpoint.read_text(encoding="utf-8"))["catalog_link_offset"], 2)

    def test_cleanup_repeats_until_empty_and_saves_cumulative_count(self):
        with tempfile.TemporaryDirectory() as directory:
            checkpoint = Path(directory) / "checkpoint.json"
            db = self.run_main(checkpoint=str(checkpoint), cleanup_counts=[100, 35, 0])
            actual_calls = [call for call in db.rpc.call_args_list
                            if call.args[0] == "finalize_tripster_catalog_refresh_v2" and call.args[1]["p_city_ids"]]
            self.assertEqual(len(actual_calls), 3)
            saved = json.loads(checkpoint.read_text(encoding="utf-8"))
            self.assertEqual(saved["inactive_count"], 135)
            self.assertIn("finished_at", saved)


if __name__ == "__main__":
    unittest.main()
