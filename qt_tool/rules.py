from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any


class RuleStatus(StrEnum):
    PASS = "PASS"
    FAIL = "FAIL"
    UNKNOWN = "UNKNOWN"
    NOT_APPLICABLE = "NOT_APPLICABLE"
    CONFLICT = "CONFLICT"


@dataclass(frozen=True)
class RuleResult:
    rule_id: str
    status: RuleStatus
    reason: str
    evidence: dict[str, Any]
    deterministic: bool = False

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["status"] = self.status.value
        return data


def _load_json_yaml(path: Path) -> dict[str, Any]:
    # Files intentionally use JSON syntax, which is a valid YAML 1.2 subset.
    return json.loads(path.read_text(encoding="utf-8"))


class RuleEngine:
    def __init__(self, rules_path: Path, conflicts_path: Path):
        self.rules = _load_json_yaml(rules_path)
        self.conflicts = _load_json_yaml(conflicts_path)
        for conflict in self.conflicts.get("conflicts", []):
            if conflict.get("id") == "CONFLICT-001" and conflict.get("resolved"):
                for unit in conflict.get("detailed_but_missing_from_list", []):
                    if unit in self.rules["units"]:
                        self.rules["units"][unit]["requires_confirmation"] = False

    @property
    def buckets(self) -> dict[str, Any]:
        return self.rules["buckets"]

    @property
    def units(self) -> dict[str, Any]:
        return self.rules["units"]

    @property
    def redlines(self) -> dict[str, Any]:
        return self.rules["redlines"]

    def duration_bucket(self, seconds: float | None) -> tuple[str | None, RuleStatus, str]:
        if seconds is None or not math.isfinite(seconds):
            return None, RuleStatus.UNKNOWN, "缺少可靠时长"
        if seconds < 5.0:
            return None, RuleStatus.FAIL, "时长不足 5 秒"
        if math.isclose(seconds, 15.0, abs_tol=0.001) or math.isclose(seconds, 30.0, abs_tol=0.001):
            return None, RuleStatus.CONFLICT, "PDF 的时长档位边界在该值重叠，需人工确认"
        if seconds < 15.0:
            return "short", RuleStatus.PASS, "短档 5-15 秒（避开 15.0 秒边界）"
        if seconds < 30.0:
            return "medium", RuleStatus.PASS, "中档 15-30 秒（避开边界）"
        return "long", RuleStatus.PASS, "长档 30 秒以上；超过 60 秒仍计入长档"

    def evaluate(self, facts: dict[str, Any]) -> list[RuleResult]:
        unit = facts.get("unit")
        bucket = facts.get("bucket") or (unit.split(".", 1)[0] if unit else None)
        results: list[RuleResult] = []
        results.append(self._r1(facts))
        results.append(self._r2(facts))
        results.extend(self._unknown("R3", "稳定性、畸变和运动模糊需人工/视觉分析"))
        results.extend(self._unknown("R4", "水印、字幕、UI 等需人工/视觉分析"))
        results.append(self._r5(facts))
        results.extend(self._unknown("R6", "穿模、瞬移和特效边界需人工/视觉分析"))
        results.extend(self._unknown("R7", "内容合规需人工审核"))
        results.append(self._r8(facts))
        results.append(self._r9(facts, bucket))
        results.append(self._r10(facts, unit))
        results.append(self._r11(facts))
        results.extend(self._unknown("R12", "背景虚化属于低置信度视觉判断，不自动拒绝"))
        results.append(self._r13(facts, unit))
        results.extend(self._unknown("R14", "活性物体需人工/视觉分析"))
        results.extend(self._unknown("R15", "变焦需人工/时序视觉分析"))
        results.append(self._r16(facts, bucket))
        results.append(self._r17(facts))
        if bucket == "T6":
            results.append(self._t6_orbit(facts, unit))
        results.append(self._fps(facts, bucket))
        results.extend(self._file_specs(facts))
        return results

    def _unknown(self, rule_id: str, reason: str) -> list[RuleResult]:
        return [RuleResult(rule_id, RuleStatus.UNKNOWN, reason, {}, False)]

    def _r1(self, facts: dict[str, Any]) -> RuleResult:
        count = facts.get("shot_count")
        if count is None:
            return RuleResult("R1", RuleStatus.UNKNOWN, "尚未进行镜头检测", {})
        if int(count) == 1:
            return RuleResult("R1", RuleStatus.PASS, "检测到单一连续镜头", {"shot_count": count}, True)
        return RuleResult("R1", RuleStatus.FAIL, "检测到多个镜头", {"shot_count": count}, True)

    def _r2(self, facts: dict[str, Any]) -> RuleResult:
        width, height = facts.get("width"), facts.get("height")
        if not width or not height:
            return RuleResult("R2", RuleStatus.UNKNOWN, "缺少可靠分辨率", {})
        ratio = float(width) / float(height)
        status = RuleStatus.PASS if 1.5 <= ratio <= 2.0 else RuleStatus.FAIL
        return RuleResult("R2", status, f"宽高比 {ratio:.3f}，要求 1.5-2.0", {"width": width, "height": height, "ratio": ratio}, True)

    def _r5(self, facts: dict[str, Any]) -> RuleResult:
        has_audio = facts.get("has_audio")
        silence_ratio = facts.get("silence_ratio")
        if has_audio is False:
            return RuleResult("R5", RuleStatus.FAIL, "文件没有音轨", {"has_audio": False}, True)
        if has_audio is None:
            return RuleResult("R5", RuleStatus.UNKNOWN, "尚未检测音轨", {})
        if silence_ratio is None:
            return RuleResult("R5", RuleStatus.UNKNOWN, "有音轨，但尚未可靠检测全程静默", {"has_audio": True})
        if silence_ratio >= 0.98:
            return RuleResult("R5", RuleStatus.FAIL, "音轨几乎全程静默", {"silence_ratio": silence_ratio}, True)
        return RuleResult("R5", RuleStatus.PASS, "存在音轨且未检测到全程静默", {"silence_ratio": silence_ratio}, True)

    def _r11(self, facts: dict[str, Any]) -> RuleResult:
        check = facts.get("subject_check")
        evidence = {
            "detector": facts.get("subject_detector"),
            "split_applied": bool(facts.get("subject_split_applied")),
            "loss_intervals": facts.get("subject_loss_intervals") or [],
        }
        if check == "PASS":
            reason = "人物主体连续性检测通过"
            if evidence["split_applied"]:
                reason = "检测到主体持续离场，已按安全边界切分；当前候选仅保留主体可见区间"
            # This is a useful visual pre-filter, but remains non-deterministic:
            # the reviewer still confirms identity and semantic relevance.
            return RuleResult("R11", RuleStatus.PASS, reason, evidence, False)
        if check == "NO_SEGMENT":
            # Same non-deterministic UNKNOWN as any other unconfirmed subject,
            # only with the finding spelled out for the reviewer.
            return RuleResult("R11", RuleStatus.UNKNOWN,
                              "主体离场但无 ≥5s 子段，保留整段待人工", evidence, False)
        if check == "SKIPPED_SHOT_CHANGE":
            return RuleResult("R11", RuleStatus.UNKNOWN, "R1 检出镜头切换，主体检测未执行", evidence, False)
        if facts.get("subject_trim_required"):
            return RuleResult("R11", RuleStatus.UNKNOWN,
                              "检测到主体中途持续离场，需要重新切分；不判整条素材失败", evidence, False)
        return RuleResult("R11", RuleStatus.UNKNOWN, "主体锚定仍需人工确认", evidence, False)

    def _r8(self, facts: dict[str, Any]) -> RuleResult:
        playable = facts.get("playable")
        if playable is None:
            return RuleResult("R8", RuleStatus.UNKNOWN, "尚未探测文件", {})
        return RuleResult("R8", RuleStatus.PASS if playable else RuleStatus.FAIL, "文件可正常探测" if playable else "文件无法正常探测", {"playable": playable}, True)

    def _r9(self, facts: dict[str, Any], bucket: str | None) -> RuleResult:
        source_type = str(facts.get("source_type") or "").upper()
        if source_type != "FINAL":
            return RuleResult("R9", RuleStatus.UNKNOWN,
                              "代理资源仅用于切镜和预览；分辨率将在下载原视频后按最终源判定",
                              {"source_type": source_type or "UNKNOWN"}, False)
        width, height = facts.get("width"), facts.get("height")
        if not width or not height or not bucket:
            return RuleResult("R9", RuleStatus.UNKNOWN, "分辨率或桶信息不足；原生分辨率仍需人工核验", {})
        min_w, min_h = ((1920, 1080) if bucket == "T9" else (2560, 1440))
        if width < min_w or height < min_h:
            return RuleResult("R9", RuleStatus.FAIL, f"分辨率 {width}x{height} 低于 {bucket} 最低规格 {min_w}x{min_h}", {"width": width, "height": height}, True)
        return RuleResult("R9", RuleStatus.UNKNOWN, "分辨率达标，但清晰度和是否升源仍需人工确认", {"width": width, "height": height}, False)

    def _r10(self, facts: dict[str, Any], unit: str | None) -> RuleResult:
        slow = facts.get("intentional_slow_motion")
        if slow is True and unit == "T8.4":
            return RuleResult("R10", RuleStatus.NOT_APPLICABLE, "T8.4 对刻意慢动作摄影豁免", {"unit": unit}, True)
        if slow is True:
            return RuleResult("R10", RuleStatus.FAIL, "非 T8.4 的刻意慢动作违反 R10", {"unit": unit}, True)
        return RuleResult("R10", RuleStatus.UNKNOWN, "起止、卡顿、跳帧、闪屏与其他变速需人工确认", {"unit": unit})

    def _r13(self, facts: dict[str, Any], unit: str | None) -> RuleResult:
        aerial = facts.get("aerial")
        if aerial is None:
            return RuleResult("R13", RuleStatus.UNKNOWN, "尚未判断是否为非地面视角", {"unit": unit})
        if not aerial:
            return RuleResult("R13", RuleStatus.PASS, "声明为地面视角", {"unit": unit}, True)
        if unit == "T8.2":
            return RuleResult("R13", RuleStatus.CONFLICT, "T8.2 合格标准包含航拍，但 R13 未列为豁免", {"conflict_id": "CONFLICT-002"}, True)
        if unit in {"T1.7", "T6.3", "T6.8"}:
            return RuleResult("R13", RuleStatus.NOT_APPLICABLE, f"{unit} 是 R13 明确豁免单元", {"unit": unit}, True)
        return RuleResult("R13", RuleStatus.FAIL, "非地面视角且不在 R13 豁免范围", {"unit": unit}, True)

    def _r16(self, facts: dict[str, Any], bucket: str | None) -> RuleResult:
        kind = facts.get("material_type")
        if not bucket or not kind:
            return RuleResult("R16", RuleStatus.UNKNOWN, "缺少桶或素材类型", {})
        if kind == "gameplay":
            return RuleResult("R16", RuleStatus.FAIL, "所有桶均禁止游戏录制和游戏过场", {"material_type": kind}, True)
        if bucket == "T9" and kind != "animation":
            return RuleResult("R16", RuleStatus.FAIL, "T9 必须为动画/风格化", {"material_type": kind}, True)
        if bucket != "T9" and kind != "live_action":
            return RuleResult("R16", RuleStatus.FAIL, "T1-T8 必须为实拍（可含影视特效合成）", {"material_type": kind}, True)
        return RuleResult("R16", RuleStatus.PASS, "声明素材类型与桶要求一致", {"material_type": kind}, True)

    def _r17(self, facts: dict[str, Any]) -> RuleResult:
        seconds = facts.get("duration")
        _, status, reason = self.duration_bucket(float(seconds) if seconds is not None else None)
        return RuleResult("R17", status, reason, {"duration": seconds}, status in {RuleStatus.PASS, RuleStatus.FAIL, RuleStatus.CONFLICT})

    def _t6_orbit(self, facts: dict[str, Any], unit: str | None) -> RuleResult:
        if unit in {"T6.3", "T6.8"}:
            return RuleResult("T6_ORBIT", RuleStatus.NOT_APPLICABLE, f"{unit} 明确豁免环视/回访", {"unit": unit}, True)
        value = facts.get("has_orbit_or_revisit")
        if value is None:
            return RuleResult("T6_ORBIT", RuleStatus.UNKNOWN, "T6 除 6.3/6.8 外必须含 360° 环视或二次回访，需人工确认", {"unit": unit})
        if value:
            return RuleResult("T6_ORBIT", RuleStatus.PASS, "已确认包含环视或二次回访", {"unit": unit}, True)
        return RuleResult("T6_ORBIT", RuleStatus.FAIL, "T6 非豁免单元缺少环视/回访", {"unit": unit}, True)

    def _fps(self, facts: dict[str, Any], bucket: str | None) -> RuleResult:
        if str(facts.get("source_type") or "").upper() == "PROXY":
            return RuleResult("SPEC_FPS", RuleStatus.UNKNOWN, "代理帧率不用于淘汰，最终按原片帧率判定", {})
        fps = facts.get("fps")
        if not bucket or fps is None or float(fps) <= 0:
            return RuleResult("SPEC_FPS", RuleStatus.UNKNOWN, "缺少可靠帧率", {})
        if bucket == "T9":
            return RuleResult("SPEC_FPS", RuleStatus.PASS, "T9 按原作帧率，不设统一下限", {"fps": fps}, True)
        status = RuleStatus.PASS if float(fps) >= 24 else RuleStatus.FAIL
        return RuleResult("SPEC_FPS", status, f"实拍桶帧率 {float(fps):.3f} fps，要求不低于 24 fps", {"fps": fps}, True)

    def _file_specs(self, facts: dict[str, Any]) -> list[RuleResult]:
        results: list[RuleResult] = []
        codec = str(facts.get("video_codec") or "").lower()
        if codec:
            ok = codec in {"h264", "hevc", "h265"}
            results.append(RuleResult("SPEC_CODEC", RuleStatus.PASS if ok else RuleStatus.FAIL,
                                      f"视频编码 {codec}；要求 H.264/H.265", {"video_codec": codec}, True))
        else:
            results.append(RuleResult("SPEC_CODEC", RuleStatus.UNKNOWN, "缺少视频编码信息", {}))
        black_ratio = facts.get("black_ratio")
        if black_ratio is None:
            results.append(RuleResult("QA_BLACK", RuleStatus.UNKNOWN, "尚未执行黑帧检测", {}))
        elif float(black_ratio) == 0:
            results.append(RuleResult("QA_BLACK", RuleStatus.PASS, "未检测到黑帧区间", {"black_ratio": 0}, True))
        else:
            results.append(RuleResult("QA_BLACK", RuleStatus.UNKNOWN, "检测到黑帧区间，需人工确认是否位于起止或属于误报",
                                      {"black_ratio": black_ratio}, False))
        return results

    def unit_gate(self, unit: str | None) -> RuleResult | None:
        if not unit:
            return None
        data = self.units.get(unit)
        if not data:
            return RuleResult("UNIT", RuleStatus.CONFLICT, "该单元没有第四章详细规则，不可自动使用", {"unit": unit}, True)
        if data.get("requires_confirmation"):
            return RuleResult("UNIT", RuleStatus.CONFLICT, "该单元超出第五章列示范围，需确认后生产", {"unit": unit, "conflict_id": "CONFLICT-001"}, True)
        return RuleResult("UNIT", RuleStatus.PASS, "单元有第四章详细规则，已在允许交付范围内", {"unit": unit}, True)

    @staticmethod
    def automatic_reject(results: list[RuleResult]) -> bool:
        return any(item.status == RuleStatus.FAIL and item.deterministic for item in results)
