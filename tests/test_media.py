from __future__ import annotations

import csv
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from qt_tool.db import Database
from qt_tool.media import (DownloadStalled, MediaPipeline, _run_download,
                           delivery_description, merge_facts,
                           source_duration_allowed, source_live_reason,
                           split_long_segment, ytdlp_error_message)
from qt_tool.subject import select_motion_valley, split_presence_samples


class MediaTests(unittest.TestCase):
    def test_low_resolution_rejected_before_clip(self):
        from unittest.mock import Mock
        from qt_tool.rules import RuleEngine
        with tempfile.TemporaryDirectory() as tmp:
            db = Database(Path(tmp) / 'test.sqlite3')
            sid, _ = db.add_source({'platform': 'test', 'video_id': 'low', 'url': 'https://example.test/low'})
            db.update_source(sid, original_path=str(Path(tmp) / 'original.mp4'))
            cid, _ = db.add_candidate({'source_id': sid, 'start_time': 0, 'end_time': 6, 'duration': 6,
                                      'candidate_bucket': 'T8', 'candidate_unit': 'T8.3'})
            db.review(cid, {'decision': 'ACCEPT', 'final_viewpoint': 'third_person'})
            rules = Mock()
            rules._r9 = lambda facts, bucket: RuleEngine._r9(None, facts, bucket)
            pipeline = MediaPipeline(SimpleNamespace(data_dir=Path(tmp)), db, rules)
            pipeline.probe = Mock(return_value={'width': 1920, 'height': 1080})
            pipeline.download_final = Mock(return_value=Path(tmp) / 'original.mp4')
            pipeline.clip_final = Mock(side_effect=AssertionError('must not encode'))
            result = pipeline.final_qa_and_deliver(cid)
            self.assertEqual(result['qa_status'], 'FAIL')
            self.assertIn('1920x1080', result['message'])
            self.assertEqual(db.get_candidate(cid)['status'], 'REJECTED')
            pipeline.clip_final.assert_not_called()

    def test_source_duration_cap_keeps_unknown_and_limits_known_duration(self):
        self.assertTrue(source_duration_allowed(None, 600))
        self.assertTrue(source_duration_allowed(600, 600))
        self.assertFalse(source_duration_allowed(600.1, 600))

    def test_live_sources_are_detected_without_rejecting_finite_replays(self):
        self.assertEqual(source_live_reason({"live_status": "is_live", "duration": None}), "正在直播")
        self.assertEqual(source_live_reason({"title": "Ocean LIVE 24/7", "duration": None}),
                         "疑似直播或无限循环视频")
        self.assertIsNone(source_live_reason({"live_status": "was_live", "duration": 300}))

    def test_stalled_download_is_terminated(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(DownloadStalled):
                _run_download([sys.executable, "-c", "import time; time.sleep(5)"],
                              Path(tmp), "*.part", stall_timeout=1, timeout=10)

    def test_legacy_live_source_is_rejected_before_job_download(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            db = Database(data_dir / "test.sqlite3")
            source_id, _ = db.add_source({"platform": "youtube", "video_id": "live",
                                          "url": "https://example.test/live", "title": "LIVE 24/7",
                                          "metadata": {"live_status": "is_live"}})
            pipeline = MediaPipeline(SimpleNamespace(data_dir=data_dir, source_max_duration_seconds=600,
                                                      ytdlp_bin="missing-yt-dlp"), db, None)
            with self.assertRaisesRegex(ValueError, "不支持下载正在直播"):
                pipeline.validate_proxy_source(source_id)

    def test_ytdlp_errors_are_operator_friendly(self):
        cookie = "ERROR: could not find firefox cookies database in C:/Profiles"
        network = "HTTPSConnection: Failed to establish a new connection: [WinError 10013]"
        self.assertIn("Firefox 登录信息", ytdlp_error_message(cookie, "代理下载"))
        self.assertEqual(ytdlp_error_message(network, "搜索"),
                         "搜索失败：无法连接 YouTube。请检查网络或代理设置后重试。")

    def test_motion_valley_prefers_quiet_point_before_action_rise(self):
        samples = [(0.1 * index, value) for index, value in enumerate(
            [24, 22, 20, 18, 16, 14, 14, 15, 18, 24, 30, 32, 29]
        )]
        selected = select_motion_valley(samples)
        self.assertIsNotNone(selected)
        self.assertGreaterEqual(selected[0], 0.4)
        self.assertLessEqual(selected[0], 0.8)

    def test_motion_valley_does_not_guess_without_sustained_rise(self):
        samples = [(0.1 * index, value) for index, value in enumerate(
            [20, 19, 18, 17, 16, 16, 17, 16, 17, 18, 17, 18, 19]
        )]
        self.assertIsNone(select_motion_valley(samples))

    def test_subject_loss_splits_instead_of_rejecting_whole_shot(self):
        present = [index / 2 for index in range(0, 29)]
        present += [19 + index / 2 for index in range(0, 16)]
        segments, gaps = split_presence_samples(0.0, 26.62, present)
        self.assertEqual(segments, ((0.0, 14.25), (19.0, 26.62)))
        self.assertEqual(gaps, ({"start": 14.25, "end": 19.0, "duration": 4.75},))

    def test_short_detector_dropout_does_not_split(self):
        present = [index / 2 for index in range(0, 29)]
        present += [18 + index / 2 for index in range(0, 3)]
        present += [22 + index / 2 for index in range(0, 10)]
        segments, gaps = split_presence_samples(0.0, 26.5, present)
        self.assertEqual(segments, ((0.0, 26.5),))
        self.assertEqual(gaps, ())

    def test_delivery_description_preserves_chinese_and_sanitizes_title(self):
        self.assertEqual(delivery_description('  城市跑步 / 跟拍: 4K  '), "城市跑步_跟拍_4K")

    def test_final_probe_values_override_candidate_facts_without_duplicate_keys(self):
        facts = merge_facts('{"duration": 12, "width": 854}',
                            {"duration": 60.074, "width": 3840, "height": 2160},
                            duration=60.074, shot_count=4)
        self.assertEqual(facts["duration"], 60.074)
        self.assertEqual(facts["width"], 3840)
        self.assertEqual(facts["height"], 2160)
        self.assertEqual(facts["shot_count"], 4)

    def test_delivery_csv_uses_required_chinese_columns(self):
        row = {
            "candidate_id": 42,
            "viewpoint": "third_person", "created_at": "2026-09-20T10:00:00+00:00",
            "exported_at": "2026-09-20T11:00:00+00:00",
            "unit": "T1.1", "width": 3840, "height": 2160, "duration": 12.3456,
        }

        class DeliveryDB:
            exported_ids = []

            @staticmethod
            def delivery_rows():
                return [row]

            @classmethod
            def mark_delivery_exported(cls, candidate_ids, exported_at=None):
                cls.exported_ids = candidate_ids

        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            (data_dir / "deliverable").mkdir()
            pipeline = MediaPipeline(SimpleNamespace(data_dir=data_dir), DeliveryDB(), None)
            output = pipeline.export_delivery_csv()
            with output.open(encoding="utf-8-sig", newline="") as stream:
                exported = list(csv.DictReader(stream))

        self.assertEqual(list(exported[0]), ["人称", "OSS路径", "交付时间", "统合单元", "分辨率", "时长"])
        self.assertEqual(exported[0]["人称"], "第三人称")
        self.assertEqual(exported[0]["OSS路径"], "OSS")
        self.assertEqual(exported[0]["交付时间"], "2026-09-20T11:00:00+00:00")
        self.assertEqual(exported[0]["统合单元"], "T1.1")
        self.assertEqual(exported[0]["分辨率"], "3840x2160")
        self.assertEqual(exported[0]["时长"], "12.346")
        self.assertEqual(DeliveryDB.exported_ids, [42])

    def test_pipeline_repairs_legacy_delivery_filename(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            db_path = data_dir / "test.sqlite3"
            db = Database(db_path)
            source_id, _ = db.add_source({"platform": "youtube", "video_id": "legacy",
                                          "url": "https://example.test/v", "title": "城市跑步跟拍"})
            candidate_id, _ = db.add_candidate({"source_id": source_id, "start_time": 0.0, "end_time": 10.0,
                                                 "duration": 10.0, "candidate_unit": "T1.1", "facts": {}})
            deliver_dir = data_dir / "deliverable" / "QT寻源数据" / "T1_高动态载具" / "第三人称"
            deliver_dir.mkdir(parents=True)
            legacy_path = deliver_dir / "unknown_legacy-1.mp4"
            legacy_path.write_bytes(b"video")
            db.create_final_clip(candidate_id, final_path=str(legacy_path), qa_status="PASS",
                                 deliverable_status="READY")

            migrated = Database(db_path)
            MediaPipeline(SimpleNamespace(data_dir=data_dir), migrated, None)
            expected = deliver_dir / "T1.1_001_城市跑步跟拍.mp4"
            self.assertTrue(expected.is_file())
            self.assertFalse(legacy_path.exists())
            self.assertEqual(migrated.delivery_rows()[0]["final_path"], str(expected))

    def test_segments_within_the_long_bucket_are_left_untouched(self):
        self.assertEqual(split_long_segment(0.0, 59.0), [(0.0, 59.0)])
        self.assertEqual(split_long_segment(12.5, 72.5), [(12.5, 72.5)])
        self.assertEqual(split_long_segment(0.0, 8.0), [(0.0, 8.0)])

    def test_61_seconds_becomes_two_deliverable_parts(self):
        parts = split_long_segment(0.0, 61.0)
        self.assertEqual(len(parts), 2)
        self.assertEqual(parts[0][0], 0.0)
        self.assertEqual(parts[-1][1], 61.0)
        for start, end in parts:
            self.assertTrue(30.0 <= end - start <= 60.0, parts)

    def test_five_minute_shot_is_split_into_contiguous_30_to_60s_parts(self):
        parts = split_long_segment(5.0, 305.0)
        self.assertGreater(len(parts), 1)
        self.assertEqual(parts[0][0], 5.0)
        self.assertEqual(parts[-1][1], 305.0)
        for start, end in parts:
            self.assertTrue(30.0 <= end - start <= 60.0, parts)
        for (_, previous_end), (next_start, _) in zip(parts, parts[1:]):
            self.assertAlmostEqual(previous_end, next_start, places=3)

    def test_split_avoids_the_15_and_30_second_conflict_boundaries(self):
        for end in (60.02, 61.0, 90.001, 90.05, 120.0, 150.0, 240.0, 300.0, 601.0):
            for start, stop in split_long_segment(0.0, end):
                for boundary in (15.0, 30.0):
                    self.assertGreater(abs((stop - start) - boundary), 0.05,
                                       f"{end}s 切出 {start}-{stop} 压在 {boundary}s 边界上")

    def test_analyze_source_splits_long_shots_and_records_the_part_number(self):
        from unittest.mock import Mock
        with tempfile.TemporaryDirectory() as tmp:
            db = Database(Path(tmp) / 'test.sqlite3')
            sid, _ = db.add_source({'platform': 'test', 'video_id': 'long', 'url': 'https://example.test/long'})
            db.update_source(sid, proxy_path=str(Path(tmp) / 'proxy.mp4'))
            rules = Mock()
            rules.buckets = {}
            rules.duration_bucket = Mock(return_value=('long', None, ''))
            rules.evaluate = Mock(return_value=[])
            rules.unit_gate = Mock(return_value=None)
            rules.automatic_reject = Mock(return_value=False)
            pipeline = MediaPipeline(SimpleNamespace(data_dir=Path(tmp)), db, rules)
            pipeline.probe = Mock(return_value={'playable': True, 'duration': 300.0})
            pipeline.detect_shots = Mock(return_value=[(0.0, 300.0)])
            pipeline.representative_frame_hash = Mock(return_value=None)
            pipeline.subject_analyzer = Mock(analyze=Mock(return_value=SimpleNamespace(
                segments=((0.0, 300.0),), facts_for=lambda segment: {})))

            created = pipeline.analyze_source(sid)

            self.assertGreater(len(created), 1)
            notes = [json.loads(db.get_candidate(cid)['facts_json'])['split_note'] for cid in created]
            self.assertEqual(notes, [f'长镜头等分 {i}/{len(created)}' for i in range(1, len(created) + 1)])
            durations = [db.get_candidate(cid)['duration'] for cid in created]
            self.assertTrue(all(30.0 <= value <= 60.0 for value in durations), durations)


if __name__ == "__main__":
    unittest.main()
