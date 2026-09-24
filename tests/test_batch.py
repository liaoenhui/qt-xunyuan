from __future__ import annotations

import io
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import Mock

import tools_batch


TEMPLATES = {
    "T7.4": [f"query {i}" for i in range(1, 9)],
    "T7.5": ["bucket word a", "bucket word b"],
}

DISCOVER_STATS = {"found": 10, "created": 4, "excluded": 3, "excluded_live": 1,
                  "max_duration_seconds": 600}


def make_runner(sources: list[dict] | None = None) -> tuple[tools_batch.BatchRunner, Mock, Mock]:
    pipeline = Mock()
    db = Mock()
    db.list_sources.return_value = list(sources or [])
    return tools_batch.BatchRunner(pipeline, db, dict(TEMPLATES)), pipeline, db


def source(source_id: int, **fields) -> dict:
    base = {"id": source_id, "title": f"source {source_id}", "target_unit": "T7.4",
            "proxy_path": None, "analysis_completed": 0}
    base.update(fields)
    return base


def quietly(callback):
    buffer = io.StringIO()
    with redirect_stdout(buffer):
        result = callback()
    return result, buffer.getvalue()


class DiscoverTests(unittest.TestCase):
    def test_uses_only_the_requested_number_of_queries(self):
        runner, pipeline, _ = make_runner()
        pipeline.discover.return_value = dict(DISCOVER_STATS)
        totals, _ = quietly(lambda: runner.discover("T7.4", queries=3, limit=5))
        self.assertEqual(pipeline.discover.call_count, 3)
        self.assertEqual([c.args for c in pipeline.discover.call_args_list],
                         [("query 1", "T7.4", 5), ("query 2", "T7.4", 5), ("query 3", "T7.4", 5)])
        self.assertEqual(totals["queries"], 3)
        self.assertEqual((totals["found"], totals["created"], totals["excluded_live"]), (30, 12, 3))
        self.assertNotIn("max_duration_seconds", totals)

    def test_failed_query_is_counted_without_stopping_the_batch(self):
        runner, pipeline, _ = make_runner()
        pipeline.discover.side_effect = [RuntimeError("网络不通"), dict(DISCOVER_STATS)]
        totals, output = quietly(lambda: runner.discover("T7.4", queries=2, limit=10))
        self.assertEqual((totals["queries"], totals["failed_queries"]), (1, 1))
        self.assertEqual(totals["found"], 10)
        self.assertIn("网络不通", output)

    def test_bucket_collects_queries_from_every_child_unit(self):
        self.assertEqual(tools_batch.queries_for(TEMPLATES, "T7")[:2], TEMPLATES["T7.4"][:2])
        self.assertEqual(tools_batch.queries_for(TEMPLATES, "T7")[-2:], TEMPLATES["T7.5"])
        self.assertEqual(tools_batch.queries_for(TEMPLATES, "T7.5"), TEMPLATES["T7.5"])

    def test_missing_template_stops_the_command(self):
        runner, _, _ = make_runner()
        with self.assertRaises(SystemExit):
            quietly(lambda: runner.discover("T2.1"))


class ProxyTests(unittest.TestCase):
    def test_counts_the_three_kinds_of_skips(self):
        rows = [source(1), source(2), source(3), source(4),
                source(5, proxy_path="data/proxy/done.mp4")]
        runner, pipeline, _ = make_runner(rows)
        pipeline.preflight_source.side_effect = [
            {"status": "PASS", "reason": "规格满足"},
            {"status": "FAIL", "reason": "分辨率不足"},
            {"status": "UNKNOWN", "reason": "格式信息不完整"},
            RuntimeError("规格预检失败：无法连接"),
        ]
        pipeline.download_proxy.return_value = Path("data/proxy/youtube_a.mp4")
        totals, output = quietly(lambda: runner.proxy("T7.4", top=10))
        self.assertEqual(totals, {"selected": 4, "downloaded": 1, "skipped_preflight_fail": 1,
                                  "skipped_preflight_unknown": 1, "failed": 1})
        self.assertEqual(pipeline.download_proxy.call_count, 1)
        pipeline.download_proxy.assert_called_once_with(1, allow_unknown=False)
        self.assertIn("分辨率不足", output)
        self.assertIn("格式信息不完整", output)

    def test_allow_unknown_downloads_unknown_sources(self):
        runner, pipeline, _ = make_runner([source(1)])
        pipeline.preflight_source.return_value = {"status": "UNKNOWN", "reason": "格式信息不完整"}
        pipeline.download_proxy.return_value = Path("data/proxy/youtube_a.mp4")
        totals, _ = quietly(lambda: runner.proxy("T7.4", top=10, allow_unknown=True))
        self.assertEqual((totals["downloaded"], totals["skipped_preflight_unknown"]), (1, 0))
        pipeline.download_proxy.assert_called_once_with(1, allow_unknown=True)

    def test_unavailable_source_is_a_preflight_skip_not_a_failure(self):
        runner, pipeline, _ = make_runner([source(1)])
        pipeline.preflight_source.side_effect = ValueError("不支持下载正在直播；请选择有固定时长的公开视频")
        totals, output = quietly(lambda: runner.proxy("T7.4"))
        self.assertEqual((totals["skipped_preflight_fail"], totals["failed"]), (1, 0))
        self.assertIn("正在直播", output)
        pipeline.download_proxy.assert_not_called()

    def test_download_failure_is_counted_and_explained(self):
        runner, pipeline, _ = make_runner([source(1)])
        pipeline.preflight_source.return_value = {"status": "PASS", "reason": "ok"}
        pipeline.download_proxy.side_effect = RuntimeError("代理下载已自动停止：\n120 秒无进度")
        totals, output = quietly(lambda: runner.proxy("T7.4"))
        self.assertEqual((totals["failed"], totals["downloaded"]), (1, 0))
        self.assertIn("120 秒无进度", output)

    def test_top_limits_the_queue_by_score_order(self):
        rows = [source(i) for i in range(1, 6)]
        runner, pipeline, db = make_runner(rows)
        pipeline.preflight_source.return_value = {"status": "FAIL", "reason": "分辨率不足"}
        totals, _ = quietly(lambda: runner.proxy("T7.4", top=2))
        self.assertEqual(totals["selected"], 2)
        self.assertEqual(db.list_sources.call_args.kwargs["bucket"], "T7")

    def test_other_units_in_the_same_bucket_are_ignored(self):
        rows = [source(1, target_unit="T7.5"), source(2, target_unit="T7.4")]
        runner, pipeline, _ = make_runner(rows)
        pipeline.preflight_source.return_value = {"status": "PASS", "reason": "ok"}
        pipeline.download_proxy.return_value = Path("p.mp4")
        quietly(lambda: runner.proxy("T7.4"))
        pipeline.download_proxy.assert_called_once_with(2, allow_unknown=False)

    def test_min_id_skips_older_sources_before_top_is_applied(self):
        rows = [source(i) for i in (3, 8, 12, 15)]
        runner, pipeline, _ = make_runner(rows)
        pipeline.preflight_source.return_value = {"status": "FAIL", "reason": "分辨率不足"}
        totals, output = quietly(lambda: runner.proxy("T7.4", top=2, min_source_id=8))
        self.assertEqual(totals["selected"], 2)
        self.assertEqual([c.args[0] for c in pipeline.preflight_source.call_args_list], [8, 12])
        self.assertIn("id ≥ 8", output)
        args = tools_batch.build_parser().parse_args(["proxy", "T7.4", "--min-id", "1500"])
        self.assertEqual(args.min_id, 1500)
        self.assertEqual(tools_batch.build_parser().parse_args(["proxy", "T7.4"]).min_id, 0)


class LogTests(unittest.TestCase):
    def test_log_replaces_characters_the_console_cannot_encode(self):
        raw = io.BytesIO()
        console = io.TextIOWrapper(raw, encoding="gbk", errors="strict")
        with redirect_stdout(console):
            tools_batch.log("跟拍 \U0001F600 done")
        console.flush()
        self.assertIn("跟拍 ? done", raw.getvalue().decode("gbk"))


class AnalyzeTests(unittest.TestCase):
    def test_only_downloaded_but_unanalyzed_sources_are_analyzed(self):
        rows = [source(1, proxy_path="a.mp4"), source(2), source(3, proxy_path="c.mp4", analysis_completed=1)]
        runner, pipeline, _ = make_runner(rows)
        pipeline.analyze_source.return_value = [11, 12]
        totals, output = quietly(lambda: runner.analyze("T7.4"))
        pipeline.analyze_source.assert_called_once_with(1)
        self.assertEqual((totals["analyzed"], totals["candidates"], totals["failed"]), (1, 2, 0))
        self.assertIn("新增候选 2 个", output)


class RunTests(unittest.TestCase):
    def test_runs_the_three_steps_in_order_and_forwards_options(self):
        runner, _, _ = make_runner()
        order: list[str] = []
        runner.discover = Mock(side_effect=lambda *a, **k: order.append("discover") or {"found": 1})
        runner.proxy = Mock(side_effect=lambda *a, **k: order.append("proxy") or {"downloaded": 1})
        runner.analyze = Mock(side_effect=lambda *a, **k: order.append("analyze") or {"analyzed": 1})
        result = runner.run("T7.4", queries=2, limit=3, top=4, allow_unknown=True)
        self.assertEqual(order, ["discover", "proxy", "analyze"])
        self.assertEqual(list(result), ["discover", "proxy", "analyze"])
        runner.discover.assert_called_once_with("T7.4", queries=2, limit=3)
        runner.proxy.assert_called_once_with("T7.4", top=4, allow_unknown=True)
        runner.analyze.assert_called_once_with("T7.4")


class ArgumentTests(unittest.TestCase):
    def test_defaults_match_the_documented_batch_size(self):
        args = tools_batch.build_parser().parse_args(["run", "T7.4"])
        self.assertEqual((args.queries, args.limit, args.top, args.allow_unknown), (6, 10, 20, False))

    def test_unit_must_look_like_a_bucket_or_sub_unit(self):
        self.assertEqual(tools_batch.check_unit("T7.4"), "T7.4")
        self.assertEqual(tools_batch.check_unit("T7"), "T7")
        with self.assertRaises(SystemExit):
            tools_batch.check_unit("T7.4; DROP TABLE sources")

    def test_shipped_templates_cover_the_documented_example(self):
        from qt_tool.config import load_settings
        templates = tools_batch.load_templates(load_settings().search_templates_path)
        self.assertTrue(tools_batch.queries_for(templates, "T7.4"))
        self.assertTrue(tools_batch.queries_for(templates, "T7"))


if __name__ == "__main__":
    unittest.main()
