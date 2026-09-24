from __future__ import annotations

import json
import re
import sqlite3
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterator


STATUSES = (
    "DISCOVERED", "METADATA_READY", "PROXY_QUEUED", "PROXY_READY", "ANALYZING",
    "WAITING_REVIEW", "ACCEPTED", "REJECTED", "FINAL_DOWNLOADING", "FINAL_READY",
    "FINAL_QA", "DELIVERABLE", "ERROR",
)


SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS sources (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  platform TEXT NOT NULL,
  video_id TEXT NOT NULL,
  url TEXT NOT NULL,
  title TEXT NOT NULL DEFAULT '',
  uploader TEXT NOT NULL DEFAULT '',
  description TEXT NOT NULL DEFAULT '',
  duration REAL,
  thumbnail TEXT NOT NULL DEFAULT '',
  available_formats TEXT NOT NULL DEFAULT '[]',
  resolution TEXT NOT NULL DEFAULT '',
  discovered_at TEXT NOT NULL,
  metadata_json TEXT NOT NULL DEFAULT '{}',
  source_hash TEXT,
  status TEXT NOT NULL DEFAULT 'DISCOVERED',
  target_unit TEXT,
  search_query TEXT,
  source_score REAL NOT NULL DEFAULT 0,
  proxy_path TEXT,
  original_path TEXT,
  analysis_completed INTEGER NOT NULL DEFAULT 0,
  error TEXT,
  UNIQUE(platform, video_id)
);

CREATE TABLE IF NOT EXISTS search_jobs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  query TEXT NOT NULL,
  target_unit TEXT,
  status TEXT NOT NULL DEFAULT 'DISCOVERED',
  sources_found INTEGER NOT NULL DEFAULT 0,
  candidates_created INTEGER NOT NULL DEFAULT 0,
  accepted INTEGER NOT NULL DEFAULT 0,
  qa_passed INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL,
  finished_at TEXT,
  error TEXT
);

CREATE TABLE IF NOT EXISTS candidate_shots (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  source_id INTEGER NOT NULL REFERENCES sources(id),
  start_time REAL NOT NULL,
  end_time REAL NOT NULL,
  duration REAL NOT NULL,
  proxy_path TEXT,
  candidate_bucket TEXT,
  candidate_unit TEXT,
  candidate_viewpoint TEXT, -- kept for existing databases, no longer used
  material_type TEXT,
  duration_bucket TEXT,
  delivery_description TEXT,
  score REAL NOT NULL DEFAULT 0,
  status TEXT NOT NULL DEFAULT 'WAITING_REVIEW',
  representative_hash TEXT,
  facts_json TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL,
  UNIQUE(source_id, start_time, end_time)
);

CREATE TABLE IF NOT EXISTS rule_results (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  candidate_id INTEGER NOT NULL REFERENCES candidate_shots(id),
  stage TEXT NOT NULL DEFAULT 'candidate',
  rule_id TEXT NOT NULL,
  status TEXT NOT NULL,
  reason TEXT NOT NULL,
  evidence_json TEXT NOT NULL DEFAULT '{}',
  deterministic INTEGER NOT NULL DEFAULT 0,
  checked_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS ai_analysis (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  candidate_id INTEGER NOT NULL REFERENCES candidate_shots(id),
  phase TEXT NOT NULL,
  model TEXT,
  analysis_json TEXT NOT NULL,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS reviews (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  candidate_id INTEGER NOT NULL REFERENCES candidate_shots(id),
  decision TEXT NOT NULL,
  final_bucket TEXT,
  final_unit TEXT,
  final_viewpoint TEXT, -- kept for existing databases, no longer used
  notes TEXT,
  manual_rule_overrides TEXT NOT NULL DEFAULT '{}',
  reviewed_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS final_clips (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  candidate_id INTEGER NOT NULL UNIQUE REFERENCES candidate_shots(id),
  original_path TEXT,
  final_path TEXT,
  duration REAL,
  width INTEGER,
  height INTEGER,
  fps REAL,
  has_audio INTEGER,
  qa_status TEXT NOT NULL DEFAULT 'PENDING',
  deliverable_status TEXT NOT NULL DEFAULT 'PENDING',
  exported_at TEXT,
  delivery_unit TEXT,
  delivery_sequence INTEGER,
  delivery_filename TEXT,
  qa_json TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS delivery_sequences (
  unit TEXT PRIMARY KEY,
  last_sequence INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS traffic_stats (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  direction TEXT NOT NULL,
  kind TEXT NOT NULL,
  bytes INTEGER NOT NULL,
  source_id INTEGER REFERENCES sources(id),
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS jobs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  kind TEXT NOT NULL,
  entity_id INTEGER,
  payload_json TEXT NOT NULL DEFAULT '{}',
  status TEXT NOT NULL DEFAULT 'QUEUED',
  attempts INTEGER NOT NULL DEFAULT 0,
  result_json TEXT NOT NULL DEFAULT '{}',
  error TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_sources_status ON sources(status);
CREATE INDEX IF NOT EXISTS idx_candidates_status ON candidate_shots(status);
CREATE INDEX IF NOT EXISTS idx_rules_candidate ON rule_results(candidate_id);
CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status);
CREATE INDEX IF NOT EXISTS idx_reviews_candidate ON reviews(candidate_id);
"""


def now() -> str:
    return datetime.now(UTC).isoformat()


class Database:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as con:
            con.executescript(SCHEMA)
            columns = {row[1] for row in con.execute("PRAGMA table_info(jobs)")}
            if "result_json" not in columns:
                con.execute("ALTER TABLE jobs ADD COLUMN result_json TEXT NOT NULL DEFAULT '{}'")
            source_columns = {row[1] for row in con.execute("PRAGMA table_info(sources)")}
            if "deleted_at" not in source_columns:
                con.execute("ALTER TABLE sources ADD COLUMN deleted_at TEXT")
            if "analysis_completed" not in source_columns:
                con.execute("ALTER TABLE sources ADD COLUMN analysis_completed INTEGER NOT NULL DEFAULT 0")
                con.execute("""UPDATE sources SET analysis_completed=1
                               WHERE EXISTS (SELECT 1 FROM candidate_shots c WHERE c.source_id=sources.id)""")
            candidate_columns = {row[1] for row in con.execute("PRAGMA table_info(candidate_shots)")}
            if "delivery_description" not in candidate_columns:
                con.execute("ALTER TABLE candidate_shots ADD COLUMN delivery_description TEXT")
            final_columns = {row[1] for row in con.execute("PRAGMA table_info(final_clips)")}
            if "exported_at" not in final_columns:
                con.execute("ALTER TABLE final_clips ADD COLUMN exported_at TEXT")
            if "delivery_unit" not in final_columns:
                con.execute("ALTER TABLE final_clips ADD COLUMN delivery_unit TEXT")
            if "delivery_sequence" not in final_columns:
                con.execute("ALTER TABLE final_clips ADD COLUMN delivery_sequence INTEGER")
            if "delivery_filename" not in final_columns:
                con.execute("ALTER TABLE final_clips ADD COLUMN delivery_filename TEXT")
            con.execute("""CREATE TABLE IF NOT EXISTS delivery_sequences (
                           unit TEXT PRIMARY KEY,
                           last_sequence INTEGER NOT NULL
                           )""")
            self._migrate_delivery_numbering(con)
            con.execute("""CREATE UNIQUE INDEX IF NOT EXISTS idx_final_delivery_sequence
                           ON final_clips(delivery_unit,delivery_sequence)
                           WHERE delivery_unit IS NOT NULL AND delivery_sequence IS NOT NULL""")
            self._migrate_proxy_r9_results(con)

    @staticmethod
    def _migrate_delivery_numbering(con: sqlite3.Connection) -> None:
        """Assign stable per-unit sequence numbers to legacy delivered clips."""
        rows = con.execute("""SELECT f.id,f.final_path,f.delivery_unit,f.delivery_sequence,f.delivery_filename,
                               c.candidate_unit unit
                               FROM final_clips f JOIN candidate_shots c ON c.id=f.candidate_id
                               WHERE f.qa_status='PASS' AND COALESCE(c.candidate_unit,'')<>''
                               ORDER BY f.created_at,f.id""").fetchall()
        used: dict[str, set[int]] = {}
        unassigned: list[sqlite3.Row] = []

        # Preserve only assignments already stored by the new numbering system,
        # or filenames that already match UNIT_001_description.mp4.
        for row in rows:
            unit = str(row["delivery_unit"] or row["unit"])
            sequence = int(row["delivery_sequence"] or 0)
            filename = str(row["delivery_filename"] or "")
            if not filename and row["final_path"]:
                filename = Path(str(row["final_path"])).name
            compliant = re.fullmatch(rf"{re.escape(unit)}_(\d{{3,}})_.+\.mp4", filename, re.IGNORECASE)
            if sequence <= 0 and compliant:
                sequence = int(compliant.group(1))
            if sequence > 0 and sequence not in used.setdefault(unit, set()):
                used[unit].add(sequence)
                con.execute("""UPDATE final_clips
                               SET delivery_unit=?,delivery_sequence=?,delivery_filename=?
                               WHERE id=?""", (unit, sequence, filename if compliant else "", row["id"]))
            else:
                unassigned.append(row)

        last_by_unit = {
            str(row["unit"]): int(row["last_sequence"])
            for row in con.execute("SELECT unit,last_sequence FROM delivery_sequences")
        }
        for unit, sequences in used.items():
            last_by_unit[unit] = max(last_by_unit.get(unit, 0), max(sequences, default=0))

        for row in unassigned:
            unit = str(row["unit"])
            sequence = last_by_unit.get(unit, 0) + 1
            while sequence in used.setdefault(unit, set()):
                sequence += 1
            used[unit].add(sequence)
            last_by_unit[unit] = sequence
            con.execute("""UPDATE final_clips
                           SET delivery_unit=?,delivery_sequence=?,delivery_filename=''
                           WHERE id=?""", (unit, sequence, row["id"]))

        for unit, sequence in last_by_unit.items():
            con.execute("""INSERT INTO delivery_sequences(unit,last_sequence) VALUES(?,?)
                           ON CONFLICT(unit) DO UPDATE SET last_sequence=MAX(last_sequence,excluded.last_sequence)""",
                        (unit, sequence))

    @staticmethod
    def _migrate_proxy_r9_results(con: sqlite3.Connection) -> None:
        """Undo legacy automatic rejects caused solely by checking R9 on proxies."""
        con.execute("""UPDATE rule_results
                       SET status='UNKNOWN',
                           reason='代理资源仅用于切镜和预览；分辨率将在下载原视频后按最终源判定',
                           evidence_json='{"source_type":"PROXY"}',deterministic=0
                       WHERE stage='candidate' AND rule_id='R9' AND status='FAIL'""")
        con.execute("""UPDATE candidate_shots AS c SET status='WAITING_REVIEW'
                       WHERE c.status='REJECTED'
                         AND (SELECT notes FROM reviews r WHERE r.candidate_id=c.id
                              ORDER BY r.id DESC LIMIT 1)='程序确定性硬规则自动淘汰'
                         AND NOT EXISTS (
                             SELECT 1 FROM rule_results rr
                             WHERE rr.candidate_id=c.id AND rr.stage='candidate'
                               AND rr.status='FAIL' AND rr.deterministic=1
                         )""")

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        con = sqlite3.connect(self.path, timeout=30)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA foreign_keys=ON")
        try:
            yield con
            con.commit()
        finally:
            con.close()

    @staticmethod
    def _dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
        return dict(row) if row else None

    def add_source(self, source: dict[str, Any]) -> tuple[int, bool]:
        platform = source.get("platform") or "manual"
        video_id = str(source.get("video_id") or source.get("id") or source["url"])
        with self.connect() as con:
            existing = con.execute("SELECT id FROM sources WHERE platform=? AND video_id=?", (platform, video_id)).fetchone()
            if existing:
                return int(existing["id"]), False
            cur = con.execute(
                """INSERT INTO sources
                (platform,video_id,url,title,uploader,description,duration,thumbnail,available_formats,resolution,
                 discovered_at,metadata_json,status,target_unit,search_query,source_score)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (platform, video_id, source["url"], source.get("title", ""), source.get("uploader", ""),
                 source.get("description", ""), source.get("duration"), source.get("thumbnail", ""),
                 json.dumps(source.get("available_formats", []), ensure_ascii=False), source.get("resolution", ""),
                 now(), json.dumps(source.get("metadata", source), ensure_ascii=False), source.get("status", "METADATA_READY"),
                 source.get("target_unit"), source.get("search_query"), float(source.get("source_score", 0))))
            return int(cur.lastrowid), True

    @staticmethod
    def _bucket_condition(column: str, bucket: str | None) -> tuple[str | None, list[Any]]:
        if not bucket:
            return None, []
        if bucket == "unassigned":
            return f"({column} IS NULL OR {column}='')", []
        if not re.fullmatch(r"T[1-9]", bucket):
            raise ValueError("分类必须是 T1–T9 或 unassigned")
        return f"({column}=? OR {column} LIKE ?)", [bucket, f"{bucket}.%"]

    @classmethod
    def _source_filters(cls, view: str, bucket: str | None = None,
                        max_duration: int | None = None) -> tuple[str, list[Any]]:
        view_condition = {"candidate": "s.analysis_completed=0", "analyzed": "s.analysis_completed=1",
                          "all": None, "deleted": None}.get(view)
        if view not in {"candidate", "analyzed", "all", "deleted"}:
            raise ValueError("来源列表类型必须是 candidate、analyzed、deleted 或 all")
        conditions = [view_condition] if view_condition else []
        conditions.append("s.deleted_at IS NOT NULL" if view == "deleted" else "s.deleted_at IS NULL")
        args: list[Any] = []
        bucket_condition, bucket_args = cls._bucket_condition("s.target_unit", bucket)
        if bucket_condition:
            conditions.append(bucket_condition)
            args.extend(bucket_args)
        if view == "candidate" and max_duration:
            conditions.append("(s.duration IS NULL OR s.duration<=?)")
            args.append(max_duration)
        return ("WHERE " + " AND ".join(conditions)) if conditions else "", args

    def count_sources(self, view: str = "all", bucket: str | None = None,
                      max_duration: int | None = None) -> int:
        where, args = self._source_filters(view, bucket, max_duration)
        with self.connect() as con:
            return int(con.execute("SELECT COUNT(*) FROM sources s " + where, args).fetchone()[0])

    def list_sources(self, limit: int = 20, view: str = "all", offset: int = 0,
                     bucket: str | None = None, max_duration: int | None = None) -> list[dict[str, Any]]:
        where, args = self._source_filters(view, bucket, max_duration)
        with self.connect() as con:
            items = [dict(r) for r in con.execute("""SELECT s.*,
                (SELECT COUNT(*) FROM candidate_shots c WHERE c.source_id=s.id) candidate_count,
                (SELECT j.id FROM jobs j WHERE j.entity_id=s.id AND j.kind IN ('proxy','analyze','preflight')
                 AND j.status IN ('QUEUED','RUNNING') ORDER BY j.id DESC LIMIT 1) active_job_id,
                (SELECT j.kind FROM jobs j WHERE j.entity_id=s.id AND j.kind IN ('proxy','analyze','preflight')
                 AND j.status IN ('QUEUED','RUNNING') ORDER BY j.id DESC LIMIT 1) active_job_kind
                FROM sources s """ + where + " ORDER BY s.source_score DESC,s.id DESC LIMIT ? OFFSET ?",
                args + [limit, offset])]
            for item in items:
                if item.get("active_job_id"):
                    item.update(self._job_queue_fields(con, int(item["active_job_id"])))
            return items

    def get_source(self, source_id: int) -> dict[str, Any] | None:
        with self.connect() as con:
            return self._dict(con.execute("SELECT * FROM sources WHERE id=?", (source_id,)).fetchone())

    def update_source(self, source_id: int, **fields: Any) -> None:
        allowed = {"status", "proxy_path", "original_path", "analysis_completed", "error", "duration", "resolution", "metadata_json", "source_score", "thumbnail", "available_formats"}
        pairs = [(k, v) for k, v in fields.items() if k in allowed]
        if not pairs:
            return
        with self.connect() as con:
            con.execute(f"UPDATE sources SET {','.join(k+'=?' for k,_ in pairs)} WHERE id=?", [v for _, v in pairs] + [source_id])

    def set_source_deleted(self, source_id: int, deleted: bool) -> None:
        with self.connect() as con:
            con.execute("BEGIN IMMEDIATE")
            source = con.execute("SELECT * FROM sources WHERE id=?", (source_id,)).fetchone()
            if not source:
                raise KeyError("来源不存在")
            if deleted:
                if source["analysis_completed"]:
                    raise ValueError("请先将已分析来源移回候选来源，再删除")
                if con.execute("SELECT 1 FROM jobs WHERE entity_id=? AND status IN ('QUEUED','RUNNING')", (source_id,)).fetchone():
                    raise ValueError("来源正在执行任务，请先取消或等待任务完成后再删除")
            con.execute("UPDATE sources SET deleted_at=? WHERE id=?", (now() if deleted else None, source_id))

    def update_candidate_proxies(self, source_id: int, proxy_path: str) -> None:
        with self.connect() as con:
            con.execute("UPDATE candidate_shots SET proxy_path=? WHERE source_id=?", (proxy_path, source_id))

    def create_job(self, kind: str, entity_id: int, payload: dict[str, Any] | None = None) -> tuple[int, bool]:
        if kind not in {"proxy", "analyze", "preflight"}:
            raise ValueError("不支持的后台任务类型")
        with self.connect() as con:
            con.execute("BEGIN IMMEDIATE")
            source = con.execute("SELECT deleted_at FROM sources WHERE id=?", (entity_id,)).fetchone()
            if source and source["deleted_at"]:
                raise ValueError("来源已删除，请先恢复后再操作")
            existing = con.execute("""SELECT id FROM jobs WHERE entity_id=?
                AND status IN ('QUEUED','RUNNING') ORDER BY id DESC LIMIT 1""", (entity_id,)).fetchone()
            if existing:
                return int(existing["id"]), False
            timestamp = now()
            cur = con.execute("""INSERT INTO jobs(kind,entity_id,payload_json,status,created_at,updated_at)
                VALUES(?,?,?,'QUEUED',?,?)""",
                (kind, entity_id, json.dumps(payload or {}, ensure_ascii=False), timestamp, timestamp))
            return int(cur.lastrowid), True

    def get_job(self, job_id: int) -> dict[str, Any] | None:
        with self.connect() as con:
            job = self._dict(con.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone())
            if job:
                job.update(self._job_queue_fields(con, job_id))
            return job

    @staticmethod
    def _job_queue_fields(con: sqlite3.Connection, job_id: int) -> dict[str, Any]:
        row = con.execute("SELECT status,kind FROM jobs WHERE id=?", (job_id,)).fetchone()
        status = row["status"] if row else None
        if status != "QUEUED":
            return {"active_job_status": status, "queue_position": None, "queue_ahead": 0}
        queued_before = int(con.execute(
            "SELECT COUNT(*) FROM jobs WHERE status='QUEUED' AND kind=? AND id<?", (row["kind"], job_id)
        ).fetchone()[0])
        running = int(con.execute("SELECT COUNT(*) FROM jobs WHERE status='RUNNING' AND kind=?", (row["kind"],)).fetchone()[0])
        return {"active_job_status": status, "queue_position": queued_before + 1,
                "queue_ahead": running + queued_before}

    def start_job(self, job_id: int) -> None:
        with self.connect() as con:
            con.execute("UPDATE jobs SET status='RUNNING',attempts=attempts+1,error=NULL,updated_at=? WHERE id=?", (now(), job_id))

    def finish_job(self, job_id: int, status: str, result: dict[str, Any] | None = None, error: str | None = None) -> None:
        if status not in {"DONE", "FAILED", "CANCELLED"}:
            raise ValueError("任务结束状态必须是 DONE 或 FAILED")
        with self.connect() as con:
            con.execute("UPDATE jobs SET status=?,result_json=?,error=?,updated_at=? WHERE id=?",
                        (status, json.dumps(result or {}, ensure_ascii=False), error, now(), job_id))

    def job_progress(self, job_id: int, progress: dict[str, Any]) -> None:
        with self.connect() as con:
            con.execute("UPDATE jobs SET result_json=?,updated_at=? WHERE id=? AND status='RUNNING'",
                        (json.dumps(progress, ensure_ascii=False), now(), job_id))

    def fail_interrupted_jobs(self) -> None:
        with self.connect() as con:
            con.execute("""UPDATE sources SET status='ERROR',error='服务重启导致后台任务中断，请重新点击操作'
                           WHERE id IN (SELECT entity_id FROM jobs WHERE status IN ('QUEUED','RUNNING'))""")
            con.execute("""UPDATE jobs SET status='FAILED',error='服务重启导致任务中断，请重新点击操作',updated_at=?
                           WHERE status IN ('QUEUED','RUNNING')""", (now(),))

    def add_candidate(self, data: dict[str, Any]) -> tuple[int, bool]:
        with self.connect() as con:
            row = con.execute("SELECT id FROM candidate_shots WHERE source_id=? AND start_time=? AND end_time=?",
                              (data["source_id"], data["start_time"], data["end_time"])).fetchone()
            if row:
                return int(row["id"]), False
            cur = con.execute(
                """INSERT INTO candidate_shots
                (source_id,start_time,end_time,duration,proxy_path,candidate_bucket,candidate_unit,
                 material_type,duration_bucket,score,status,representative_hash,facts_json,created_at)
                 VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (data["source_id"], data["start_time"], data["end_time"], data["duration"], data.get("proxy_path"),
                 data.get("candidate_bucket"), data.get("candidate_unit"),
                 data.get("material_type"), data.get("duration_bucket"), data.get("score", 0),
                 data.get("status", "WAITING_REVIEW"), data.get("representative_hash"),
                 json.dumps(data.get("facts", {}), ensure_ascii=False), now()))
            return int(cur.lastrowid), True

    def trim_candidate(self, candidate_id: int, start_time: float, end_time: float,
                       duration_bucket: str | None, facts: dict[str, Any]) -> None:
        with self.connect() as con:
            row = con.execute("SELECT source_id,status FROM candidate_shots WHERE id=?", (candidate_id,)).fetchone()
            if not row:
                raise KeyError("候选不存在")
            if row["status"] != "WAITING_REVIEW":
                raise ValueError("只有待人工审核的候选可以调整边界")
            if con.execute("SELECT 1 FROM final_clips WHERE candidate_id=?", (candidate_id,)).fetchone():
                raise ValueError("已经进入最终处理的候选不能再调整边界")
            duplicate = con.execute("""SELECT id FROM candidate_shots
                                       WHERE source_id=? AND start_time=? AND end_time=? AND id<>?""",
                                    (row["source_id"], start_time, end_time, candidate_id)).fetchone()
            if duplicate:
                raise ValueError(f"相同时间范围已存在候选 #{duplicate['id']}")
            con.execute("""UPDATE candidate_shots
                           SET start_time=?,end_time=?,duration=?,duration_bucket=?,facts_json=?
                           WHERE id=?""",
                        (start_time, end_time, end_time - start_time, duration_bucket,
                         json.dumps(facts, ensure_ascii=False), candidate_id))

    def clear_replaceable_candidates(self, source_id: int) -> int:
        """Remove stale machine-only slices before re-running scene detection."""
        with self.connect() as con:
            rows = con.execute("""SELECT c.id FROM candidate_shots c
                                  WHERE c.source_id=?
                                    AND NOT EXISTS (SELECT 1 FROM final_clips f WHERE f.candidate_id=c.id)
                                    AND NOT EXISTS (
                                        SELECT 1 FROM reviews r WHERE r.candidate_id=c.id
                                          AND r.decision IN ('ACCEPT','REJECT')
                                          AND COALESCE(r.notes,'')<>'程序确定性硬规则自动淘汰'
                                    )""", (source_id,)).fetchall()
            ids = [int(row["id"]) for row in rows]
            if not ids:
                return 0
            placeholders = ",".join("?" for _ in ids)
            con.execute(f"DELETE FROM rule_results WHERE candidate_id IN ({placeholders})", ids)
            con.execute(f"DELETE FROM reviews WHERE candidate_id IN ({placeholders})", ids)
            con.execute(f"DELETE FROM candidate_shots WHERE id IN ({placeholders})", ids)
            return len(ids)

    def candidate_part_number(self, candidate_id: int) -> int:
        with self.connect() as con:
            row = con.execute("SELECT source_id,start_time,id FROM candidate_shots WHERE id=?", (candidate_id,)).fetchone()
            if not row:
                raise KeyError("候选不存在")
            return int(con.execute("""SELECT COUNT(*) FROM candidate_shots
                                      WHERE source_id=? AND (start_time<? OR (start_time=? AND id<=?))""",
                                   (row["source_id"], row["start_time"], row["start_time"], row["id"])).fetchone()[0])

    def candidate_hash_exists(self, representative_hash: str) -> bool:
        with self.connect() as con:
            return con.execute("SELECT 1 FROM candidate_shots WHERE representative_hash=? LIMIT 1", (representative_hash,)).fetchone() is not None

    def get_candidate(self, candidate_id: int) -> dict[str, Any] | None:
        with self.connect() as con:
            row = con.execute("""SELECT c.*,s.url source_url,s.title source_title,s.uploader, s.original_path,
                                 s.platform,s.video_id FROM candidate_shots c JOIN sources s ON s.id=c.source_id WHERE c.id=?""",
                              (candidate_id,)).fetchone()
            return self._dict(row)

    def save_camera_advice(self, candidate_id: int, start: float, end: float, advice: dict) -> None:
        self.save_visual_advice(candidate_id, start, end, "camera_motion", advice)

    def save_visual_advice(self, candidate_id: int, start: float, end: float, key: str, advice: dict) -> None:
        if key not in {"camera_motion", "operator_framing"}:
            raise ValueError("未知视觉提示类型")
        with self.connect() as con:
            con.execute("BEGIN IMMEDIATE")
            row = con.execute("SELECT status,start_time,end_time,facts_json FROM candidate_shots WHERE id=?", (candidate_id,)).fetchone()
            if not row or row["status"] != "WAITING_REVIEW" or (row["start_time"], row["end_time"]) != (start, end):
                raise ValueError("候选状态或边界已改变，检测结果未保存，请刷新后再试")
            facts = json.loads(row["facts_json"] or "{}")
            facts[key] = advice
            con.execute("UPDATE candidate_shots SET facts_json=? WHERE id=?", (json.dumps(facts, ensure_ascii=False), candidate_id))

    @staticmethod
    def _camera_condition(camera: str | None) -> tuple[str, list]:
        if not camera:
            return "", []
        field = "COALESCE(json_extract(c.facts_json, '$.camera_motion.status'), 'UNTESTED')"
        if camera == "FIXED":
            return field + " IN ('FIXED','SHAKE')", []
        if camera not in {"ZOOM", "MOVING", "MIXED", "UNKNOWN", "UNTESTED"}:
            raise ValueError("未知运镜筛选分类")
        return field + "=?", [camera]

    @classmethod
    def _candidate_filters(cls, status: str | None = None, bucket: str | None = None,
                           camera: str | None = None, unit: str | None = None) -> tuple[str, list[Any]]:
        conditions, args = (["c.status=?"], [status]) if status else ([], [])
        bucket_condition, bucket_args = cls._bucket_condition("c.candidate_unit", bucket)
        if bucket_condition:
            if bucket == "unassigned":
                bucket_condition = "((c.candidate_bucket IS NULL OR c.candidate_bucket='') AND " + bucket_condition + ")"
            else:
                bucket_condition = "(c.candidate_bucket=? OR " + bucket_condition + ")"
                bucket_args = [bucket] + bucket_args
            conditions.append(bucket_condition)
            args.extend(bucket_args)
        camera_condition, camera_args = cls._camera_condition(camera)
        if camera_condition:
            conditions.append(camera_condition)
            args.extend(camera_args)
        if unit:
            conditions.append("c.candidate_unit=?")
            args.append(unit)
        return ("WHERE " + " AND ".join(conditions) if conditions else ""), args

    def count_candidates(self, status: str | None = None, bucket: str | None = None,
                         camera: str | None = None, unit: str | None = None) -> int:
        where, args = self._candidate_filters(status, bucket, camera, unit)
        with self.connect() as con:
            return int(con.execute(f"SELECT COUNT(*) FROM candidate_shots c {where}", args).fetchone()[0])

    def list_candidates(self, status: str | None = None, limit: int = 100, offset: int = 0,
                        bucket: str | None = None, camera: str | None = None,
                        unit: str | None = None) -> list[dict[str, Any]]:
        where, args = self._candidate_filters(status, bucket, camera, unit)
        with self.connect() as con:
            rows = con.execute(f"""SELECT c.*,s.title source_title,s.url source_url,
                                    (SELECT notes FROM reviews r WHERE r.candidate_id=c.id ORDER BY r.id DESC LIMIT 1) rejection_reason
                                    FROM candidate_shots c
                                    JOIN sources s ON s.id=c.source_id {where}
                                    ORDER BY c.score DESC,c.id ASC LIMIT ? OFFSET ?""", args + [limit, offset]).fetchall()
            return [dict(r) for r in rows]

    @staticmethod
    def _final_where(state: str) -> str:
        if state == "pending":
            return "c.status='ACCEPTED' OR (f.qa_status='PASS' AND f.exported_at IS NULL)"
        elif state == "processed":
            return "f.qa_status='PASS' AND f.exported_at IS NOT NULL"
        raise ValueError("最终处理列表状态必须是 pending 或 processed")

    def count_final_candidates(self, state: str) -> int:
        where = self._final_where(state)
        with self.connect() as con:
            return int(con.execute(f"""SELECT COUNT(*) FROM candidate_shots c
                                       LEFT JOIN final_clips f ON f.candidate_id=c.id
                                       WHERE {where}""").fetchone()[0])

    def list_final_candidates(self, state: str, limit: int = 20, offset: int = 0) -> list[dict[str, Any]]:
        where = self._final_where(state)
        with self.connect() as con:
            rows = con.execute(f"""SELECT c.*,s.title source_title,s.url source_url,
                                    f.qa_status,f.final_path,f.exported_at,f.width,f.height,f.qa_json
                                    FROM candidate_shots c JOIN sources s ON s.id=c.source_id
                                    LEFT JOIN final_clips f ON f.candidate_id=c.id
                                    WHERE {where}
                                    ORDER BY COALESCE(f.exported_at,c.created_at) DESC,c.id DESC LIMIT ? OFFSET ?""",
                               (limit, offset)).fetchall()
            return [dict(r) for r in rows]

    def save_rule_results(self, candidate_id: int, results: list[dict[str, Any]], stage: str = "candidate") -> None:
        with self.connect() as con:
            con.execute("DELETE FROM rule_results WHERE candidate_id=? AND stage=?", (candidate_id, stage))
            con.executemany(
                """INSERT INTO rule_results(candidate_id,stage,rule_id,status,reason,evidence_json,deterministic,checked_at)
                   VALUES(?,?,?,?,?,?,?,?)""",
                [(candidate_id, stage, r["rule_id"], r["status"], r["reason"], json.dumps(r.get("evidence", {}), ensure_ascii=False),
                  int(bool(r.get("deterministic"))), now()) for r in results])

    def get_rule_results(self, candidate_id: int, stage: str = "candidate") -> list[dict[str, Any]]:
        with self.connect() as con:
            return [dict(r) for r in con.execute("SELECT * FROM rule_results WHERE candidate_id=? AND stage=? ORDER BY rule_id", (candidate_id, stage))]

    def review(self, candidate_id: int, payload: dict[str, Any]) -> int:
        decision = payload["decision"].upper()
        if decision not in {"ACCEPT", "REJECT", "RESTORE"}:
            raise ValueError("decision 必须是 ACCEPT、REJECT 或 RESTORE")
        new_status = {"ACCEPT": "ACCEPTED", "REJECT": "REJECTED", "RESTORE": "WAITING_REVIEW"}[decision]
        with self.connect() as con:
            cur = con.execute(
                """INSERT INTO reviews(candidate_id,decision,final_bucket,final_unit,notes,manual_rule_overrides,reviewed_at)
                   VALUES(?,?,?,?,?,?,?)""",
                (candidate_id, decision, payload.get("final_bucket"), payload.get("final_unit"),
                 payload.get("notes", ""), json.dumps(payload.get("manual_rule_overrides", {}), ensure_ascii=False), now()))
            con.execute("""UPDATE candidate_shots
                           SET status=?,candidate_bucket=COALESCE(?,candidate_bucket),candidate_unit=COALESCE(?,candidate_unit),
                               delivery_description=COALESCE(?,delivery_description)
                           WHERE id=?""",
                        (new_status, payload.get("final_bucket"), payload.get("final_unit"),
                         str(payload.get("delivery_description") or "").strip() or None, candidate_id))
            return int(cur.lastrowid)

    def reject_waiting_by_source(self, source_id: int, notes: str) -> list[int]:
        """Reject every WAITING_REVIEW candidate of one source; other states are untouched."""
        stamp = now()
        with self.connect() as con:
            ids = [int(row["id"]) for row in con.execute(
                "SELECT id FROM candidate_shots WHERE source_id=? AND status='WAITING_REVIEW' ORDER BY id",
                (source_id,))]
            con.executemany(
                """INSERT INTO reviews(candidate_id,decision,notes,manual_rule_overrides,reviewed_at)
                   VALUES(?,'REJECT',?,'{}',?)""", [(cid, notes, stamp) for cid in ids])
            if ids:
                placeholders = ",".join("?" for _ in ids)
                con.execute(f"UPDATE candidate_shots SET status='REJECTED' WHERE id IN ({placeholders})", ids)
            return ids

    def add_traffic(self, kind: str, byte_count: int, source_id: int | None = None, direction: str = "download") -> None:
        with self.connect() as con:
            con.execute("INSERT INTO traffic_stats(direction,kind,bytes,source_id,created_at) VALUES(?,?,?,?,?)",
                        (direction, kind, int(byte_count), source_id, now()))

    def dashboard(self) -> dict[str, Any]:
        today = datetime.now().date().isoformat()
        with self.connect() as con:
            counts = {r["status"]: r["n"] for r in con.execute("SELECT status,COUNT(*) n FROM candidate_shots GROUP BY status")}
            source_count = con.execute("SELECT COUNT(*) n FROM sources").fetchone()["n"]
            traffic = con.execute("""SELECT COALESCE(SUM(bytes),0) total,
                    COALESCE(SUM(CASE WHEN kind='proxy' THEN bytes ELSE 0 END),0) proxy,
                    COALESCE(SUM(CASE WHEN kind='final' THEN bytes ELSE 0 END),0) final,
                    COALESCE(SUM(CASE WHEN substr(created_at,1,10)=? THEN bytes ELSE 0 END),0) today
                    FROM traffic_stats WHERE direction='download'""", (today,)).fetchone()
            qa_passed = con.execute("SELECT COUNT(*) n FROM final_clips WHERE qa_status='PASS'").fetchone()["n"]
            reviewed_today = con.execute("""SELECT COUNT(*) n FROM reviews
                WHERE substr(reviewed_at,1,10)=? AND COALESCE(notes,'')<>'程序确定性硬规则自动淘汰'""", (today,)).fetchone()["n"]
            accepted = counts.get("ACCEPTED", 0) + counts.get("FINAL_DOWNLOADING", 0) + counts.get("FINAL_READY", 0) + counts.get("FINAL_QA", 0) + counts.get("DELIVERABLE", 0)
            return {"sources": source_count, "counts": counts, "accepted": accepted, "qa_passed": qa_passed,
                    "reviewed_today": reviewed_today, "traffic": dict(traffic), "estimated_income": accepted * 30}

    def quota_state(self) -> list[dict[str, Any]]:
        with self.connect() as con:
            rows = con.execute("""SELECT candidate_bucket bucket,duration_bucket,COUNT(*) count
                                  FROM candidate_shots WHERE status IN ('ACCEPTED','FINAL_DOWNLOADING','FINAL_READY','FINAL_QA','DELIVERABLE')
                                  GROUP BY candidate_bucket,duration_bucket""").fetchall()
        return [dict(r) for r in rows]

    def create_final_clip(self, candidate_id: int, **fields: Any) -> int:
        with self.connect() as con:
            cur = con.execute("""INSERT INTO final_clips(candidate_id,original_path,final_path,duration,width,height,fps,has_audio,qa_status,deliverable_status,qa_json,created_at)
                                 VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
                                 ON CONFLICT(candidate_id) DO UPDATE SET original_path=excluded.original_path,final_path=excluded.final_path,
                                 duration=excluded.duration,width=excluded.width,height=excluded.height,fps=excluded.fps,has_audio=excluded.has_audio,
                                 qa_status=excluded.qa_status,deliverable_status=excluded.deliverable_status,qa_json=excluded.qa_json,
                                 exported_at=NULL
                                 RETURNING id""",
                              (candidate_id, fields.get("original_path"), fields.get("final_path"), fields.get("duration"), fields.get("width"),
                               fields.get("height"), fields.get("fps"), fields.get("has_audio"), fields.get("qa_status", "PENDING"),
                               fields.get("deliverable_status", "PENDING"), json.dumps(fields.get("qa", {}), ensure_ascii=False), now()))
            return int(cur.fetchone()[0])

    def reserve_delivery_filename(self, candidate_id: int, unit: str, description: str) -> dict[str, Any]:
        """Reserve and persist the next per-unit delivery number for a candidate."""
        description = description.strip(" ._") or "视频片段"
        with self.connect() as con:
            row = con.execute("""SELECT delivery_unit,delivery_sequence,delivery_filename
                                 FROM final_clips WHERE candidate_id=?""", (candidate_id,)).fetchone()
            if not row:
                raise ValueError("最终片段记录不存在，无法分配交付序号")
            existing_unit = str(row["delivery_unit"] or "")
            existing_sequence = int(row["delivery_sequence"] or 0)
            existing_filename = str(row["delivery_filename"] or "")
            if existing_unit == unit and existing_sequence > 0:
                filename = existing_filename or f"{unit}_{existing_sequence:03d}_{description}.mp4"
                if not existing_filename:
                    con.execute("UPDATE final_clips SET delivery_filename=? WHERE candidate_id=?",
                                (filename, candidate_id))
                return {"unit": unit, "sequence": existing_sequence, "filename": filename}

            sequence = int(con.execute("""INSERT INTO delivery_sequences(unit,last_sequence) VALUES(?,1)
                                          ON CONFLICT(unit) DO UPDATE SET last_sequence=last_sequence+1
                                          RETURNING last_sequence""", (unit,)).fetchone()[0])
            filename = f"{unit}_{sequence:03d}_{description}.mp4"
            con.execute("""UPDATE final_clips
                           SET delivery_unit=?,delivery_sequence=?,delivery_filename=?
                           WHERE candidate_id=?""", (unit, sequence, filename, candidate_id))
            return {"unit": unit, "sequence": sequence, "filename": filename}

    def update_final_clip_delivery(self, candidate_id: int, final_path: str, filename: str,
                                   deliverable_status: str = "READY") -> None:
        with self.connect() as con:
            con.execute("""UPDATE final_clips
                           SET final_path=?,delivery_filename=?,deliverable_status=?
                           WHERE candidate_id=?""", (final_path, filename, deliverable_status, candidate_id))

    def delivery_filename_rows(self) -> list[dict[str, Any]]:
        with self.connect() as con:
            rows = con.execute("""SELECT f.candidate_id,f.final_path,f.delivery_unit,f.delivery_sequence,
                                  f.delivery_filename,f.deliverable_status,c.candidate_unit unit,
                                  c.candidate_bucket bucket,c.delivery_description,s.title source_title
                                  FROM final_clips f
                                  JOIN candidate_shots c ON c.id=f.candidate_id
                                  JOIN sources s ON s.id=c.source_id
                                  WHERE f.qa_status='PASS' AND f.delivery_sequence IS NOT NULL
                                  ORDER BY f.delivery_unit,f.delivery_sequence""").fetchall()
            return [dict(row) for row in rows]

    def mark_delivery_exported(self, candidate_ids: list[int], exported_at: str | None = None) -> None:
        if not candidate_ids:
            return
        placeholders = ",".join("?" for _ in candidate_ids)
        with self.connect() as con:
            con.execute(f"""UPDATE final_clips SET exported_at=COALESCE(exported_at,?)
                            WHERE candidate_id IN ({placeholders})""",
                        [exported_at or now(), *candidate_ids])

    def restore_exported_candidate(self, candidate_id: int) -> None:
        with self.connect() as con:
            row = con.execute("SELECT qa_status,exported_at FROM final_clips WHERE candidate_id=?", (candidate_id,)).fetchone()
            if not row or row["qa_status"] != "PASS" or not row["exported_at"]:
                raise ValueError("该候选不在已处理列表中")
            con.execute("UPDATE final_clips SET exported_at=NULL WHERE candidate_id=?", (candidate_id,))

    def delivery_rows(self) -> list[dict[str, Any]]:
        with self.connect() as con:
            rows = con.execute("""SELECT c.id candidate_id,c.candidate_bucket bucket,c.candidate_unit unit,
                c.start_time,c.end_time,c.duration candidate_duration,c.duration_bucket,
                c.delivery_description,
                s.url source_url,s.platform,s.video_id,s.title source_title,
                f.final_path,f.delivery_unit,f.delivery_sequence,f.delivery_filename,
                f.duration,f.width,f.height,f.fps,f.has_audio,f.qa_status,f.deliverable_status,
                f.created_at,f.exported_at,
                (SELECT notes FROM reviews r WHERE r.candidate_id=c.id ORDER BY r.id DESC LIMIT 1) notes
                FROM final_clips f JOIN candidate_shots c ON c.id=f.candidate_id JOIN sources s ON s.id=c.source_id
                WHERE f.qa_status='PASS' ORDER BY c.candidate_bucket,c.id""").fetchall()
            return [dict(r) for r in rows]
