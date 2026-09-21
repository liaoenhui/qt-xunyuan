"""无人值守批量找源命令行：搜索模板 → 元数据 → 代理下载 → 镜头分析。

用法（在仓库根目录运行）：
    python tools_batch.py discover T7.4 [--queries 6] [--limit 10]
    python tools_batch.py proxy    T7.4 [--top 20] [--allow-unknown]
    python tools_batch.py analyze  T7.4
    python tools_batch.py run      T7.4 [--queries 6] [--limit 10] [--top 20]
    python tools_batch.py status   [T7.4]
    python tools_batch.py tools

单元可以写完整子单元（T7.4）或整桶（T7）。产出全部写入同一个 `data/qt_tool.sqlite3`，
人工审核仍然在网页 http://127.0.0.1:8765/review 完成；网页和命令行可以同时运行。
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path
from typing import Any

from qt_tool.config import load_settings
from qt_tool.db import Database
from qt_tool.media import MediaPipeline, tool_status
from qt_tool.rules import RuleEngine


PAGE_SIZE = 200
UNIT_PATTERN = re.compile(r"T[1-9](\.\d+)?")
# discover() 返回的配置回显，不是计数，不参与汇总累加
NON_COUNT_KEYS = {"max_duration_seconds"}


def log(message: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {message}", flush=True)


def one_line(value: Any, limit: int = 200) -> str:
    text = " ".join(str(value).split())
    return text[:limit] if text else value.__class__.__name__


def load_templates(path: Path) -> dict[str, list[str]]:
    """搜索模板使用 JSON 语法（YAML 1.2 的合法子集），与规则文件一致。"""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    return {unit: list((item or {}).get("positive") or [])
            for unit, item in (data.get("templates") or {}).items()}


def check_unit(unit: str) -> str:
    if not UNIT_PATTERN.fullmatch(unit or ""):
        raise SystemExit(f"单元必须形如 T7 或 T7.4，收到：{unit!r}")
    return unit


def queries_for(templates: dict[str, list[str]], unit: str) -> list[str]:
    """子单元取自己的搜索词；整桶按子单元顺序依次取词。"""
    if unit in templates:
        return list(templates[unit])
    prefix = f"{unit}."
    words: list[str] = []
    for key in sorted(k for k in templates if k.startswith(prefix)):
        words.extend(templates[key])
    return words


def unit_matches(target_unit: Any, unit: str) -> bool:
    target = str(target_unit or "")
    if "." in unit:
        return target == unit
    return target == unit or target.startswith(f"{unit}.")


def _scope(column: str, unit: str | None) -> tuple[str, list[Any]]:
    if not unit:
        return "", []
    if "." in unit:
        return f"{column}=?", [unit]
    return f"({column}=? OR {column} LIKE ?)", [unit, f"{unit}.%"]


class BatchRunner:
    def __init__(self, pipeline: MediaPipeline, db: Database, templates: dict[str, list[str]]):
        self.pipeline = pipeline
        self.db = db
        self.templates = templates

    # ---------- 子命令 ----------

    def discover(self, unit: str, queries: int = 6, limit: int = 10) -> dict[str, int]:
        words = queries_for(self.templates, unit)[:max(1, queries)]
        if not words:
            raise SystemExit(f"rules/search_templates.yaml 里没有 {unit} 的搜索词")
        totals: dict[str, int] = {"queries": 0, "failed_queries": 0}
        log(f"discover {unit}：使用 {len(words)} 条搜索词，每条最多 {limit} 个结果")
        for query in words:
            log(f"搜索：{query}")
            try:
                stats = self.pipeline.discover(query, unit, limit)
            except Exception as exc:  # 单条搜索词失败不应中断整批
                totals["failed_queries"] += 1
                log(f"  搜索失败：{one_line(exc)}")
                continue
            totals["queries"] += 1
            for key, value in (stats or {}).items():
                if key in NON_COUNT_KEYS or isinstance(value, bool) or not isinstance(value, (int, float)):
                    continue
                totals[key] = totals.get(key, 0) + int(value)
            log("  " + "，".join(f"{key} {value}" for key, value in (stats or {}).items()))
        log(f"discover 汇总 {unit}：{json.dumps(totals, ensure_ascii=False)}")
        return totals

    def proxy(self, unit: str, top: int = 20, allow_unknown: bool = False) -> dict[str, int]:
        pending = [s for s in self.sources(unit) if not s.get("proxy_path")][:max(0, top)]
        totals = {"selected": len(pending), "downloaded": 0, "skipped_preflight_fail": 0,
                  "skipped_preflight_unknown": 0, "failed": 0}
        log(f"proxy {unit}：未下载代理的来源按分数取前 {top}，本次处理 {len(pending)} 条")
        for source in pending:
            source_id = int(source["id"])
            log(f"#{source_id} {str(source.get('title') or '')[:60]}")
            try:
                verdict = self.pipeline.preflight_source(source_id, refresh=False)
            except ValueError as exc:  # 直播、超时长等硬性拒绝
                totals["skipped_preflight_fail"] += 1
                log(f"  跳过（不可下载）：{one_line(exc)}")
                continue
            except Exception as exc:
                totals["failed"] += 1
                log(f"  规格预检失败：{one_line(exc)}")
                continue
            status = str((verdict or {}).get("status") or "UNKNOWN")
            reason = str((verdict or {}).get("reason") or "")
            if status == "FAIL":
                totals["skipped_preflight_fail"] += 1
                log(f"  跳过（预检 FAIL）：{reason}")
                continue
            if status == "UNKNOWN" and not allow_unknown:
                totals["skipped_preflight_unknown"] += 1
                log(f"  跳过（预检 UNKNOWN，可加 --allow-unknown 继续）：{reason}")
                continue
            try:
                path = self.pipeline.download_proxy(source_id, allow_unknown=allow_unknown)
            except ValueError as exc:  # 预检结论在两次调用之间发生变化
                key = "skipped_preflight_unknown" if status == "UNKNOWN" else "skipped_preflight_fail"
                totals[key] += 1
                log(f"  跳过（预检未通过）：{one_line(exc)}")
            except Exception as exc:
                totals["failed"] += 1
                log(f"  代理下载失败：{one_line(exc)}")
            else:
                totals["downloaded"] += 1
                log(f"  代理完成：{Path(path).name}")
        log(f"proxy 汇总 {unit}：{json.dumps(totals, ensure_ascii=False)}")
        return totals

    def analyze(self, unit: str) -> dict[str, int]:
        pending = [s for s in self.sources(unit)
                   if s.get("proxy_path") and not int(s.get("analysis_completed") or 0)]
        totals = {"selected": len(pending), "analyzed": 0, "candidates": 0, "failed": 0}
        log(f"analyze {unit}：已有代理且未分析的来源 {len(pending)} 条")
        for source in pending:
            source_id = int(source["id"])
            log(f"#{source_id} {str(source.get('title') or '')[:60]}")
            started = time.time()
            try:
                candidate_ids = self.pipeline.analyze_source(source_id)
            except Exception as exc:
                totals["failed"] += 1
                log(f"  镜头分析失败：{one_line(exc)}")
                continue
            totals["analyzed"] += 1
            totals["candidates"] += len(candidate_ids)
            log(f"  新增候选 {len(candidate_ids)} 个（{time.time() - started:.0f}s）")
        log(f"analyze 汇总 {unit}：{json.dumps(totals, ensure_ascii=False)}")
        return totals

    def run(self, unit: str, queries: int = 6, limit: int = 10,
            top: int = 20, allow_unknown: bool = False) -> dict[str, dict[str, int]]:
        return {
            "discover": self.discover(unit, queries=queries, limit=limit),
            "proxy": self.proxy(unit, top=top, allow_unknown=allow_unknown),
            "analyze": self.analyze(unit),
        }

    def status(self, unit: str | None = None) -> dict[str, Any]:
        source_where, source_args = _scope("s.target_unit", unit)
        candidate_where, candidate_args = _scope("c.candidate_unit", unit)
        if unit and "." not in unit:
            candidate_where = f"(c.candidate_bucket=? OR {candidate_where})"
            candidate_args = [unit] + candidate_args
        with self.db.connect() as con:
            conditions = " AND ".join(x for x in ("s.deleted_at IS NULL", source_where) if x)
            sources = {row["status"]: int(row["n"]) for row in con.execute(
                f"SELECT s.status status,COUNT(*) n FROM sources s WHERE {conditions} "
                "GROUP BY s.status ORDER BY n DESC", source_args)}
            candidates = {row["status"]: int(row["n"]) for row in con.execute(
                "SELECT c.status status,COUNT(*) n FROM candidate_shots c "
                + (f"WHERE {candidate_where} " if candidate_where else "")
                + "GROUP BY c.status ORDER BY n DESC", candidate_args)}
            reasons = [dict(row) for row in con.execute(
                """SELECT r.rule_id rule_id,COUNT(*) n,MAX(r.reason) sample
                   FROM rule_results r JOIN candidate_shots c ON c.id=r.candidate_id
                   WHERE r.stage='candidate' AND r.status='FAIL' AND r.deterministic=1
                     AND c.status='REJECTED' """
                + (f"AND {candidate_where} " if candidate_where else "")
                + "GROUP BY r.rule_id ORDER BY n DESC LIMIT 10", candidate_args)]
        log(f"status {unit or '全部单元'}")
        log(f"  来源状态：{json.dumps(sources, ensure_ascii=False)}")
        log(f"  候选状态：{json.dumps(candidates, ensure_ascii=False)}")
        if not reasons:
            log("  自动淘汰原因：无")
        for row in reasons:
            log(f"  自动淘汰 {row['rule_id']}：{row['n']} 个，例如 {one_line(row['sample'], 80)}")
        return {"sources": sources, "candidates": candidates, "reject_reasons": reasons}

    def tools(self) -> dict[str, bool]:
        status = tool_status(self.pipeline.settings)
        print(json.dumps(status, ensure_ascii=False, indent=2))
        return status

    # ---------- 内部 ----------

    def sources(self, unit: str) -> list[dict[str, Any]]:
        """该单元下未删除的来源，沿用 db.list_sources 的 source_score 降序。"""
        bucket = unit.split(".", 1)[0]
        items: list[dict[str, Any]] = []
        offset = 0
        while True:
            rows = self.db.list_sources(limit=PAGE_SIZE, view="all", offset=offset, bucket=bucket)
            if not rows:
                break
            items.extend(row for row in rows if unit_matches(row.get("target_unit"), unit))
            if len(rows) < PAGE_SIZE:
                break
            offset += PAGE_SIZE
        return items


def build_runner() -> BatchRunner:
    settings = load_settings()
    db = Database(settings.db_path)
    # 故意不调用 db.fail_interrupted_jobs()：那会把网页正在执行的后台任务误判为失败。
    rules = RuleEngine(settings.rules_path, settings.conflicts_path)
    pipeline = MediaPipeline(settings, db, rules)
    return BatchRunner(pipeline, db, load_templates(settings.search_templates_path))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="tools_batch.py", description="QT 无人值守批量找源",
                                     formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    discover = sub.add_parser("discover", help="按搜索模板批量拉取来源元数据")
    discover.add_argument("unit")
    discover.add_argument("--queries", type=int, default=6, help="使用该单元的前 N 条搜索词，默认 6")
    discover.add_argument("--limit", type=int, default=10, help="每条搜索词最多取回多少结果，默认 10")

    proxy = sub.add_parser("proxy", help="按分数批量下载代理")
    proxy.add_argument("unit")
    proxy.add_argument("--top", type=int, default=20, help="按 source_score 降序取前 N 条，默认 20")
    proxy.add_argument("--allow-unknown", action="store_true", help="规格预检为 UNKNOWN 时仍然下载")

    analyze = sub.add_parser("analyze", help="对已有代理且未分析的来源做镜头分析")
    analyze.add_argument("unit")

    run = sub.add_parser("run", help="discover → proxy → analyze 连跑")
    run.add_argument("unit")
    run.add_argument("--queries", type=int, default=6)
    run.add_argument("--limit", type=int, default=10)
    run.add_argument("--top", type=int, default=20)
    run.add_argument("--allow-unknown", action="store_true")

    status = sub.add_parser("status", help="来源/候选状态与自动淘汰原因汇总")
    status.add_argument("unit", nargs="?")

    sub.add_parser("tools", help="打印外部工具可用性")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    runner = build_runner()
    if args.command == "tools":
        runner.tools()
    elif args.command == "status":
        runner.status(check_unit(args.unit) if args.unit else None)
    elif args.command == "discover":
        runner.discover(check_unit(args.unit), queries=args.queries, limit=args.limit)
    elif args.command == "proxy":
        runner.proxy(check_unit(args.unit), top=args.top, allow_unknown=args.allow_unknown)
    elif args.command == "analyze":
        runner.analyze(check_unit(args.unit))
    else:
        runner.run(check_unit(args.unit), queries=args.queries, limit=args.limit,
                   top=args.top, allow_unknown=args.allow_unknown)
    return 0


if __name__ == "__main__":
    sys.exit(main())
