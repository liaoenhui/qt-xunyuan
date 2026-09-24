from __future__ import annotations

import json
import os
import unittest
import tempfile
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
from pathlib import Path
from dataclasses import replace
from threading import Event, Thread
from types import SimpleNamespace
from urllib.parse import urlencode
from qt_tool.config import ROOT, load_settings
from qt_tool.db import Database
from qt_tool.rules import RuleEngine

from qt_tool.web import Handler, App
from unittest.mock import Mock, patch


class WebPaginationTests(unittest.TestCase):
    def test_preflight_runs_while_download_pool_is_occupied(self):
        with tempfile.TemporaryDirectory() as tmp, \
                patch.dict(os.environ, {"DATA_DIR": tmp}), \
                patch("qt_tool.config._load_dotenv"), \
                patch("qt_tool.config._detect_cookies_file", return_value=""):
            settings = replace(load_settings(), db_path=Path(tmp) / 'test.sqlite3', max_download_concurrency=1)
            app = App(settings)
            release = Event()
            downloading = Event()
            checked = Event()
            def download(*args, **kwargs):
                downloading.set()
                release.wait(5)
                return Path(tmp) / 'proxy.mp4'
            def preflight(*args, **kwargs):
                checked.set()
                return {'reason': 'test'}
            app.pipeline.validate_proxy_source = Mock()
            app.pipeline.download_proxy = download
            app.pipeline.preflight_source = preflight
            try:
                ids = [app.db.add_source({'platform': 'test', 'video_id': str(i), 'url': f'https://example.test/{i}'})[0] for i in range(3)]
                app.queue_source_job('proxy', ids[0])
                self.assertTrue(downloading.wait(2))
                queued, _ = app.queue_source_job('proxy', ids[1])
                check, _ = app.queue_source_job('preflight', ids[2])
                self.assertTrue(checked.wait(2), '规格检查不应等待下载队列')
                self.assertEqual(app.db.get_job(queued)['status'], 'QUEUED')
                self.assertEqual(app.db.get_job(queued)['queue_ahead'], 1)
                self.assertEqual(app.queue_source_job('preflight', ids[0])[1], False)
            finally:
                release.set()
                for executor in app.executors.values():
                    executor.shutdown(wait=True)

    def test_analysis_summary_distinguishes_rejected_from_waiting(self):
        app = App.__new__(App)
        app.db = Mock()
        app.db.get_candidate.side_effect = lambda cid: {'status': 'REJECTED' if cid == 1 else 'WAITING_REVIEW'}
        app.db.get_rule_results.return_value = [{'rule_id': 'SPEC_FPS', 'status': 'FAIL', 'deterministic': True, 'reason': '23.976 fps 低于 24 fps'}]
        result = app.analysis_summary([1, 2])
        self.assertEqual((result['waiting_count'], result['rejected_count']), (1, 1))
        self.assertIn('待人工审核 1 个，自动拒绝 1 个', result['message'])
        self.assertIn('23.976', result['message'])
        result = app.analysis_summary([1])
        self.assertEqual(result['waiting_count'], 0)
        self.assertIn('拒绝列表', result['message'])
        self.assertIn('本次未新增候选', app.analysis_summary([])['message'])

    def test_quota_compares_accepted_total_with_bucket_target(self):
        rules = RuleEngine(ROOT / "rules" / "qt_rules_v4.yaml", ROOT / "rules" / "conflicts.yaml")
        db = Mock()
        db.quota_state.return_value = [
            {"bucket": "T1", "duration_bucket": "short", "count": 3},
            {"bucket": "T1", "duration_bucket": "long", "count": 2},
            {"bucket": "T2", "duration_bucket": "short", "count": 999},
        ]
        handler = Handler.__new__(Handler)
        handler.app = SimpleNamespace(db=db, rules=rules)
        quota = {row["bucket"]: row for row in handler._quota()}
        t1 = quota["T1"]
        self.assertEqual(t1["total"], {"actual": 5, "target": rules.buckets["T1"]["target_total"]})
        self.assertEqual(t1["duration"]["short"]["actual"], 3)
        self.assertEqual(t1["duration"]["medium"]["actual"], 0)
        self.assertEqual(t1["largest_gap"], "短档")
        self.assertEqual(quota["T2"]["largest_gap"], "已达标")
        self.assertNotIn("first_person", t1)
        self.assertNotIn("third_person", t1)

    def test_pagination_is_twenty_by_default_and_clamps_page(self):
        self.assertEqual(Handler._pagination({}, 45), (1, 20, 0))
        self.assertEqual(Handler._pagination({"page": ["2"]}, 45), (2, 20, 20))
        self.assertEqual(Handler._pagination({"page": ["99"]}, 45), (3, 20, 40))

    def test_explicit_page_size_keeps_review_queue_compatibility(self):
        self.assertEqual(Handler._pagination({"limit": ["500"]}, 420, 100), (1, 500, 0))


class CandidateFilterHTTPTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        # Only the GET dependencies are needed; do not load local settings or start job pools.
        app = App.__new__(App)
        app.db = Database(Path(self.tmp.name) / "test.sqlite3")
        app.settings = SimpleNamespace(root=ROOT)
        app.rules = RuleEngine(ROOT / "rules/qt_rules_v4.yaml", ROOT / "rules/conflicts.yaml")
        source_id, _ = app.db.add_source({"platform": "test", "video_id": "http-unit-filter",
                                          "url": "https://example.test/http-unit-filter"})
        self.ids = {}
        candidates = [
            ("t11", "T1", "T1.1", "WAITING_REVIEW"),
            ("t12", "T1", "T1.2", "WAITING_REVIEW"),
            ("t22", "T2", "T2.2", "WAITING_REVIEW"),
            ("t11_inferred", None, "T1.1", "WAITING_REVIEW"),
            ("t11_rejected", "T1", "T1.1", "REJECTED"),
            ("null_labels", None, None, "WAITING_REVIEW"),
            ("empty_labels", "", "", "WAITING_REVIEW"),
            ("bucket_only", "T1", None, "WAITING_REVIEW"),
        ]
        for index, (name, bucket, unit, status) in enumerate(candidates):
            self.ids[name], _ = app.db.add_candidate({
                "source_id": source_id, "start_time": index * 10, "end_time": index * 10 + 10,
                "duration": 10, "candidate_bucket": bucket, "candidate_unit": unit, "status": status,
            })
        handler = type("TestHandler", (Handler,), {"app": app, "log_message": lambda *args: None})
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.addCleanup(self.server.server_close)
        thread = Thread(target=self.server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join)
        self.addCleanup(self.server.shutdown)

    def get(self, path):
        connection = HTTPConnection(*self.server.server_address, timeout=5)
        try:
            connection.request("GET", path)
            response = connection.getresponse()
            body = response.read().decode("utf-8")
            self.assertEqual(response.status, 200, body)
            return body
        finally:
            connection.close()

    def candidates(self, **query):
        payload = json.loads(self.get("/api/candidates?" + urlencode(query)))
        self.assertTrue(payload["ok"])
        return payload

    def assert_candidates(self, names, **query):
        payload = self.candidates(**query)
        self.assertEqual(payload["total"], len(names))
        self.assertEqual([row["id"] for row in payload["items"]], [self.ids[name] for name in names])
        return payload

    def test_unit_combines_with_bucket_and_status_over_http(self):
        self.assert_candidates(["t11", "t11_inferred", "t11_rejected"], unit="T1.1")
        payload = self.assert_candidates(["t11", "t11_inferred"],
                                         status="WAITING_REVIEW", bucket="T1", unit="T1.1")
        self.assertEqual(payload["unit"], "T1.1")
        self.assertEqual(payload["bucket"], "T1")
        self.assert_candidates(["t22"], status="WAITING_REVIEW", bucket="T2", unit="T2.2")
        self.assert_candidates(["t11_rejected"], status="REJECTED", unit="T1.1")

    def test_omitted_and_empty_filters_preserve_existing_http_calls(self):
        omitted = self.assert_candidates(list(self.ids))
        empty = self.assert_candidates(list(self.ids), status="", bucket="", unit="")
        self.assertEqual(empty, omitted)
        self.assertIsNone(omitted["unit"])
        self.assertEqual(omitted["page_size"], 100)
        self.assert_candidates(["t11", "t12", "t11_inferred", "bucket_only"],
                               status="WAITING_REVIEW", bucket="T1")
        limited = self.candidates(status="WAITING_REVIEW", limit=1)
        self.assertEqual((limited["total"], limited["page_size"], len(limited["items"])), (7, 1, 1))

    def test_unassigned_and_no_match_combinations_over_http(self):
        self.assert_candidates(["null_labels", "empty_labels"], bucket="unassigned", unit="")
        for query in ({"bucket": "unassigned", "unit": "T1.1"},
                      {"bucket": "T2", "unit": "T1.1"},
                      {"status": "REJECTED", "unit": "T2.2"},
                      {"unit": "T9.999"}, {"unit": "T1.%"},
                      {"unit": "T1.1' OR 1=1 --"}):
            with self.subTest(query=query):
                payload = self.assert_candidates([], page=99, page_size=1, **query)
                self.assertEqual(payload["page"], 1)

    def test_filtered_pagination_uses_filtered_total_and_clamps_last_page(self):
        for page, expected_page, name in ((1, 1, "t11"), (2, 2, "t11_inferred"), (99, 2, "t11_inferred")):
            with self.subTest(page=page):
                payload = self.candidates(status="WAITING_REVIEW", bucket="T1", unit="T1.1", page=page, page_size=1)
                self.assertEqual((payload["total"], payload["page"], payload["page_size"]), (2, expected_page, 1))
                self.assertEqual([row["id"] for row in payload["items"]], [self.ids[name]])

    def test_review_page_exposes_unit_filter_and_rules_supply_unit_options(self):
        html = self.get("/review")
        self.assertIn('id="review-bucket-filter"', html)
        self.assertIn('id="review-unit-filter" aria-label="审核单元"', html)
        rules = json.loads(self.get("/api/rules"))
        self.assertTrue(rules["ok"])
        self.assertIn("T1.1", rules["units"])
        self.assertIn("T2.2", rules["units"])


class SourceRejectHTTPTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        app = App.__new__(App)
        app.db = Database(Path(self.tmp.name) / "test.sqlite3")
        app.settings = SimpleNamespace(root=ROOT)
        app.rules = RuleEngine(ROOT / "rules/qt_rules_v4.yaml", ROOT / "rules/conflicts.yaml")
        self.db = app.db
        self.source_a, _ = app.db.add_source({"platform": "test", "video_id": "a", "url": "https://example.test/a"})
        self.source_b, _ = app.db.add_source({"platform": "test", "video_id": "b", "url": "https://example.test/b"})
        self.ids = {}
        for index, (name, source, status) in enumerate([
                ("a_wait1", self.source_a, "WAITING_REVIEW"), ("a_wait2", self.source_a, "WAITING_REVIEW"),
                ("a_accepted", self.source_a, "ACCEPTED"), ("a_rejected", self.source_a, "REJECTED"),
                ("b_wait", self.source_b, "WAITING_REVIEW")]):
            self.ids[name], _ = app.db.add_candidate({
                "source_id": source, "start_time": index * 10, "end_time": index * 10 + 10, "duration": 10,
                "candidate_bucket": "T1", "candidate_unit": "T1.1", "status": status})
        handler = type("TestHandler", (Handler,), {"app": app, "log_message": lambda *args: None})
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.addCleanup(self.server.server_close)
        thread = Thread(target=self.server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join)
        self.addCleanup(self.server.shutdown)

    def post(self, path, body):
        connection = HTTPConnection(*self.server.server_address, timeout=5)
        try:
            connection.request("POST", path, body=json.dumps(body).encode("utf-8"),
                               headers={"Content-Type": "application/json"})
            response = connection.getresponse()
            return response.status, json.loads(response.read().decode("utf-8"))
        finally:
            connection.close()

    def status_of(self, name):
        return self.db.get_candidate(self.ids[name])["status"]

    def latest_notes(self, name):
        with self.db.connect() as con:
            return con.execute("SELECT notes FROM reviews WHERE candidate_id=? ORDER BY id DESC LIMIT 1",
                               (self.ids[name],)).fetchone()["notes"]

    def test_only_waiting_candidates_of_that_source_are_rejected(self):
        status, data = self.post(f"/api/sources/{self.source_a}/reject-waiting", {"notes": " 整段远景 "})
        self.assertEqual(status, 200)
        self.assertEqual(data["rejected_ids"], [self.ids["a_wait1"], self.ids["a_wait2"]])
        self.assertEqual(data["count"], 2)
        self.assertEqual((self.status_of("a_wait1"), self.status_of("a_wait2")), ("REJECTED", "REJECTED"))
        self.assertEqual(self.status_of("a_accepted"), "ACCEPTED")
        self.assertEqual(self.status_of("b_wait"), "WAITING_REVIEW")
        self.assertEqual(self.latest_notes("a_wait2"), "整段远景")
        status, data = self.post(f"/api/sources/{self.source_a}/reject-waiting", {})
        self.assertEqual((status, data["count"]), (200, 0))

    def test_notes_are_optional_and_unknown_source_is_404(self):
        status, data = self.post(f"/api/sources/{self.source_b}/reject-waiting", {})
        self.assertEqual((status, data["rejected_ids"]), (200, [self.ids["b_wait"]]))
        self.assertEqual(self.latest_notes("b_wait"), "")
        status, _ = self.post("/api/sources/99999/reject-waiting", {"notes": "x"})
        self.assertEqual(status, 404)


if __name__ == "__main__":
    unittest.main()
