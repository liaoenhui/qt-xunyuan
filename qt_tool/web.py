from __future__ import annotations

import json
import mimetypes
import re
import traceback
from threading import Event, Lock
from concurrent.futures import ThreadPoolExecutor
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from .config import Settings, load_settings
from .db import Database
from .media import MediaPipeline, tool_status, DownloadCancelled
from .rules import RuleEngine


class App:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.db = Database(settings.db_path)
        self.db.fail_interrupted_jobs()
        self.rules = RuleEngine(settings.rules_path, settings.conflicts_path)
        self.pipeline = MediaPipeline(settings, self.db, self.rules)
        self.cancel_events: dict[int, Event] = {}
        self.job_lock = Lock()
        self.executors = {
            "proxy": ThreadPoolExecutor(max_workers=max(1, settings.max_download_concurrency), thread_name_prefix="qt-download"),
            "preflight": ThreadPoolExecutor(max_workers=max(1, settings.max_preflight_concurrency), thread_name_prefix="qt-preflight"),
            "analyze": ThreadPoolExecutor(max_workers=max(1, settings.max_analysis_concurrency), thread_name_prefix="qt-analysis"),
        }

    def queue_source_job(self, kind: str, source_id: int, allow_unknown: bool = False) -> tuple[int, bool]:
        source = self.db.get_source(source_id)
        if not source:
            raise KeyError("来源不存在")
        if kind in {"proxy", "preflight"}:
            self.pipeline.validate_proxy_source(source_id)
        if kind == "analyze" and not source.get("proxy_path"):
            raise ValueError("请先完成代理下载，再运行镜头分析")
        job_id, created = self.db.create_job(kind, source_id, {"allow_unknown": allow_unknown})
        if created:
            with self.job_lock:
                self.cancel_events[job_id] = Event()
            next_status = "ANALYZING" if kind == "analyze" else "PROXY_QUEUED" if kind == "proxy" else "PREFLIGHT_QUEUED"
            self.db.update_source(source_id, status=next_status, error=None)
            self.executors[kind].submit(self._run_source_job, job_id, kind, source_id)
        return job_id, created

    def _run_source_job(self, job_id: int, kind: str, source_id: int) -> None:
        with self.job_lock:
            if self.db.get_job(job_id)["status"] == "CANCELLED":
                self.cancel_events.pop(job_id, None)
                return
            self.db.start_job(job_id)
            event = self.cancel_events[job_id]
        state: dict[str, Any] = {}
        def progress(update):
            state.update(update)
            self.db.job_progress(job_id, state)
        try:
            if event.is_set():
                raise DownloadCancelled("任务已取消")
            if kind == "proxy":
                payload = json.loads(self.db.get_job(job_id).get("payload_json") or "{}")
                path = self.pipeline.download_proxy(source_id, cancel=event, progress=progress, allow_unknown=bool(payload.get("allow_unknown")))
                result = {"path": str(path), "message": "代理下载完成"}
            elif kind == "preflight":
                result = self.pipeline.preflight_source(source_id, cancel=event, progress=progress)
                result["message"] = "规格预检：" + result["reason"]
                self.db.update_source(source_id, status="METADATA_READY", error=None)
            else:
                candidate_ids = self.pipeline.analyze_source(source_id)
                result = self.analysis_summary(candidate_ids)
            self.db.finish_job(job_id, "DONE", result=result)
        except DownloadCancelled as exc:
            source = self.db.get_source(source_id) or {}
            self.db.update_source(source_id, status="PROXY_READY" if source.get("proxy_path") else "METADATA_READY", error=None)
            self.db.finish_job(job_id, "CANCELLED", result={"message": str(exc)})
        except Exception as exc:
            message = str(exc)
            self.db.update_source(source_id, status="ERROR", error=message[-4000:])
            self.db.finish_job(job_id, "FAILED", error=message[-4000:])
        finally:
            with self.job_lock:
                self.cancel_events.pop(job_id, None)

    def cancel_job(self, job_id: int) -> None:
        job = self.db.get_job(job_id)
        if not job or job["kind"] not in {"proxy", "preflight"}:
            raise ValueError("仅支持取消代理下载和规格预检")
        with self.job_lock:
            event = self.cancel_events.get(job_id)
            if event:
                event.set()
                if self.db.get_job(job_id)["status"] == "QUEUED":
                    self.db.finish_job(job_id, "CANCELLED", result={"message": "已取消排队"})
                    source = self.db.get_source(job["entity_id"]) or {}
                    self.db.update_source(job["entity_id"], status="PROXY_READY" if source.get("proxy_path") else "METADATA_READY", error=None)

    def analysis_summary(self, candidate_ids: list[int]) -> dict[str, Any]:
        waiting = rejected = 0
        reasons: dict[str, int] = {}
        for candidate_id in candidate_ids:
            candidate = self.db.get_candidate(candidate_id) or {}
            waiting += candidate.get("status") == "WAITING_REVIEW"
            if candidate.get("status") == "REJECTED":
                rejected += 1
                for rule in self.db.get_rule_results(candidate_id):
                    if rule["status"] == "FAIL" and rule["deterministic"]:
                        reason = f"{rule['rule_id']}：{rule['reason']}"
                        reasons[reason] = reasons.get(reason, 0) + 1
        message = f"镜头分析完成：生成 {len(candidate_ids)} 个，待人工审核 {waiting} 个，自动拒绝 {rejected} 个。"
        if waiting:
            message += "待审核片段请到人工审核查看（尚未通过交付审核）。"
        if rejected:
            message += "拒绝片段请到拒绝列表查看。原因：" + "；".join(f"{reason}（{count} 个）" for reason, count in reasons.items())
        if not candidate_ids:
            message += "本次未新增候选，可能没有满足最短时长的片段，或片段已存在。"
        return {"candidate_ids": candidate_ids, "waiting_count": waiting, "rejected_count": rejected,
                "rejection_reasons": reasons, "message": message}


class Handler(BaseHTTPRequestHandler):
    app: App
    server_version = "QTTool/0.1"

    def log_message(self, fmt: str, *args: Any) -> None:
        print(f"[{self.log_date_time_string()}] {fmt % args}")

    def _json(self, payload: Any, status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        if length > 2_000_000:
            raise ValueError("请求体过大")
        return json.loads(self.rfile.read(length).decode("utf-8")) if length else {}

    @staticmethod
    def _pagination(query: dict[str, list[str]], total: int, default_size: int = 20) -> tuple[int, int, int]:
        page_size = max(1, min(500, int(query.get("page_size", query.get("limit", [default_size]))[0])))
        page_count = max(1, (total + page_size - 1) // page_size)
        page = min(page_count, max(1, int(query.get("page", [1])[0])))
        return page, page_size, (page - 1) * page_size

    def _error(self, exc: Exception) -> None:
        traceback.print_exc()
        code = HTTPStatus.NOT_FOUND if isinstance(exc, (KeyError, FileNotFoundError)) else HTTPStatus.BAD_REQUEST
        self._json({"ok": False, "error": str(exc)}, int(code))

    def do_GET(self) -> None:
        try:
            parsed = urlparse(self.path)
            path = parsed.path
            if path in {"/", "/review"}:
                return self._file(self.app.settings.root / "web" / ("index.html" if path == "/" else "review.html"))
            if path.startswith("/static/"):
                return self._file(self.app.settings.root / "web" / path.removeprefix("/static/"))
            if path == "/api/dashboard":
                return self._json({"ok": True, "dashboard": self.app.db.dashboard(), "tools": tool_status(self.app.settings),
                                   "quota": self._quota(), "traffic_warning_bytes": int(self.app.settings.daily_traffic_warning_gb * 1024**3)})
            if path == "/api/sources":
                q = parse_qs(parsed.query)
                view = q.get("view", ["all"])[0]
                bucket = q.get("bucket", [None])[0]
                maximum = self.app.settings.source_max_duration_seconds
                total = self.app.db.count_sources(view, bucket, maximum)
                page, page_size, offset = self._pagination(q, total)
                return self._json({"ok": True,
                                   "items": self.app.db.list_sources(page_size, view, offset, bucket, maximum),
                                   "total": total, "page": page, "page_size": page_size,
                                   "bucket": bucket, "max_duration_seconds": maximum})
            if match := re.fullmatch(r"/api/jobs/(\d+)", path):
                job = self.app.db.get_job(int(match.group(1)))
                if not job:
                    raise KeyError("任务不存在")
                try:
                    job["result"] = json.loads(job.get("result_json") or "{}")
                except json.JSONDecodeError:
                    job["result"] = {}
                return self._json({"ok": True, "job": job})
            if path == "/api/candidates":
                q = parse_qs(parsed.query)
                status = q.get("status", [None])[0]
                bucket = q.get("bucket", [None])[0]
                camera = q.get("camera", [None])[0]
                unit = q.get("unit", [None])[0]
                total = self.app.db.count_candidates(status, bucket, camera, unit)
                page, page_size, offset = self._pagination(q, total, 100)
                return self._json({"ok": True,
                                   "items": self.app.db.list_candidates(status, page_size, offset, bucket, camera, unit),
                                   "total": total, "page": page, "page_size": page_size, "bucket": bucket,
                                   "camera": camera, "unit": unit})
            if path == "/api/final-candidates":
                q = parse_qs(parsed.query)
                state = q.get("state", ["pending"])[0]
                total = self.app.db.count_final_candidates(state)
                page, page_size, offset = self._pagination(q, total)
                return self._json({"ok": True, "items": self.app.db.list_final_candidates(state, page_size, offset),
                                   "total": total, "page": page, "page_size": page_size})
            if match := re.fullmatch(r"/api/candidates/(\d+)", path):
                candidate_id = int(match.group(1))
                item = self.app.db.get_candidate(candidate_id)
                if not item:
                    raise KeyError("候选不存在")
                item["facts"] = json.loads(item.get("facts_json") or "{}")
                item["rules"] = self.app.db.get_rule_results(candidate_id)
                item["unit_rule"] = self.app.rules.units.get(item.get("candidate_unit"))
                return self._json({"ok": True, "item": item})
            if path == "/api/rules":
                return self._json({"ok": True, "redlines": self.app.rules.redlines, "buckets": self.app.rules.buckets,
                                   "units": self.app.rules.units, "conflicts": self.app.rules.conflicts})
            if match := re.fullmatch(r"/media/candidate/(\d+)", path):
                item = self.app.db.get_candidate(int(match.group(1)))
                if not item or not item.get("proxy_path"):
                    raise FileNotFoundError("候选代理不存在")
                return self._media(Path(item["proxy_path"]))
            self.send_error(404)
        except Exception as exc:
            self._error(exc)

    def do_POST(self) -> None:
        try:
            path = urlparse(self.path).path
            data = self._read_json()
            if match := re.fullmatch(r"/api/jobs/(\d+)/cancel", path):
                self.app.cancel_job(int(match.group(1)))
                return self._json({"ok": True})
            if match := re.fullmatch(r"/api/sources/(\d+)/preflight", path):
                job_id, created = self.app.queue_source_job("preflight", int(match.group(1)))
                return self._json({"ok": True, "job_id": job_id, "created": created}, 202)
            if path == "/api/sources/import":
                source_id, created = self.app.pipeline.import_url(str(data["url"]).strip(), data.get("target_unit"))
                return self._json({"ok": True, "source_id": source_id, "created": created})
            if path == "/api/sources/discover":
                result = self.app.pipeline.discover(str(data["query"]).strip(), data.get("target_unit"), int(data.get("limit", 10)))
                return self._json({"ok": True, **result})
            if match := re.fullmatch(r"/api/sources/(\d+)/proxy", path):
                job_id, created = self.app.queue_source_job("proxy", int(match.group(1)), data.get("allow_unknown") is True)
                return self._json({"ok": True, "job_id": job_id, "created": created, "status": "QUEUED"}, 202)
            if match := re.fullmatch(r"/api/sources/(\d+)/analyze", path):
                job_id, created = self.app.queue_source_job("analyze", int(match.group(1)))
                return self._json({"ok": True, "job_id": job_id, "created": created, "status": "QUEUED"}, 202)
            if match := re.fullmatch(r"/api/sources/(\d+)/deleted", path):
                if not isinstance(data.get("deleted"), bool):
                    raise ValueError("deleted 必须是布尔值")
                self.app.db.set_source_deleted(int(match.group(1)), data["deleted"])
                return self._json({"ok": True})
            if match := re.fullmatch(r"/api/sources/(\d+)/reject-waiting", path):
                source_id = int(match.group(1))
                if not self.app.db.get_source(source_id):
                    raise KeyError("来源不存在")
                ids = self.app.db.reject_waiting_by_source(source_id, str(data.get("notes") or "").strip())
                return self._json({"ok": True, "rejected_ids": ids, "count": len(ids)})
            if match := re.fullmatch(r"/api/sources/(\d+)/analysis-state", path):
                source_id = int(match.group(1))
                if not self.app.db.get_source(source_id):
                    raise KeyError("来源不存在")
                completed = bool(data.get("completed"))
                self.app.db.update_source(source_id, analysis_completed=int(completed))
                return self._json({"ok": True, "analysis_completed": completed})
            if match := re.fullmatch(r"/api/candidates/(\d+)/review", path):
                candidate_id = int(match.group(1))
                if data.get("decision", "").upper() == "ACCEPT":
                    failures = [r for r in self.app.db.get_rule_results(candidate_id) if r["status"] == "FAIL" and r["deterministic"]]
                    if failures and not data.get("confirm_hard_fail"):
                        return self._json({"ok": False, "requires_confirmation": True, "hard_failures": failures}, 409)
                    if failures and data.get("confirm_hard_fail"):
                        data["manual_rule_overrides"] = {r["rule_id"]: r["reason"] for r in failures}
                review_id = self.app.db.review(candidate_id, data)
                return self._json({"ok": True, "review_id": review_id})
            if match := re.fullmatch(r"/api/candidates/(\d+)/camera-check", path):
                result = self.app.pipeline.check_candidate_camera(int(match.group(1)))
                return self._json({"ok": True, "camera_motion": result})
            if match := re.fullmatch(r"/api/candidates/(\d+)/operator-check", path):
                result = self.app.pipeline.check_candidate_operator(int(match.group(1)))
                return self._json({"ok": True, "operator_framing": result})
            if match := re.fullmatch(r"/api/candidates/(\d+)/trim", path):
                candidate_id = int(match.group(1))
                result = self.app.pipeline.trim_candidate(
                    candidate_id, float(data["start_time"]), float(data["end_time"])
                )
                return self._json({"ok": True, **result})
            if match := re.fullmatch(r"/api/candidates/(\d+)/finalize", path):
                candidate_id = int(match.group(1))
                result = self.app.pipeline.final_qa_and_deliver(candidate_id)
                return self._json({"ok": True, **result})
            if match := re.fullmatch(r"/api/candidates/(\d+)/export-state", path):
                candidate_id = int(match.group(1))
                if data.get("processed") is not False:
                    raise ValueError("目前只支持将已处理候选移回最终处理列表")
                self.app.db.restore_exported_candidate(candidate_id)
                return self._json({"ok": True, "processed": False})
            if path == "/api/export":
                output = self.app.pipeline.export_delivery_csv()
                return self._json({"ok": True, "path": str(output), "rows": len(self.app.db.delivery_rows())})
            self.send_error(404)
        except Exception as exc:
            self._error(exc)

    def _quota(self) -> list[dict[str, Any]]:
        actual = {(r["bucket"], r["duration_bucket"]): r["count"] for r in self.app.db.quota_state()}
        result = []
        for bucket, cfg in self.app.rules.buckets.items():
            total = cfg["target_total"]
            row = {"bucket": bucket, "name": cfg["name"], "target": total,
                   "total": {"actual": sum(v for (b, _), v in actual.items() if b == bucket), "target": total},
                   "duration": {d: {"actual": sum(v for (b, db), v in actual.items() if b == bucket and db == d),
                                     "recommended": round(total * ratio), "minimum": round(total * .2)}
                                for d, ratio in {"short": .4, "medium": .35, "long": .25}.items()}}
            gaps = [(row["duration"][d]["recommended"] - row["duration"][d]["actual"], {"short": "短档", "medium": "中档", "long": "长档"}[d])
                    for d in ("short", "medium", "long")]
            row["largest_gap"] = "已达标" if row["total"]["actual"] >= total else max(gaps)[1]
            result.append(row)
        return result

    def _file(self, path: Path) -> None:
        root = (self.app.settings.root / "web").resolve()
        path = path.resolve()
        if root != path.parent and root not in path.parents:
            self.send_error(403)
            return
        if not path.is_file():
            self.send_error(404)
            return
        body = path.read_bytes()
        content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        self.send_response(200)
        self.send_header("Content-Type", f"{content_type}; charset=utf-8" if content_type.startswith("text/") else content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _media(self, path: Path) -> None:
        path = path.resolve()
        proxy_root = (self.app.settings.data_dir / "proxy").resolve()
        if proxy_root not in path.parents or not path.is_file():
            raise FileNotFoundError("非法或不存在的代理路径")
        size = path.stat().st_size
        start, end = 0, size - 1
        status = 200
        if range_header := self.headers.get("Range"):
            match = re.match(r"bytes=(\d*)-(\d*)", range_header)
            if match:
                start = int(match.group(1) or 0)
                end = min(int(match.group(2) or size - 1), size - 1)
                status = 206
        length = max(0, end - start + 1)
        self.send_response(status)
        self.send_header("Content-Type", mimetypes.guess_type(path.name)[0] or "video/mp4")
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(length))
        if status == 206:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()
        with path.open("rb") as stream:
            stream.seek(start)
            remaining = length
            while remaining:
                chunk = stream.read(min(1024 * 1024, remaining))
                if not chunk:
                    break
                self.wfile.write(chunk)
                remaining -= len(chunk)


def run() -> None:
    settings = load_settings()
    app = App(settings)
    handler = type("QTHandler", (Handler,), {"app": app})
    server = ThreadingHTTPServer((settings.host, settings.port), handler)
    print(f"QT 视频数据生产工具已启动：http://{settings.host}:{settings.port}")
    print(f"数据库：{settings.db_path}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        for executor in app.executors.values():
            executor.shutdown(wait=False, cancel_futures=True)
        server.server_close()
