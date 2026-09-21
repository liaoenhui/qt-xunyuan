from __future__ import annotations

import hashlib
import csv
import json
import math
import os
import re
import shutil
import subprocess
import tempfile
import time
import unicodedata
import uuid
from threading import Lock, BoundedSemaphore
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .config import Settings
from .db import Database
from .rules import MINIMUM_LIVE_ACTION_FPS, RuleEngine, RuleStatus
from .subject import SubjectContinuityAnalyzer


class ToolMissing(RuntimeError):
    pass


class DownloadStalled(TimeoutError):
    pass


class DownloadCancelled(RuntimeError):
    pass


def format_preflight(formats: list[dict[str, Any]], bucket: str | None) -> dict[str, str]:
    if not bucket:
        return {"status": "UNKNOWN", "reason": "未选择目标单元，无法确认最低规格"}
    width, height = (1920, 1080) if bucket == "T9" else (2560, 1440)
    videos = [f for f in formats if f.get("vcodec") not in (None, "none")]
    def fits(f):
        return (f.get("width") or 0) >= width and (f.get("height") or 0) >= height and (bucket == "T9" or (f.get("fps") or 0) >= MINIMUM_LIVE_ACTION_FPS)
    if any(fits(f) for f in videos):
        return {"status": "PASS", "reason": "存在符合分辨率和帧率要求的格式；清晰度、原生画质仍需审核"}
    if not videos or any(not f.get("width") or not f.get("height") or (bucket != "T9" and not f.get("fps")) for f in videos):
        return {"status": "UNKNOWN", "reason": "格式信息不完整，暂不能确定规格"}
    return {"status": "FAIL", "reason": f"当前可用格式均不满足 {width}×{height}" + ("、≥24fps" if bucket != "T9" else "")}


def _command_exists(command: str) -> bool:
    path = Path(command)
    return path.exists() if path.parent != Path(".") else shutil.which(command) is not None


def _run(args: list[str], timeout: int = 3600) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, text=True, encoding="utf-8", errors="replace", capture_output=True, timeout=timeout, check=False)


def _terminate_process_tree(proc: subprocess.Popen[Any]) -> None:
    if proc.poll() is not None:
        return
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                       capture_output=True, text=True, check=False)
    else:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=5)


def _run_download(args: list[str], progress_dir: Path, progress_pattern: str,
                  stall_timeout: int, timeout: int, cancel=None, progress=None) -> subprocess.CompletedProcess[str]:
    """Run yt-dlp while aborting a download whose output files stop changing."""
    def marker() -> tuple[int, int]:
        files = [path for path in progress_dir.glob(progress_pattern) if path.is_file()]
        return (sum(path.stat().st_size for path in files),
                max((path.stat().st_mtime_ns for path in files), default=0))

    if cancel and cancel.is_set():
        raise DownloadCancelled("已取消下载")
    with tempfile.TemporaryFile() as log:
        proc = subprocess.Popen(args, stdout=log, stderr=subprocess.STDOUT)
        started = last_progress = time.monotonic()
        previous = marker()
        try:
            while proc.poll() is None:
                time.sleep(1)
                current = marker()
                now_mono = time.monotonic()
                if cancel and cancel.is_set():
                    raise DownloadCancelled("已取消下载；已下载的部分保留供重试")
                if progress:
                    progress({"bytes": current[0], "speed": max(0, current[0] - previous[0])})
                if current != previous:
                    previous = current
                    last_progress = now_mono
                if now_mono - started >= timeout:
                    _terminate_process_tree(proc)
                    raise subprocess.TimeoutExpired(args, timeout)
                if now_mono - last_progress >= stall_timeout:
                    _terminate_process_tree(proc)
                    raise DownloadStalled(f"连续 {stall_timeout} 秒没有下载进度")
        finally:
            if proc.poll() is None:
                _terminate_process_tree(proc)
        log.seek(0)
        output = log.read().decode("utf-8", errors="replace")
        return subprocess.CompletedProcess(args, int(proc.returncode or 0), stdout="", stderr=output)


def ytdlp_error_message(stderr: str, action: str) -> str:
    """Translate common yt-dlp failures into short operator-facing guidance."""
    raw = str(stderr or "").strip()
    lowered = raw.lower()
    if "could not find" in lowered and "cookies database" in lowered:
        return "无法读取 Firefox 登录信息。请用 Firefox 登录 YouTube、完全退出 Firefox 后重试；不要提供账号密码。"
    if "sign in to confirm" in lowered or "not a bot" in lowered:
        return "YouTube 要求登录确认。请确认 Firefox 已登录 YouTube 并完全退出浏览器，然后重试。"
    network_markers = (
        "failed to establish a new connection", "unable to download api page",
        "network is unreachable", "name resolution", "winerror 10013",
        "winerror 10060", "winerror 10061",
    )
    if any(marker in lowered for marker in network_markers):
        return f"{action}失败：无法连接 YouTube。请检查网络或代理设置后重试。"
    meaningful = [line.strip() for line in raw.splitlines() if line.strip().startswith("ERROR:")]
    detail = meaningful[-1] if meaningful else raw
    if not detail or detail.count("�") >= 3:
        return f"{action}失败，请检查网络后重试；若仍失败，请查看工作台服务日志。"
    return detail[-800:]


def source_duration_allowed(duration: Any, maximum_seconds: int) -> bool:
    """Keep unknown durations, but reject known sources beyond the resource cap."""
    if duration in (None, ""):
        return True
    try:
        value = float(duration)
    except (TypeError, ValueError):
        return True
    return value <= float(maximum_seconds)


def source_live_reason(metadata: dict[str, Any]) -> str | None:
    """Return a short reason when metadata identifies a live or endless source."""
    live_status = str(metadata.get("live_status") or "").lower()
    if metadata.get("is_live") is True or live_status == "is_live":
        return "正在直播"
    if live_status == "is_upcoming" or metadata.get("is_upcoming") is True:
        return "尚未结束的首播/直播"
    duration = metadata.get("duration")
    if duration not in (None, ""):
        return None
    if metadata.get("concurrent_view_count") not in (None, 0, ""):
        return "无固定时长的直播"
    title = str(metadata.get("title") or "")
    if re.search(r"(?i)(?:\b24\s*/\s*7\b|\blive\s*stream\b|\blivestream\b|\blive\s+24\b|\bendless\s+loop\b)", title):
        return "疑似直播或无限循环视频"
    return None


# CONFLICT-003：PDF 的时长档位在 15.0s 和 30.0s 上重叠，切片必须主动避开精确边界，
# 否则 RuleEngine.duration_bucket 会把候选判成 CONFLICT 并挡在自动交付之外。
DURATION_BOUNDARIES = (15.0, 30.0)
DURATION_BOUNDARY_TOLERANCE = 0.05
DURATION_BOUNDARY_NUDGE = 0.1
_SPLIT_EPSILON = 1e-9


def _on_duration_boundary(seconds: float) -> bool:
    return any(abs(seconds - boundary) <= DURATION_BOUNDARY_TOLERANCE for boundary in DURATION_BOUNDARIES)


def _equal_part_count(length: float, target: float, minimum: float, maximum: float) -> int:
    """满足每段落在 [minimum, maximum] 的等分段数，优先贴近 target。"""
    fewest = max(1, math.ceil(length / maximum - _SPLIT_EPSILON))
    # 段数上限就是“最后一段不足 minimum 时并入前一段”：少切一刀等于把尾巴摊给其余各段。
    most = max(fewest, int(length / minimum + _SPLIT_EPSILON))
    wanted = min(max(math.ceil(length / target - _SPLIT_EPSILON), fewest), most)
    # 等分后各段等长，一旦整体压在档位边界上就换一个段数重新均分。
    for count in sorted(range(fewest, most + 1), key=lambda c: (abs(c - wanted), c)):
        if not _on_duration_boundary(length / count):
            return count
    return wanted


def split_long_segment(start: float, end: float, target: float = 45.0,
                       minimum: float = 30.0, maximum: float = 60.0) -> list[tuple[float, float]]:
    """把超过 maximum 的单镜头段等分成若干 minimum–maximum 的候选。

    超过 60s 的段仍然只计入长档，多出来的时长拿不到任何额度，所以在建候选前就按
    target 等分；不超过 maximum 的段原样返回，边界仍由人工裁剪面板决定。
    """
    length = end - start
    if length <= maximum + _SPLIT_EPSILON:
        return [(round(start, 3), round(end, 3))]
    count = _equal_part_count(length, target, minimum, maximum)
    step = length / count
    cuts = [start + index * step for index in range(count)] + [end]
    # 段首段尾必须贴合原镜头，只有内部切点可以微调 0.1s 躲开 15.0/30.0 边界。
    for _ in range(count):
        for index in range(1, count):
            while _on_duration_boundary(cuts[index] - cuts[index - 1]):
                cuts[index] -= DURATION_BOUNDARY_NUDGE
        if not _on_duration_boundary(cuts[-1] - cuts[-2]):
            break
        cuts[-2] -= DURATION_BOUNDARY_NUDGE
    return [(round(a, 3), round(b, 3)) for a, b in zip(cuts, cuts[1:])]


def merge_facts(base_json: str, probe: dict[str, Any], **overrides: Any) -> dict[str, Any]:
    facts = dict(json.loads(base_json or "{}"))
    facts.update(probe)
    facts.update(overrides)
    return facts


def delivery_description(title: str, limit: int = 60) -> str:
    """Return a Windows-safe short description while preserving Chinese text."""
    value = unicodedata.normalize("NFKC", str(title or ""))
    value = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", value)
    value = re.sub(r"\s+", "_", value)
    value = re.sub(r"_+", "_", value).strip(" ._")
    return (value[:limit].rstrip(" ._") or "视频片段")


def tool_status(settings: Settings) -> dict[str, bool]:
    subject = SubjectContinuityAnalyzer(settings.root / "tools" / "models")
    return {
        "ffmpeg": _command_exists(settings.ffmpeg_bin),
        "ffprobe": _command_exists(settings.ffprobe_bin),
        "yt_dlp": _command_exists(settings.ytdlp_bin),
        "youtube_js": bool(settings.ytdlp_js_runtime),
        "youtube_auth": bool(settings.ytdlp_cookies_from_browser),
        "subject_ai": subject.available,
    }


def source_score(metadata: dict[str, Any], target_unit: str | None, query: str, gap_boost: float = 0) -> float:
    title = str(metadata.get("title", "")).lower()
    description = str(metadata.get("description", "")).lower()
    text = f"{title} {description}"
    positive = [w for w in re.split(r"\W+", query.lower()) if len(w) > 2]
    relevance = min(35.0, sum(6.0 for word in positive if word in text))
    height = int(metadata.get("height") or 0)
    quality = 20.0 if height >= 2160 else 14.0 if height >= 1440 else 7.0 if height >= 1080 else 0.0
    duration = float(metadata.get("duration") or 0)
    duration_score = 15.0 if duration >= 30 else 10.0 if duration >= 15 else 4.0 if duration >= 5 else -40.0
    negatives = ("montage", "compilation", "gameplay", "shorts", "reaction")
    penalty = sum(18.0 for word in negatives if word in text)
    unit_bonus = 8.0 if target_unit else 0.0
    return max(0.0, min(100.0, 20 + relevance + quality + duration_score + unit_bonus + gap_boost - penalty))


class MediaPipeline:
    def __init__(self, settings: Settings, db: Database, rules: RuleEngine):
        self.settings = settings
        self.db = db
        self.rules = rules
        self._final_lock_guard = Lock()
        self._final_source_locks: dict[int, Any] = {}
        self._final_candidate_locks: dict[int, Any] = {}
        self._verified_originals: dict[str, tuple[int, int]] = {}
        self._final_download_slots = BoundedSemaphore(max(1, getattr(settings, "max_download_concurrency", 2)))
        root = getattr(settings, "root", settings.data_dir.parent)
        self.subject_analyzer = SubjectContinuityAnalyzer(Path(root) / "tools" / "models")
        if hasattr(self.db, "delivery_filename_rows"):
            self.repair_delivery_filenames()

    def repair_delivery_filenames(self) -> int:
        """Rename legacy deliverables and update their persisted standard names."""
        root = (self.settings.data_dir / "deliverable").resolve()
        renamed = 0
        for row in self.db.delivery_filename_rows():
            unit = str(row.get("delivery_unit") or row.get("unit") or "UNSET")
            sequence = int(row.get("delivery_sequence") or 0)
            if sequence <= 0:
                continue
            stored_name = str(row.get("delivery_filename") or "")
            compliant = re.fullmatch(rf"{re.escape(unit)}_{sequence:03d}_.+\.mp4", stored_name, re.IGNORECASE)
            filename = stored_name if compliant else f"{unit}_{sequence:03d}_{delivery_description(row.get('source_title') or '')}.mp4"
            old_path = Path(str(row.get("final_path") or ""))
            final_path = old_path
            if old_path.is_file():
                resolved = old_path.resolve()
                if root == resolved.parent or root in resolved.parents:
                    target = old_path.with_name(filename)
                    if target != old_path:
                        if target.exists():
                            raise FileExistsError(f"交付文件名冲突：{target}")
                        shutil.copy2(old_path, target)
                        if target.stat().st_size != old_path.stat().st_size:
                            target.unlink(missing_ok=True)
                            raise OSError(f"交付文件重命名校验失败：{old_path}")
                        old_path.unlink()
                        final_path = target
                        renamed += 1
            self.db.update_final_clip_delivery(int(row["candidate_id"]), str(final_path), filename,
                                               str(row.get("deliverable_status") or "PENDING"))
        return renamed

    def _ytdlp(self, *args: str, use_cookies: bool = False) -> list[str]:
        command = [self.settings.ytdlp_bin, "--encoding", "utf-8"]
        if self.settings.ytdlp_js_runtime:
            command.extend(("--js-runtimes", self.settings.ytdlp_js_runtime))
        if _command_exists(self.settings.ffmpeg_bin):
            command.extend(("--ffmpeg-location", str(Path(self.settings.ffmpeg_bin).parent)))
        if use_cookies and self.settings.ytdlp_cookies_from_browser:
            command.extend(("--cookies-from-browser", self.settings.ytdlp_cookies_from_browser,
                            "--sleep-requests", "1",
                            "--sleep-interval", str(self.settings.ytdlp_sleep_interval),
                            "--max-sleep-interval", str(max(self.settings.ytdlp_sleep_interval,
                                                            self.settings.ytdlp_max_sleep_interval))))
        command.extend(args)
        return command

    def discover(self, query: str, target_unit: str | None = None, limit: int = 10) -> dict[str, int]:
        if not _command_exists(self.settings.ytdlp_bin):
            raise ToolMissing("未找到 yt-dlp；安装后才能自动搜索公开来源，也可先手工导入 URL。")
        target = f"ytsearch{max(1, min(limit, 50))}:{query}"
        proc = _run(self._ytdlp("--dump-single-json", "--flat-playlist", "--skip-download",
                                "--ignore-errors", "--no-warnings", target), timeout=300)
        if proc.returncode != 0:
            raise RuntimeError(ytdlp_error_message(proc.stderr, "搜索"))
        payload = json.loads(proc.stdout)
        self.db.add_traffic("metadata", len(proc.stdout.encode("utf-8")))
        entries = payload.get("entries") or []
        found = created = excluded = excluded_live = 0
        for entry in entries:
            if not entry:
                continue
            found += 1
            url = entry.get("webpage_url") or entry.get("url")
            if not url:
                continue
            metadata = self._normalize_ytdlp(entry, query, target_unit)
            if source_live_reason(metadata.get("metadata") or metadata):
                excluded += 1
                excluded_live += 1
                continue
            if not source_duration_allowed(metadata.get("duration"), self.settings.source_max_duration_seconds):
                excluded += 1
                continue
            _, is_new = self.db.add_source(metadata)
            created += int(is_new)
        return {"found": found, "created": created, "excluded": excluded, "excluded_live": excluded_live,
                "max_duration_seconds": self.settings.source_max_duration_seconds}

    def import_url(self, url: str, target_unit: str | None = None) -> tuple[int, bool]:
        if _command_exists(self.settings.ytdlp_bin):
            proc = _run(self._ytdlp("--dump-single-json", "--skip-download", "--no-warnings", url), timeout=180)
            if proc.returncode == 0:
                self.db.add_traffic("metadata", len(proc.stdout.encode("utf-8")))
                item = json.loads(proc.stdout)
                if reason := source_live_reason(item):
                    raise ValueError(f"不支持导入{reason}；请选择有固定时长的公开视频")
                return self.db.add_source(self._normalize_ytdlp(item, "manual", target_unit))
        platform = "youtube" if "youtu" in url else "vimeo" if "vimeo" in url else "manual"
        video_id = hashlib.sha256(url.encode()).hexdigest()[:20]
        return self.db.add_source({"platform": platform, "video_id": video_id, "url": url, "title": url,
                                   "target_unit": target_unit, "status": "DISCOVERED"})

    def _normalize_ytdlp(self, item: dict[str, Any], query: str, target_unit: str | None) -> dict[str, Any]:
        extractor = str(item.get("extractor_key") or item.get("extractor") or "unknown").lower()
        platform = "youtube" if "youtube" in extractor else "vimeo" if "vimeo" in extractor else extractor
        formats = [{k: f.get(k) for k in ("format_id", "ext", "width", "height", "fps", "filesize", "vcodec", "acodec")}
                   for f in (item.get("formats") or [])]
        raw_url = item.get("webpage_url") or item.get("original_url") or item.get("url")
        if platform == "youtube" and raw_url and not str(raw_url).startswith(("http://", "https://")):
            raw_url = f"https://www.youtube.com/watch?v={raw_url}"
        result = {
            "platform": platform,
            "video_id": str(item.get("id") or hashlib.sha256(str(item.get("webpage_url", "")).encode()).hexdigest()[:20]),
            "url": raw_url,
            "title": item.get("title") or "",
            "uploader": item.get("uploader") or item.get("channel") or "",
            "description": item.get("description") or "",
            "duration": item.get("duration"),
            "thumbnail": item.get("thumbnail") or next((t.get("url") for t in reversed(item.get("thumbnails") or []) if t.get("url")), ""),
            "available_formats": formats,
            "resolution": f"{item.get('width') or 0}x{item.get('height') or 0}",
            "target_unit": target_unit,
            "search_query": query,
            "metadata": item,
            "status": "METADATA_READY",
        }
        result["source_score"] = source_score(item, target_unit, query)
        return result

    def validate_proxy_source(self, source_id: int) -> dict[str, Any]:
        source = self._required_source(source_id)
        try:
            metadata = json.loads(source.get("metadata_json") or "{}")
        except json.JSONDecodeError:
            metadata = {}
        metadata.setdefault("title", source.get("title"))
        metadata.setdefault("duration", source.get("duration"))
        if reason := source_live_reason(metadata):
            raise ValueError(f"不支持下载{reason}；请选择有固定时长的公开视频")
        if not source_duration_allowed(source.get("duration"), self.settings.source_max_duration_seconds):
            minutes = self.settings.source_max_duration_seconds / 60
            raise ValueError(f"来源时长超过 {minutes:g} 分钟上限，为控制下载和分析成本，请换用更短的视频")
        if not _command_exists(self.settings.ytdlp_bin):
            raise ToolMissing("未找到 yt-dlp")
        return source

    def preflight_source(self, source_id: int, cancel=None, progress=None, refresh=True) -> dict[str, str]:
        source = self.validate_proxy_source(source_id)
        cached = json.loads(source.get("metadata_json") or "{}")
        checked_at = (cached.get("preflight") or {}).get("checked_at")
        if not refresh and checked_at and (datetime.now(UTC) - datetime.fromisoformat(checked_at)).total_seconds() < 3600:
            return format_preflight(cached.get("formats") or [], (source.get("target_unit") or "").split('.')[0] or None)
        if progress:
            progress({"stage": "正在查询原片格式（不下载视频）"})
        proc = _run_download(self._ytdlp("--dump-single-json", "--skip-download", "--no-playlist", "--no-warnings", source["url"], use_cookies=True),
                             self.settings.data_dir, "__metadata_none__", 180, 180, cancel=cancel)
        if proc.returncode:
            raise RuntimeError(ytdlp_error_message(proc.stderr, "规格预检"))
        item = json.loads(next(line for line in proc.stderr.splitlines() if line.startswith('{')))
        formats = item.get("formats") or []
        result = format_preflight(formats, (source.get("target_unit") or "").split('.')[0] or None)
        if source_live_reason(item) or not source_duration_allowed(item.get("duration"), self.settings.source_max_duration_seconds):
            result = {"status": "FAIL", "reason": "来源正在直播或超过时长上限"}
        item["preflight"] = dict(result, checked_at=datetime.now(UTC).isoformat())
        best = max((f for f in formats if f.get("vcodec") not in (None, "none")), key=lambda f: (f.get("height") or 0, f.get("width") or 0), default={})
        self.db.update_source(source_id, metadata_json=json.dumps(item, ensure_ascii=False),
                              available_formats=json.dumps(formats, ensure_ascii=False),
                              thumbnail=item.get("thumbnail") or source.get("thumbnail") or "",
                              duration=item.get("duration") or source.get("duration"),
                              resolution=f"{best.get('width') or 0}x{best.get('height') or 0}")
        return result

    def download_proxy(self, source_id: int, cancel=None, progress=None, allow_unknown=False) -> Path:
        source = self.validate_proxy_source(source_id)
        result = self.preflight_source(source_id, cancel=cancel, progress=progress, refresh=False)
        source = self.validate_proxy_source(source_id)
        if result["status"] == "FAIL" or (result["status"] == "UNKNOWN" and not allow_unknown):
            raise ValueError(result["reason"] + "；代理尚未下载，请确认目标单元或重试规格检查")
        if progress:
            progress({"stage": "下载代理中", "bytes": 0, "speed": 0})
        output = self.settings.data_dir / "proxy" / f"{source['platform']}_{source['video_id']}.%(ext)s"
        self.db.update_source(source_id, status="PROXY_QUEUED", error=None)
        before = self._matching_bytes(output.parent, f"{source['platform']}_{source['video_id']}.*")
        fmt = (f"bestvideo[height<={self.settings.proxy_max_height}][vcodec^=avc1]+bestaudio[ext=m4a]/"
               f"best[height<={self.settings.proxy_max_height}][vcodec^=avc1][acodec!=none]/"
               f"bestvideo[height<={self.settings.proxy_max_height}][vcodec!=none]+bestaudio/"
               f"best[height<={self.settings.proxy_max_height}][vcodec!=none]")
        try:
            proc = _run_download(self._ytdlp("-f", fmt, "--merge-output-format", "mp4", "--no-playlist",
                                             "-o", str(output), source["url"], use_cookies=True),
                                 output.parent, f"{source['platform']}_{source['video_id']}.*",
                                 self.settings.ytdlp_stall_timeout_seconds, timeout=3600, cancel=cancel, progress=progress)
        except DownloadStalled as exc:
            message = f"代理下载已自动停止：{exc}。请检查网络，或换一个有固定时长的公开视频。"
            for partial in output.parent.glob(f"{source['platform']}_{source['video_id']}*.part"):
                if partial.is_file():
                    partial.unlink(missing_ok=True)
            self.db.update_source(source_id, status="ERROR", error=message)
            raise RuntimeError(message) from exc
        if proc.returncode != 0:
            message = ytdlp_error_message(proc.stderr, "代理下载")
            self.db.update_source(source_id, status="ERROR", error=message)
            raise RuntimeError(message)
        path = self._find_download(output.parent, f"{source['platform']}_{source['video_id']}.*")
        if progress:
            progress({"stage": "正在验证代理文件", "speed": 0})
        info = self.probe(path)
        if not info.get("playable"):
            self.db.update_source(source_id, status="ERROR", error="代理文件不包含视频画面，请重新下载")
            raise RuntimeError("代理文件不包含视频画面，请重新下载")
        self.db.add_traffic("proxy", max(0, path.stat().st_size - before), source_id)
        self.db.update_source(source_id, status="PROXY_READY", proxy_path=str(path), error=None)
        self.db.update_candidate_proxies(source_id, str(path))
        return path

    def download_final(self, source_id: int) -> Path:
        # All candidates from one source share this lock and re-read the cache
        # AFTER acquiring it. A stale candidate snapshot must not start a second download.
        with self._final_lock_guard:
            lock = self._final_source_locks.setdefault(source_id, Lock())
        with lock:
            return self._download_final_locked(source_id)

    def _validate_original(self, path: Path, source: dict[str, Any]) -> None:
        root = (self.settings.data_dir / "original").resolve()
        path = path.resolve()
        if root not in path.parents:
            raise ValueError("最终源必须位于 original 目录，不能使用代理或外部文件")
        stat = path.stat()
        signature = (stat.st_size, stat.st_mtime_ns)
        if self._verified_originals.get(str(path)) == signature:
            return
        info = self.probe(path)
        if not info.get("playable") or info.get("probe_error") or not info.get("has_audio"):
            raise ValueError("高清源损坏、无法正常探测，或缺少音轨")
        expected = float(source.get("duration") or 0)
        tolerance = max(2.0, expected * 0.01)
        durations = [float(info.get(k) or 0) for k in ("duration", "video_duration", "audio_duration")]
        known = [d for d in durations if d > 0]
        if not known or (expected and any(abs(d - expected) > tolerance for d in known)):
            raise ValueError(f"高清源时长不完整：预期 {expected:.1f}s，文件／视频／音频 {durations}")
        if max(known) - min(known) > tolerance:
            raise ValueError("高清源音视频轨时长不一致")
        # Duration metadata alone is insufficient: decode every video/audio frame
        # before publishing the original for all candidate clips to reuse.
        prefix = [self.settings.ffmpeg_bin, "-hide_banner", "-v", "error", "-xerror"]
        suffix = ["-i", str(path), "-map", "0:v:0", "-map", "0:a:0", "-f", "null", "-"]
        proc = None
        if info.get("video_codec") == "av1":
            # Avoid the very slow libaom software decoder where NVIDIA AV1
            # decoding is available. Keep frames on-device for null output.
            proc = _run(prefix + ["-hwaccel", "cuda", "-hwaccel_output_format", "cuda",
                                  "-c:v", "av1_cuvid"] + suffix, timeout=7200)
        if proc is None or proc.returncode or proc.stderr.strip():
            proc = _run(prefix + suffix, timeout=7200)
        if proc.returncode or proc.stderr.strip():
            raise ValueError("高清源完整解码校验失败：" + proc.stderr[-1000:])
        self._verified_originals[str(path)] = signature

    def _quarantine_originals(self, paths: list[Path]) -> Path:
        root = (self.settings.data_dir / "original").resolve()
        destination = root / "quarantine" / (datetime.now(UTC).strftime("%Y%m%dT%H%M%S") + "_" + uuid.uuid4().hex[:8])
        for path in paths:
            path = path.resolve()
            if path.is_file():
                if root not in path.resolve().parents:
                    raise ValueError("拒绝移动 original 目录以外的文件")
                destination.mkdir(parents=True, exist_ok=True)
                shutil.move(str(path), str(destination / path.name))
        return destination

    def _download_final_locked(self, source_id: int) -> Path:
        source = self._required_source(source_id)
        if not _command_exists(self.settings.ytdlp_bin):
            raise ToolMissing("未找到 yt-dlp")
        root = self.settings.data_dir / "original"
        root.mkdir(parents=True, exist_ok=True)
        # Inspect existing final files, including legacy files not yet recorded in DB.
        stem = f"{source['platform']}_{source['video_id']}"
        legacy = [] if re.search(r'[\\/\[\]*?]', stem) else list(root.glob(stem + ".*"))
        recorded = Path(source["original_path"]) if source.get("original_path") else None
        possible = [recorded] if recorded else [p for p in legacy if p.suffix.lower() in {".mp4", ".mkv", ".webm"} and not re.search(r"\.f\d+\.", p.name)]
        for path in possible:
            if path and path.is_file():
                try:
                    self._validate_original(path, source)
                    self.db.update_source(source_id, original_path=str(path), error=None)
                    return path
                except (ValueError, FileNotFoundError):
                    self._quarantine_originals([path])
        self.db.update_source(source_id, original_path=None)
        if legacy:
            self._quarantine_originals(legacy)
        # An attempt never writes into another attempt's files. Failed attempts
        # remain available for diagnosis, but are never trusted as finished media.
        attempt = root / f"source_{source_id}" / uuid.uuid4().hex
        attempt.mkdir(parents=True)
        output = attempt / "source.%(ext)s"
        log_path = attempt / "download.log"
        try:
            self.db.update_source(source_id, status="FINAL_QUEUED", error=None)
            with self._final_download_slots:
                self.db.update_source(source_id, status="FINAL_DOWNLOADING", error=None)
                proc = _run_download(self._ytdlp("-f", "bestvideo+bestaudio/best", "--merge-output-format", "mp4",
                                    "--no-playlist", "--no-progress", "--socket-timeout", "30", "--retries", "3",
                                    "-o", str(output), source["url"], use_cookies=True),
                                     attempt, "source.*", 180, 7200)
                # Keep diagnostics locally; redact signed download URLs.
                log_path.write_text(re.sub(r"https?://\S+", "[URL]", proc.stderr), encoding="utf-8")
                if proc.returncode:
                    raise RuntimeError(ytdlp_error_message(proc.stderr, "最终源下载"))
                path = self._find_download(attempt, "source.*")
                self.db.update_source(source_id, status="FINAL_VERIFYING")
                self._validate_original(path, source)
            self.db.add_traffic("final", path.stat().st_size, source_id)
            self.db.update_source(source_id, status="FINAL_READY", original_path=str(path), error=None)
            return path
        except Exception as exc:
            detail = re.sub(r"https?://\S+", "[URL]", str(exc))
            with log_path.open("a", encoding="utf-8") as log:
                log.write("\n" + detail + "\n")
            message = f"{detail}；诊断日志：{log_path}"
            self.db.update_source(source_id, status="ERROR", error=message)
            raise RuntimeError(message) from exc

    @staticmethod
    def _matching_bytes(parent: Path, pattern: str) -> int:
        return sum(p.stat().st_size for p in parent.glob(pattern) if p.is_file())

    @staticmethod
    def _find_download(parent: Path, pattern: str) -> Path:
        matches = sorted((p for p in parent.glob(pattern)
                          if p.is_file() and p.suffix.lower() in {".mp4", ".mkv", ".webm", ".mov"} and not re.search(r"\.f\d+\.", p.name)),
                         key=lambda p: p.stat().st_mtime, reverse=True)
        if not matches:
            raise FileNotFoundError("下载完成但未找到已合并的视频文件")
        return matches[0]

    def probe(self, path: Path) -> dict[str, Any]:
        if not _command_exists(self.settings.ffprobe_bin):
            raise ToolMissing("未找到 ffprobe")
        proc = _run([self.settings.ffprobe_bin, "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path)], timeout=120)
        if proc.returncode != 0:
            return {"playable": False, "probe_error": proc.stderr[-1000:]}
        payload = json.loads(proc.stdout)
        video = next((s for s in payload.get("streams", []) if s.get("codec_type") == "video"), {})
        audio = next((s for s in payload.get("streams", []) if s.get("codec_type") == "audio"), None)
        raw_fps = video.get("avg_frame_rate") or video.get("r_frame_rate") or "0/1"
        try:
            a, b = raw_fps.split("/", 1)
            fps = float(a) / float(b)
        except (ValueError, ZeroDivisionError):
            fps = 0.0
        duration = payload.get("format", {}).get("duration") or video.get("duration")
        return {"playable": bool(video), "duration": float(duration or 0), "width": int(video.get("width") or 0),
                "height": int(video.get("height") or 0), "fps": fps, "video_codec": video.get("codec_name"),
                "has_audio": audio is not None, "audio_codec": audio.get("codec_name") if audio else None,
                "video_duration": float(video.get("duration") or 0),
                "audio_duration": float(audio.get("duration") or 0) if audio else 0,
                "probe_error": proc.stderr.strip(),
                "format_name": payload.get("format", {}).get("format_name", ""), "file_size": path.stat().st_size}

    def detect_shots(self, path: Path, threshold: float = 0.28, safety_margin: float = 0.12) -> list[tuple[float, float]]:
        info = self.probe(path)
        duration = float(info.get("duration") or 0)
        if duration <= 0:
            return []
        if not _command_exists(self.settings.ffmpeg_bin):
            return [(0.0, duration)] if duration >= 5 else []
        filter_expr = f"select='gt(scene,{threshold})',showinfo"
        proc = _run([self.settings.ffmpeg_bin, "-hide_banner", "-i", str(path), "-vf", filter_expr, "-an", "-f", "null", "-"], timeout=7200)
        cuts = [float(x) for x in re.findall(r"pts_time:([0-9.]+)", proc.stderr)]
        boundaries = [0.0] + sorted({c for c in cuts if 0 < c < duration}) + [duration]
        shots: list[tuple[float, float]] = []
        for idx, (start, end) in enumerate(zip(boundaries, boundaries[1:])):
            safe_start = start + (safety_margin if idx > 0 else 0)
            safe_end = end - (safety_margin if idx < len(boundaries) - 2 else 0)
            if safe_end - safe_start >= 5.0:
                shots.append((round(safe_start, 3), round(safe_end, 3)))
        return shots

    def analyze_source(self, source_id: int) -> list[int]:
        source = self._required_source(source_id)
        if not source.get("proxy_path"):
            raise ValueError("请先下载代理")
        path = Path(source["proxy_path"])
        info = self.probe(path)
        if not info.get("playable"):
            raise ValueError("代理文件不包含视频画面，请返回生产台重新下载代理")
        shots = self.detect_shots(path)
        unit = source.get("target_unit")
        bucket = unit.split(".", 1)[0] if unit else None
        material_type = self.rules.buckets.get(bucket, {}).get("material_type") if bucket else None
        # Re-analysis replaces only machine-created/unreviewed slices. Human
        # decisions and already-produced final clips are deliberately preserved.
        self.db.clear_replaceable_candidates(source_id)
        created_ids: list[int] = []
        for shot_start, shot_end in shots:
            # Stage 1 is always the hard-cut detector. Only a single-shot range
            # reaches stage 2, where sustained subject absence creates new
            # reviewable slices instead of rejecting the whole source.
            subject = self.subject_analyzer.analyze(path, shot_start, shot_end, unit)
            for segment in subject.segments:
                # 一个 5 分钟的长镜头只能占一个长档名额，先等分成 30-60s 的交付片段，
                # 人工不必再靠裁剪面板一刀一刀切。
                parts = split_long_segment(*segment)
                for part_index, (start, end) in enumerate(parts, start=1):
                    duration = end - start
                    frame_hash = self.representative_frame_hash(path, start + duration / 2)
                    if frame_hash and self.db.candidate_hash_exists(frame_hash):
                        continue
                    duration_bucket, _, _ = self.rules.duration_bucket(duration)
                    facts = dict(info, duration=duration, shot_count=1, unit=unit, bucket=bucket,
                                 material_type=material_type, source_type="PROXY", **subject.facts_for(segment))
                    if len(parts) > 1:
                        facts["split_note"] = f"长镜头等分 {part_index}/{len(parts)}"
                    cid, created = self.db.add_candidate({"source_id": source_id, "start_time": start, "end_time": end,
                        "duration": duration, "proxy_path": str(path), "candidate_bucket": bucket, "candidate_unit": unit,
                        "material_type": material_type, "duration_bucket": duration_bucket, "score": source.get("source_score", 0),
                        "representative_hash": frame_hash, "facts": facts})
                    if created:
                        results = self.rules.evaluate(facts)
                        gate = self.rules.unit_gate(unit)
                        if gate:
                            results.append(gate)
                        self.db.save_rule_results(cid, [r.to_dict() for r in results])
                        if self.rules.automatic_reject(results):
                            self.db.review(cid, {"decision": "REJECT", "notes": "程序确定性硬规则自动淘汰"})
                        created_ids.append(cid)
        self.db.update_source(source_id, status="WAITING_REVIEW", analysis_completed=1, error=None)
        return created_ids

    def representative_frame_hash(self, path: Path, at_seconds: float) -> str | None:
        """Representative-frame dHash, robust to resolution and moderate recompression."""
        if not _command_exists(self.settings.ffmpeg_bin):
            return None
        proc = subprocess.run([self.settings.ffmpeg_bin, "-v", "error", "-ss", f"{at_seconds:.3f}", "-i", str(path),
                               "-frames:v", "1", "-vf", "scale=9:8,format=gray", "-f", "rawvideo", "-"],
                              capture_output=True, timeout=120, check=False)
        raw = proc.stdout
        if proc.returncode != 0 or len(raw) < 72:
            return None
        bits = []
        for y in range(8):
            row = raw[y * 9:(y + 1) * 9]
            bits.extend(1 if row[x] > row[x + 1] else 0 for x in range(8))
        value = sum(bit << (63 - idx) for idx, bit in enumerate(bits))
        return f"{value:016x}"

    def silence_ratio(self, path: Path, duration: float) -> float | None:
        if not _command_exists(self.settings.ffmpeg_bin) or duration <= 0:
            return None
        proc = _run([self.settings.ffmpeg_bin, "-hide_banner", "-i", str(path), "-af", "silencedetect=noise=-50dB:d=0.5", "-f", "null", "-"], timeout=7200)
        starts = [float(x) for x in re.findall(r"silence_start: ([0-9.]+)", proc.stderr)]
        ends = [(float(a), float(b)) for a, b in re.findall(r"silence_end: ([0-9.]+) \| silence_duration: ([0-9.]+)", proc.stderr)]
        silent = sum(length for _, length in ends)
        if len(starts) > len(ends) and starts:
            silent += max(0.0, duration - starts[-1])
        return min(1.0, silent / duration)

    def black_ratio(self, path: Path, duration: float) -> float | None:
        if not _command_exists(self.settings.ffmpeg_bin) or duration <= 0:
            return None
        proc = _run([self.settings.ffmpeg_bin, "-hide_banner", "-i", str(path), "-vf", "blackdetect=d=0.05:pic_th=0.98", "-an", "-f", "null", "-"], timeout=7200)
        black = sum(float(x) for x in re.findall(r"black_duration:([0-9.]+)", proc.stderr))
        return min(1.0, black / duration)

    def trim_candidate(self, candidate_id: int, start_time: float, end_time: float) -> dict[str, Any]:
        candidate = self.db.get_candidate(candidate_id)
        if not candidate:
            raise KeyError("候选不存在")
        if not all(math.isfinite(value) for value in (start_time, end_time)):
            raise ValueError("裁剪时间必须是有效数字")
        facts = json.loads(candidate.get("facts_json") or "{}")
        allowed_start = float(facts.get("analysis_segment_start", facts.get("subject_segment_start", candidate["start_time"])))
        allowed_end = float(facts.get("analysis_segment_end", facts.get("subject_segment_end", candidate["end_time"])))
        start_time, end_time = round(float(start_time), 3), round(float(end_time), 3)
        if start_time < allowed_start - 0.001 or end_time > allowed_end + 0.001:
            raise ValueError(f"人工微调只能在原候选范围 {allowed_start:.3f}s–{allowed_end:.3f}s 内收缩")
        if end_time <= start_time:
            raise ValueError("终点必须晚于起点")
        duration = end_time - start_time
        if duration < 5.0:
            raise ValueError("调整后的候选不得短于 5 秒")
        duration_bucket, _, _ = self.rules.duration_bucket(duration)
        facts.update({
            "duration": duration,
            "manual_trim_applied": True,
            "manual_trim_start": start_time,
            "manual_trim_end": end_time,
            "boundary_reviewed": True,
        })
        suggestion = dict(facts.get("boundary_suggestion") or {})
        suggestion["applied_or_reviewed"] = True
        facts["boundary_suggestion"] = suggestion
        self.db.trim_candidate(candidate_id, start_time, end_time, duration_bucket, facts)
        results = self.rules.evaluate(facts)
        gate = self.rules.unit_gate(candidate.get("candidate_unit"))
        if gate:
            results.append(gate)
        self.db.save_rule_results(candidate_id, [result.to_dict() for result in results])
        updated = self.db.get_candidate(candidate_id)
        return {"candidate": updated, "rules": [result.to_dict() for result in results]}

    def clip_final(self, candidate_id: int) -> Path:
        candidate = self.db.get_candidate(candidate_id)
        if not candidate:
            raise KeyError("候选不存在")
        if candidate["status"] != "ACCEPTED":
            raise ValueError("只有人工接受的候选才能制作最终片段")
        source_path = str(self.download_final(int(candidate["source_id"])))
        original = Path(source_path).resolve()
        proxy_root = (self.settings.data_dir / "proxy").resolve()
        original_root = (self.settings.data_dir / "original").resolve()
        if proxy_root == original or proxy_root in original.parents:
            raise AssertionError("禁止从代理文件制作交付片段")
        if original_root not in original.parents:
            raise AssertionError("最终片段源文件必须来自 original 目录（source_type=FINAL）")
        if not _command_exists(self.settings.ffmpeg_bin):
            raise ToolMissing("未找到 ffmpeg")
        output = self._clip_path(candidate)
        start, duration = float(candidate["start_time"]), float(candidate["duration"])
        # Accurate input seeking + normal encode preserves content and avoids keyframe drift.
        proc = _run([self.settings.ffmpeg_bin, "-hide_banner", "-y", "-ss", f"{start:.3f}", "-i", str(original),
                     "-t", f"{duration:.3f}", "-map", "0:v:0", "-map", "0:a?", "-c:v", "libx264", "-preset", "medium",
                     "-crf", "17", "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", str(output)], timeout=7200)
        if proc.returncode != 0:
            raise RuntimeError(proc.stderr[-2000:] or "最终剪片失败")
        return output

    def final_qa_and_deliver(self, candidate_id: int) -> dict[str, Any]:
        with self._final_lock_guard:
            lock = self._final_candidate_locks.setdefault(candidate_id, Lock())
        if not lock.acquire(blocking=False):
            raise ValueError("该片段正在最终处理，请勿重复提交")
        try:
            return self._final_qa_and_deliver_locked(candidate_id)
        finally:
            lock.release()

    def _final_qa_and_deliver_locked(self, candidate_id: int) -> dict[str, Any]:
        candidate = self.db.get_candidate(candidate_id)
        if not candidate:
            raise KeyError("候选不存在")
        if not candidate.get("candidate_bucket") or not candidate.get("candidate_unit") or candidate.get("candidate_viewpoint") not in {"first_person", "third_person"}:
            raise ValueError("最终处理前必须由人工确认桶、单元和人称")
        original = self.download_final(int(candidate["source_id"]))
        candidate = self.db.get_candidate(candidate_id) or candidate
        source_info = self.probe(original)
        resolution = self.rules._r9(dict(source_info, source_type="FINAL"), candidate["candidate_bucket"])
        if resolution.status == RuleStatus.FAIL:
            result = resolution.to_dict()
            self.db.save_rule_results(candidate_id, [result], stage="final")
            self.db.create_final_clip(candidate_id, original_path=str(original),
                                      width=source_info.get("width"), height=source_info.get("height"),
                                      qa_status="FAIL", deliverable_status="BLOCKED",
                                      qa={"rules": [result], "probe": source_info})
            self.db.review(candidate_id, {"decision": "REJECT", "notes": resolution.reason})
            return {"qa_status": "FAIL", "final_path": None, "probe": source_info,
                    "rules": [result], "message": resolution.reason + "；已筛除，可在拒绝列表查看"}
        clip = self._clip_path(candidate)
        if not clip.exists():
            clip = self.clip_final(candidate_id)
            candidate = self.db.get_candidate(candidate_id) or candidate
        info = self.probe(clip)
        if info.get("has_audio"):
            info["silence_ratio"] = self.silence_ratio(clip, float(info.get("duration") or 0))
        info["black_ratio"] = self.black_ratio(clip, float(info.get("duration") or 0))
        detected = self.detect_shots(clip)
        info["shot_count"] = max(1, len(detected))
        subject_trim_required = False
        if info["shot_count"] == 1:
            subject = self.subject_analyzer.analyze(
                clip, 0.0, float(info.get("duration") or 0), candidate.get("candidate_unit")
            )
            info.update(subject.facts_for((0.0, float(info.get("duration") or 0))))
            info["subject_proposed_segments"] = [list(segment) for segment in subject.segments]
            subject_trim_required = subject.status == "SPLIT"
            info["subject_trim_required"] = subject_trim_required
            if subject_trim_required:
                info["subject_check"] = "TRIM_REQUIRED"
        else:
            info.update({"subject_check": "SKIPPED_SHOT_CHANGE", "subject_trim_required": False})
        facts = merge_facts(candidate.get("facts_json") or "{}", info,
                            unit=candidate.get("candidate_unit"), bucket=candidate.get("candidate_bucket"),
                            material_type=candidate.get("material_type"), duration=info.get("duration"),
                            shot_count=info["shot_count"], source_type="FINAL")
        results = self.rules.evaluate(facts)
        gate = self.rules.unit_gate(candidate.get("candidate_unit"))
        if gate:
            results.append(gate)
        hard_fail = self.rules.automatic_reject(results)
        conflicts = any(r.status == RuleStatus.CONFLICT for r in results)
        qa_status = "FAIL" if hard_fail else "TRIM_REQUIRED" if subject_trim_required else "CONFLICT" if conflicts else "PASS"
        self.db.save_rule_results(candidate_id, [r.to_dict() for r in results], stage="final")
        qa_payload = {"rules": [r.to_dict() for r in results], "probe": info}
        self.db.create_final_clip(candidate_id, original_path=candidate.get("original_path"), final_path=str(clip),
                                  duration=info.get("duration"), width=info.get("width"), height=info.get("height"), fps=info.get("fps"),
                                  has_audio=int(bool(info.get("has_audio"))), qa_status=qa_status,
                                  deliverable_status="PENDING" if qa_status == "PASS" else "BLOCKED", qa=qa_payload)
        final_path = None
        if qa_status == "PASS":
            bucket = candidate.get("candidate_bucket") or "UNSET"
            unit = candidate.get("candidate_unit") or "UNSET"
            bucket_name = self.rules.buckets.get(bucket, {}).get("name", "未分类")
            viewpoint = "第一人称" if candidate.get("candidate_viewpoint") == "first_person" else "第三人称"
            deliver_dir = self.settings.data_dir / "deliverable" / "QT寻源数据" / f"{bucket}_{bucket_name}" / viewpoint
            deliver_dir.mkdir(parents=True, exist_ok=True)
            assignment = self.db.reserve_delivery_filename(
                candidate_id, unit, delivery_description(candidate.get("source_title") or "")
            )
            final_path = deliver_dir / assignment["filename"]
            shutil.copy2(clip, final_path)
            self.db.update_final_clip_delivery(candidate_id, str(final_path), assignment["filename"], "READY")
        if qa_status == "PASS":
            self.db.update_source(int(candidate["source_id"]), status="DELIVERABLE")
            with self.db.connect() as con:
                con.execute("UPDATE candidate_shots SET status='DELIVERABLE' WHERE id=?", (candidate_id,))
        return {"qa_status": qa_status, "final_path": str(final_path) if final_path else None, "probe": info,
                "rules": [r.to_dict() for r in results]}

    def export_delivery_csv(self) -> Path:
        rows = self.db.delivery_rows()
        delivery_time = datetime.now(UTC).isoformat()
        output = self.settings.data_dir / "deliverable" / "QT寻源数据_交付信息表.csv"
        fields = ["人称", "OSS路径", "交付时间", "统合单元", "分辨率", "时长"]
        try:
            stream = output.open("w", encoding="utf-8-sig", newline="")
        except PermissionError:
            # Spreadsheet programs can exclusively lock a CSV on Windows. Keep
            # exporting useful instead of failing the whole operation.
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            output = output.with_name(f"QT寻源数据_交付信息表_{timestamp}.csv")
            stream = output.open("w", encoding="utf-8-sig", newline="")
        with stream:
            writer = csv.DictWriter(stream, fieldnames=fields)
            writer.writeheader()
            for row in rows:
                writer.writerow({
                    "人称": "第一人称" if row["viewpoint"] == "first_person" else "第三人称",
                    "OSS路径": "OSS",
                    "交付时间": row.get("exported_at") or delivery_time,
                    "统合单元": row["unit"],
                    "分辨率": f"{row['width']}x{row['height']}",
                    "时长": round(float(row["duration"] or 0), 3),
                })
        self.db.mark_delivery_exported([int(row["candidate_id"]) for row in rows], delivery_time)
        return output

    def _clip_path(self, candidate: dict[str, Any]) -> Path:
        source_stem = (Path(candidate["original_path"]).stem if candidate.get("original_path")
                       else f"{candidate.get('platform') or 'source'}_{candidate.get('video_id') or candidate['source_id']}")
        # Isolated download attempts use a generic basename; clip storage is
        # shared across sources, so include the stable source ID there.
        if source_stem == "source":
            source_stem = f"source_{candidate['source_id']}"
        safe_stem = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", source_stem).strip(" .") or "source"
        part_number = self.db.candidate_part_number(int(candidate["id"]))
        return self.settings.data_dir / "clips" / f"{safe_stem}-{part_number}.mp4"

    def _required_source(self, source_id: int) -> dict[str, Any]:
        source = self.db.get_source(source_id)
        if not source:
            raise KeyError("来源不存在")
        return source
