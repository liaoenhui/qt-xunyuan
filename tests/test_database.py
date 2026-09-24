from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from qt_tool.db import Database


class DatabaseTests(unittest.TestCase):
    def test_source_soft_delete_restore_and_job_guard(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Database(Path(tmp) / 'test.sqlite3')
            source = {'platform': 'test', 'video_id': 'delete', 'url': 'https://example.test/delete'}
            sid, _ = db.add_source(source)
            db.set_source_deleted(sid, True)
            self.assertEqual(db.count_sources('candidate'), 0)
            self.assertEqual(db.count_sources('deleted'), 1)
            self.assertEqual(db.list_sources(view='deleted')[0]['id'], sid)
            self.assertEqual(db.add_source(source), (sid, False))
            with self.assertRaisesRegex(ValueError, '已删除'):
                db.create_job('proxy', sid)
            db.set_source_deleted(sid, False)
            self.assertEqual(db.count_sources('candidate'), 1)
            db.create_job('proxy', sid)
            with self.assertRaisesRegex(ValueError, '任务'):
                db.set_source_deleted(sid, True)
            self.assertIsNone(db.get_source(sid)['deleted_at'])

    def test_accept_and_quota_state_need_only_bucket_and_duration(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Database(Path(tmp) / 'test.sqlite3')
            sid, _ = db.add_source({'platform': 'test', 'video_id': 'view', 'url': 'https://example.test/view'})
            cid, _ = db.add_candidate({'source_id': sid, 'start_time': 0, 'end_time': 6, 'duration': 6,
                                      'duration_bucket': 'short'})
            db.review(cid, {'decision': 'ACCEPT', 'final_bucket': 'T1', 'final_unit': 'T1.1'})
            self.assertEqual(db.get_candidate(cid)['status'], 'ACCEPTED')
            self.assertEqual(db.quota_state(), [{'bucket': 'T1', 'duration_bucket': 'short', 'count': 1}])

    def test_waiting_candidate_can_be_trimmed_and_facts_are_persisted(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Database(Path(tmp) / "test.sqlite3")
            source_id, _ = db.add_source(
                {"platform": "youtube", "video_id": "trim", "url": "https://example.test/v", "title": "A"}
            )
            candidate_id, _ = db.add_candidate({"source_id": source_id, "start_time": 10.0, "end_time": 25.0,
                                                 "duration": 15.0, "facts": {"duration": 15.0}})
            db.trim_candidate(candidate_id, 10.5, 22.9, "short", {"duration": 12.4, "boundary_reviewed": True})
            updated = db.get_candidate(candidate_id)
            self.assertEqual(updated["start_time"], 10.5)
            self.assertEqual(updated["end_time"], 22.9)
            self.assertAlmostEqual(updated["duration"], 12.4)
            self.assertIn('"boundary_reviewed": true', updated["facts_json"])

    def test_source_dedup_and_persistent_candidate(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Database(Path(tmp) / "test.sqlite3")
            source = {"platform": "youtube", "video_id": "abc", "url": "https://example.test/v", "title": "A"}
            first, created = db.add_source(source)
            second, created_again = db.add_source(source)
            self.assertEqual(first, second)
            self.assertTrue(created)
            self.assertFalse(created_again)
            cid, was_created = db.add_candidate({"source_id": first, "start_time": 1.0, "end_time": 8.0,
                                                  "duration": 7.0, "facts": {"duration": 7.0}})
            self.assertTrue(was_created)
            self.assertEqual(db.get_candidate(cid)["duration"], 7.0)

    def test_background_job_lifecycle_and_deduplication(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Database(Path(tmp) / "test.sqlite3")
            source_id, _ = db.add_source(
                {"platform": "manual", "video_id": "job-source", "url": "https://example.test/v", "title": "A"}
            )

            job_id, created = db.create_job("proxy", source_id)
            duplicate_id, duplicate_created = db.create_job("proxy", source_id)
            self.assertTrue(created)
            self.assertFalse(duplicate_created)
            self.assertEqual(job_id, duplicate_id)
            self.assertEqual(db.list_sources()[0]["active_job_id"], job_id)

            db.start_job(job_id)
            self.assertEqual(db.get_job(job_id)["status"], "RUNNING")
            self.assertEqual(db.get_job(job_id)["attempts"], 1)

            db.finish_job(job_id, "DONE", {"message": "完成"})
            finished = db.get_job(job_id)
            self.assertEqual(finished["status"], "DONE")
            self.assertIn("完成", finished["result_json"])
            self.assertIsNone(db.list_sources()[0]["active_job_id"])

    def test_source_jobs_show_running_state_and_queue_position(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Database(Path(tmp) / "test.sqlite3")
            first_source, _ = db.add_source(
                {"platform": "manual", "video_id": "queue-1", "url": "https://example.test/1", "title": "A"}
            )
            second_source, _ = db.add_source(
                {"platform": "manual", "video_id": "queue-2", "url": "https://example.test/2", "title": "B"}
            )
            first_job, _ = db.create_job("proxy", first_source)
            second_job, _ = db.create_job("proxy", second_source)
            db.start_job(first_job)

            sources = {row["id"]: row for row in db.list_sources()}
            self.assertEqual(sources[first_source]["active_job_status"], "RUNNING")
            self.assertEqual(sources[second_source]["active_job_status"], "QUEUED")
            self.assertEqual(sources[second_source]["queue_position"], 1)
            self.assertEqual(sources[second_source]["queue_ahead"], 1)
            self.assertEqual(db.get_job(second_job)["queue_ahead"], 1)

    def test_candidate_part_number_follows_source_timeline(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Database(Path(tmp) / "test.sqlite3")
            source_id, _ = db.add_source(
                {"platform": "youtube", "video_id": "parts", "url": "https://example.test/v", "title": "A"}
            )
            later, _ = db.add_candidate({"source_id": source_id, "start_time": 20.0, "end_time": 30.0,
                                         "duration": 10.0, "facts": {}})
            earlier, _ = db.add_candidate({"source_id": source_id, "start_time": 5.0, "end_time": 15.0,
                                           "duration": 10.0, "facts": {}})
            self.assertEqual(db.candidate_part_number(earlier), 1)
            self.assertEqual(db.candidate_part_number(later), 2)

    def test_reanalysis_removes_only_machine_owned_candidates(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Database(Path(tmp) / "test.sqlite3")
            source_id, _ = db.add_source(
                {"platform": "youtube", "video_id": "reanalyze", "url": "https://example.test/v", "title": "A"}
            )
            machine_id, _ = db.add_candidate({"source_id": source_id, "start_time": 0.0, "end_time": 10.0,
                                               "duration": 10.0, "facts": {}})
            restored_id, _ = db.add_candidate({"source_id": source_id, "start_time": 20.0, "end_time": 30.0,
                                                "duration": 10.0, "facts": {}})
            db.review(restored_id, {"decision": "RESTORE", "notes": "从拒绝列表恢复"})
            reviewed_id, _ = db.add_candidate({"source_id": source_id, "start_time": 10.0, "end_time": 20.0,
                                                "duration": 10.0, "facts": {}})
            db.review(reviewed_id, {"decision": "REJECT", "notes": "人工拒绝"})

            self.assertEqual(db.clear_replaceable_candidates(source_id), 2)
            self.assertIsNone(db.get_candidate(machine_id))
            self.assertIsNone(db.get_candidate(restored_id))
            self.assertIsNotNone(db.get_candidate(reviewed_id))

    def test_source_moves_between_candidate_and_analyzed_lists(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Database(Path(tmp) / "test.sqlite3")
            source_id, _ = db.add_source(
                {"platform": "youtube", "video_id": "source-state", "url": "https://example.test/v", "title": "A"}
            )
            self.assertEqual([row["id"] for row in db.list_sources(view="candidate")], [source_id])
            self.assertEqual(db.list_sources(view="analyzed"), [])

            db.update_source(source_id, analysis_completed=1)
            self.assertEqual(db.list_sources(view="candidate"), [])
            self.assertEqual([row["id"] for row in db.list_sources(view="analyzed")], [source_id])

            db.update_source(source_id, analysis_completed=0)
            self.assertEqual([row["id"] for row in db.list_sources(view="candidate")], [source_id])

    def test_source_pagination_uses_limit_offset_and_total(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Database(Path(tmp) / "test.sqlite3")
            for index in range(25):
                db.add_source({"platform": "youtube", "video_id": f"page-{index}",
                               "url": f"https://example.test/{index}", "title": str(index)})

            self.assertEqual(db.count_sources("candidate"), 25)
            self.assertEqual(len(db.list_sources(20, "candidate", 0)), 20)
            self.assertEqual(len(db.list_sources(20, "candidate", 20)), 5)

    def test_source_list_filters_bucket_and_overlong_candidates(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Database(Path(tmp) / "test.sqlite3")
            short_t1, _ = db.add_source({"platform": "youtube", "video_id": "short-t1",
                                         "url": "https://example.test/1", "title": "T1",
                                         "target_unit": "T1.1", "duration": 120})
            db.add_source({"platform": "youtube", "video_id": "long-t1",
                           "url": "https://example.test/2", "title": "Long",
                           "target_unit": "T1.2", "duration": 601})
            t2, _ = db.add_source({"platform": "youtube", "video_id": "short-t2",
                                   "url": "https://example.test/3", "title": "T2",
                                   "target_unit": "T2.1", "duration": 90})
            self.assertEqual(db.count_sources("candidate", max_duration=600), 2)
            self.assertEqual([row["id"] for row in db.list_sources(view="candidate", bucket="T1",
                                                                     max_duration=600)], [short_t1])
            self.assertEqual([row["id"] for row in db.list_sources(view="candidate", bucket="T2",
                                                                     max_duration=600)], [t2])

    def test_waiting_candidates_filter_by_bucket(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Database(Path(tmp) / "test.sqlite3")
            source_id, _ = db.add_source({"platform": "youtube", "video_id": "bucketed",
                                          "url": "https://example.test/v", "title": "A"})
            t1, _ = db.add_candidate({"source_id": source_id, "start_time": 0, "end_time": 10,
                                      "duration": 10, "candidate_bucket": "T1", "candidate_unit": "T1.1"})
            db.add_candidate({"source_id": source_id, "start_time": 10, "end_time": 20,
                              "duration": 10, "candidate_bucket": "T2", "candidate_unit": "T2.1"})
            self.assertEqual(db.count_candidates("WAITING_REVIEW", "T1"), 1)
            self.assertEqual([row["id"] for row in db.list_candidates("WAITING_REVIEW", bucket="T1")], [t1])

    def test_exported_candidate_moves_to_processed_and_can_be_restored(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Database(Path(tmp) / "test.sqlite3")
            source_id, _ = db.add_source(
                {"platform": "youtube", "video_id": "delivery-state", "url": "https://example.test/v", "title": "A"}
            )
            candidate_id, _ = db.add_candidate({"source_id": source_id, "start_time": 0.0, "end_time": 10.0,
                                                 "duration": 10.0, "status": "DELIVERABLE", "facts": {}})
            db.create_final_clip(candidate_id, duration=10.0, width=3840, height=2160, fps=30,
                                 has_audio=1, qa_status="PASS", deliverable_status="READY")

            self.assertEqual([row["id"] for row in db.list_final_candidates("pending")], [candidate_id])
            self.assertEqual(db.list_final_candidates("processed"), [])

            db.mark_delivery_exported([candidate_id])
            self.assertEqual(db.list_final_candidates("pending"), [])
            processed = db.list_final_candidates("processed")
            self.assertEqual([row["id"] for row in processed], [candidate_id])
            first_export_time = processed[0]["exported_at"]
            db.mark_delivery_exported([candidate_id], "2099-01-01T00:00:00+00:00")
            self.assertEqual(db.list_final_candidates("processed")[0]["exported_at"], first_export_time)

            db.restore_exported_candidate(candidate_id)
            self.assertEqual([row["id"] for row in db.list_final_candidates("pending")], [candidate_id])
            self.assertEqual(db.list_final_candidates("processed"), [])

    def test_delivery_sequence_is_persistent_per_unit_and_reused_on_retry(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "test.sqlite3"
            db = Database(path)
            source_id, _ = db.add_source(
                {"platform": "youtube", "video_id": "numbering", "url": "https://example.test/v", "title": "城市跑步跟拍"}
            )

            def add_final(unit: str, start: float) -> int:
                candidate_id, _ = db.add_candidate({"source_id": source_id, "start_time": start,
                                                     "end_time": start + 10, "duration": 10,
                                                     "candidate_unit": unit, "facts": {}})
                db.create_final_clip(candidate_id, qa_status="PASS", deliverable_status="PENDING")
                return candidate_id

            first = add_final("T1.1", 0)
            second = add_final("T1.1", 10)
            other_unit = add_final("T6.3", 20)
            self.assertEqual(db.reserve_delivery_filename(first, "T1.1", "城市跑步跟拍")["filename"],
                             "T1.1_001_城市跑步跟拍.mp4")
            self.assertEqual(db.reserve_delivery_filename(second, "T1.1", "城市跑步跟拍")["filename"],
                             "T1.1_002_城市跑步跟拍.mp4")
            self.assertEqual(db.reserve_delivery_filename(other_unit, "T6.3", "城市跑步跟拍")["filename"],
                             "T6.3_001_城市跑步跟拍.mp4")

            reopened = Database(path)
            self.assertEqual(reopened.reserve_delivery_filename(first, "T1.1", "另一个标题")["filename"],
                             "T1.1_001_城市跑步跟拍.mp4")
            third = add_final("T1.1", 30)
            self.assertEqual(reopened.reserve_delivery_filename(third, "T1.1", "新片段")["filename"],
                             "T1.1_003_新片段.mp4")


class CandidateUnitFilterTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Database(Path(self.tmp.name) / "test.sqlite3")
        source_id, _ = self.db.add_source({"platform": "test", "video_id": "unit-filter",
                                           "url": "https://example.test/unit-filter"})
        self.ids = {}
        candidates = [
            ("t11", "T1", "T1.1", "WAITING_REVIEW"),
            ("t11_inferred", None, "T1.1", "WAITING_REVIEW"),
            ("t12", "T1", "T1.2", "WAITING_REVIEW"),
            ("t22", "T2", "T2.2", "WAITING_REVIEW"),
            ("t11_rejected", "T1", "T1.1", "REJECTED"),
            ("null_labels", None, None, "WAITING_REVIEW"),
            ("empty_labels", "", "", "WAITING_REVIEW"),
            ("bucket_only", "T1", None, "WAITING_REVIEW"),
            ("unit_prefix", "T1", "T1.10", "WAITING_REVIEW"),
        ]
        for index, (name, bucket, unit, status) in enumerate(candidates):
            self.ids[name], _ = self.db.add_candidate({
                "source_id": source_id, "start_time": index * 10, "end_time": index * 10 + 10,
                "duration": 10, "candidate_bucket": bucket, "candidate_unit": unit, "status": status,
            })

    def assert_candidates(self, names, **filters):
        expected = [self.ids[name] for name in names]
        self.assertEqual(self.db.count_candidates(**filters), len(expected))
        self.assertEqual([row["id"] for row in self.db.list_candidates(**filters)], expected)

    def test_unit_filter_is_exact_and_works_without_bucket_or_status(self):
        self.assert_candidates(["t11", "t11_inferred", "t11_rejected"], unit="T1.1")
        self.assert_candidates(["t22"], unit="T2.2")

    def test_unit_combines_with_status_and_bucket_including_inferred_bucket(self):
        self.assert_candidates(["t11", "t11_inferred"],
                               status="WAITING_REVIEW", bucket="T1", unit="T1.1")
        self.assert_candidates(["t11_rejected"], status="REJECTED", unit="T1.1")
        self.assert_candidates(["t22"], status="WAITING_REVIEW", bucket="T2", unit="T2.2")

    def test_empty_filters_and_existing_positional_calls_remain_compatible(self):
        self.assert_candidates(list(self.ids), status="", bucket="", unit="")
        self.assert_candidates(list(self.ids), status=None, bucket=None, unit=None)
        self.assertEqual(self.db.count_candidates("WAITING_REVIEW", "T1"), 5)
        self.assertEqual([row["id"] for row in self.db.list_candidates("WAITING_REVIEW", 1, 1, "T1")],
                         [self.ids["t11_inferred"]])
        self.assert_candidates(["t11", "t11_inferred", "t12", "bucket_only", "unit_prefix"],
                               status="WAITING_REVIEW", bucket="T1", unit="")

    def test_unassigned_requires_both_labels_empty_and_cannot_match_a_unit(self):
        self.assert_candidates(["null_labels", "empty_labels"], bucket="unassigned", unit="")
        self.assert_candidates([], bucket="unassigned", unit="T1.1")

    def test_no_matches_and_sql_metacharacters_are_not_interpreted(self):
        for filters in ({"bucket": "T2", "unit": "T1.1"},
                        {"status": "REJECTED", "unit": "T2.2"},
                        {"unit": "T9.999"}, {"unit": "T1.%"},
                        {"unit": "T1.1' OR 1=1 --"}):
            with self.subTest(filters=filters):
                self.assert_candidates([], **filters)

    def test_unit_pagination_counts_all_matches_and_preserves_order(self):
        filters = {"status": "WAITING_REVIEW", "bucket": "T1", "unit": "T1.1"}
        self.assertEqual(self.db.count_candidates(**filters), 2)
        for offset, names in ((0, ["t11"]), (1, ["t11_inferred"]), (2, [])):
            with self.subTest(offset=offset):
                rows = self.db.list_candidates(limit=1, offset=offset, **filters)
                self.assertEqual([row["id"] for row in rows], [self.ids[name] for name in names])


if __name__ == "__main__":
    unittest.main()
