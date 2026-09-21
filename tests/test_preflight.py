import json
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from qt_tool.db import Database
from qt_tool.media import DownloadCancelled, MediaPipeline, _run_download, format_preflight
from qt_tool.web import App


class PreflightTests(unittest.TestCase):
    def test_specs_must_match_in_same_format(self):
        formats = [dict(vcodec='vp9', width=3840, height=2160, fps=23),
                   dict(vcodec='h264', width=1280, height=720, fps=30)]
        self.assertEqual(format_preflight(formats, 'T7')['status'], 'FAIL')
        # 23.976（24000/1001）是标准 24p，与规则引擎口径一致，预检不得拦下
        self.assertEqual(format_preflight([dict(vcodec='vp9', width=3840, height=2160, fps=23.976)], 'T7')['status'], 'PASS')
        self.assertEqual(format_preflight(formats, 'T9')['status'], 'PASS')
        formats.append(dict(vcodec='vp9', width=2560, height=1440, fps=24))
        self.assertEqual(format_preflight(formats, 'T7')['status'], 'PASS')
        self.assertEqual(format_preflight([], 'T7')['status'], 'UNKNOWN')
        self.assertEqual(format_preflight(formats, None)['status'], 'UNKNOWN')

    def test_preflight_blocks_before_download_and_unknown_requires_opt_in(self):
        pipeline = MediaPipeline.__new__(MediaPipeline)
        pipeline.validate_proxy_source = Mock(return_value={})
        pipeline.preflight_source = Mock(return_value={'status': 'FAIL', 'reason': '规格不足'})
        with self.assertRaisesRegex(ValueError, '规格不足'):
            pipeline.download_proxy(1, allow_unknown=True)
        pipeline.preflight_source.return_value = {'status': 'UNKNOWN', 'reason': '格式未知'}
        with self.assertRaisesRegex(ValueError, '格式未知'):
            pipeline.download_proxy(1)

    def test_cancel_stops_running_process(self):
        event = threading.Event()
        timer = threading.Timer(0.2, event.set)
        with tempfile.TemporaryDirectory() as tmp:
            timer.start()
            try:
                with self.assertRaises(DownloadCancelled):
                    _run_download([sys.executable, '-c', 'import time; time.sleep(30)'],
                                  Path(tmp), '*.part', 60, 60, cancel=event)
            finally:
                timer.cancel()

    def test_queued_cancel_never_runs_download(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = App.__new__(App)
            app.db = Database(Path(tmp) / 'test.sqlite3')
            app.job_lock = threading.Lock()
            app.pipeline = Mock()
            source, _ = app.db.add_source({'platform': 'test', 'video_id': 'cancel', 'url': 'https://example.test/v'})
            job, _ = app.db.create_job('proxy', source)
            app.cancel_events = {job: threading.Event()}
            app.cancel_job(job)
            app._run_source_job(job, 'proxy', source)
            self.assertEqual(app.db.get_job(job)['status'], 'CANCELLED')
            app.pipeline.download_proxy.assert_not_called()

    def test_failed_metadata_does_not_start_video_download(self):
        pipeline = MediaPipeline.__new__(MediaPipeline)
        pipeline.validate_proxy_source = Mock(return_value={'url': 'https://example.test/v', 'metadata_json': '{}'})
        pipeline._ytdlp = Mock(return_value=['yt-dlp'])
        pipeline.settings = SimpleNamespace(data_dir=Path('.'))
        with patch('qt_tool.media._run_download', return_value=SimpleNamespace(returncode=1, stderr='network unavailable')) as run:
            with self.assertRaises(RuntimeError):
                pipeline.download_proxy(1)
            self.assertEqual(run.call_count, 1)

    def test_proxy_frame_rate_is_not_a_rejection(self):
        from qt_tool.rules import RuleEngine, RuleStatus
        result = RuleEngine._fps(None, {'source_type': 'PROXY', 'fps': 23.976}, 'T2')
        self.assertEqual(result.status, RuleStatus.UNKNOWN)
