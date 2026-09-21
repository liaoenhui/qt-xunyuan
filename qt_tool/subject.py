from __future__ import annotations

import statistics
from dataclasses import dataclass
from pathlib import Path
from typing import Any


# Enabled for every unit whose rulebook subject is a clearly visible human.
# Applying a person detector to animals, vehicles, hands, or first-person
# footage would create unsafe cuts, so units whose subject may be a pet, a
# vehicle, a crowd or a natural event stay out: T1.3/T1.6-T1.9 (vehicles),
# T3.1 (mostly POV hands and pets, and its pass criterion is one unbroken
# contact arc), T5.1/T5.2/T5.4-T5.6 (vehicles, weather, traffic, scenery),
# T5.7 (the subject is the crowd or an animal flock, not one person),
# T6.1-T6.5 (the place is the subject), T8.1-T8.3 (framing or animals).
# Footage that opens without a prominent person still degrades safely: the
# analyzer reports LOW_CONFIDENCE and never splits.
SUPPORTED_PERSON_UNITS = frozenset({
    "T1.1", "T1.2", "T1.4",
    "T3.2", "T3.3", "T3.4", "T3.5",
    "T5.3",
    "T6.6", "T6.7", "T6.8", "T6.9",
    "T8.4",
})

# Calibrated against 161 labelled clips. A 0.25s grid keeps a fast subject on
# the timeline between samples, and 3.5s is the shortest sustained absence that
# never cut a clip a reviewer had accepted; a 1.5s threshold chopped 6 of the 27
# accepted clips into unusable pieces. Both values are constructor arguments so
# a future re-calibration does not have to touch the analysis code.
SAMPLE_INTERVAL_SECONDS = 0.25
SUSTAINED_LOSS_SECONDS = 3.5


@dataclass(frozen=True)
class SubjectContinuity:
    available: bool
    reliable: bool
    status: str
    segments: tuple[tuple[float, float], ...]
    gaps: tuple[dict[str, float], ...] = ()
    evidence: dict[str, Any] | None = None
    boundary_advice: dict[str, dict[str, Any]] | None = None

    def facts_for(self, segment: tuple[float, float]) -> dict[str, Any]:
        evidence = dict(self.evidence or {})
        key = f"{segment[0]:.3f}:{segment[1]:.3f}"
        advice = dict((self.boundary_advice or {}).get(key, {}))
        return {
            "subject_check": "PASS" if self.reliable else "UNKNOWN",
            "subject_detector": evidence.get("method"),
            "subject_split_applied": self.status == "SPLIT",
            "subject_segment_start": round(segment[0], 3),
            "subject_segment_end": round(segment[1], 3),
            "subject_loss_intervals": list(self.gaps),
            "subject_detection_evidence": evidence,
            "analysis_segment_start": round(segment[0], 3),
            "analysis_segment_end": round(segment[1], 3),
            "boundary_suggestion": advice,
        }


def split_presence_samples(
    start: float,
    end: float,
    present_times: list[float],
    *,
    sample_interval: float = SAMPLE_INTERVAL_SECONDS,
    minimum_loss: float = SUSTAINED_LOSS_SECONDS,
    minimum_segment: float = 5.0,
) -> tuple[tuple[tuple[float, float], ...], tuple[dict[str, float], ...]]:
    """Split only on sustained absence, retaining every independently useful side.

    The boundary before a gap is placed half a sample after the last confirmed
    subject frame. A resumed segment starts at the first frame where the subject
    is confirmed again, preventing absent frames from leaking into the output.
    """
    times = sorted(t for t in present_times if start <= t <= end)
    if not times:
        return ((round(start, 3), round(end, 3)),), ()

    gaps: list[dict[str, float]] = []
    if times[0] - start >= minimum_loss:
        gaps.append({"start": round(start, 3), "end": round(times[0], 3),
                     "duration": round(times[0] - start, 3)})
    for previous, current in zip(times, times[1:]):
        missing_duration = current - previous - sample_interval
        if missing_duration >= minimum_loss:
            gap_start = min(end, previous + sample_interval / 2)
            gaps.append({"start": round(gap_start, 3), "end": round(current, 3),
                         "duration": round(current - gap_start, 3)})
    tail_start = times[-1] + sample_interval / 2
    if end - tail_start >= minimum_loss:
        gaps.append({"start": round(tail_start, 3), "end": round(end, 3),
                     "duration": round(end - tail_start, 3)})

    if not gaps:
        return ((round(start, 3), round(end, 3)),), ()

    segments: list[tuple[float, float]] = []
    cursor = start
    for gap in gaps:
        gap_start, gap_end = float(gap["start"]), float(gap["end"])
        if gap_start - cursor >= minimum_segment:
            segments.append((round(cursor, 3), round(gap_start, 3)))
        cursor = gap_end
    if end - cursor >= minimum_segment:
        segments.append((round(cursor, 3), round(end, 3)))
    return tuple(segments) or ((round(start, 3), round(end, 3)),), tuple(gaps)


def select_motion_valley(samples: list[tuple[float, float]]) -> tuple[float, float] | None:
    """Choose a low-motion point only when it precedes a sustained motion rise.

    A single noisy frame is deliberately insufficient.  Returning ``None`` is
    preferable to offering a plausible-looking but unsafe automatic trim.
    """
    if len(samples) < 7:
        return None
    smoothed: list[tuple[float, float]] = []
    for index, (at, _) in enumerate(samples):
        left, right = max(0, index - 1), min(len(samples), index + 2)
        smoothed.append((at, sum(score for _, score in samples[left:right]) / (right - left)))
    candidates: list[tuple[float, float, float]] = []
    for index in range(2, len(smoothed) - 4):
        at, score = smoothed[index]
        future = [value for _, value in smoothed[index + 1:min(len(smoothed), index + 11)]]
        elevated = [value for value in future if value / max(score, 0.001) >= 1.45]
        rise = (statistics.median(elevated) / max(score, 0.001)) if len(elevated) >= 3 else 0.0
        if rise >= 1.45:
            candidates.append((score, -at, rise))
    if candidates:
        score, negative_at, _ = min(candidates)
        return round(-negative_at, 3), round(score, 3)
    return None


class SubjectContinuityAnalyzer:
    """Conservative prominent-person continuity analysis for person-subject units."""

    VOC_PERSON_CLASS = 15

    def __init__(self, model_dir: Path, sample_interval: float = SAMPLE_INTERVAL_SECONDS,
                 minimum_loss: float = SUSTAINED_LOSS_SECONDS):
        self.model_dir = Path(model_dir)
        self.sample_interval = float(sample_interval)
        self.minimum_loss = float(minimum_loss)
        self.prototxt = self.model_dir / "mobilenet_ssd_deploy.prototxt"
        self.weights = self.model_dir / "mobilenet_ssd.caffemodel"
        self._cv2 = None
        self._np = None
        self._net = None

    @staticmethod
    def supports(unit: str | None) -> bool:
        return bool(unit in SUPPORTED_PERSON_UNITS)

    @property
    def available(self) -> bool:
        if not self.prototxt.is_file() or not self.weights.is_file():
            return False
        try:
            import cv2  # type: ignore
            import numpy as np  # type: ignore
        except ImportError:
            return False
        self._cv2, self._np = cv2, np
        return True

    def _network(self):
        if self._net is None:
            if not self.available:
                raise RuntimeError("主体检测组件未安装")
            self._net = self._cv2.dnn.readNetFromCaffe(str(self.prototxt), str(self.weights))
        return self._net

    def _person_area_ratios(self, frame) -> list[float]:
        cv2, np = self._cv2, self._np
        height, width = frame.shape[:2]
        blob = cv2.dnn.blobFromImage(cv2.resize(frame, (300, 300)), 0.007843, (300, 300), 127.5)
        net = self._network()
        net.setInput(blob)
        detections = net.forward()
        ratios: list[float] = []
        for index in range(detections.shape[2]):
            confidence = float(detections[0, 0, index, 2])
            class_id = int(detections[0, 0, index, 1])
            if class_id != self.VOC_PERSON_CLASS or confidence < 0.25:
                continue
            x1, y1, x2, y2 = detections[0, 0, index, 3:7] * np.array([width, height, width, height])
            box_width = max(0.0, min(float(width), x2) - max(0.0, x1))
            box_height = max(0.0, min(float(height), y2) - max(0.0, y1))
            ratios.append((box_width * box_height) / max(1.0, float(width * height)))
        return ratios

    def _motion_samples(self, path: Path, start: float, end: float, step: float = 0.1) -> list[tuple[float, float]]:
        cv2, np = self._cv2, self._np
        cap = cv2.VideoCapture(str(path))
        previous = None
        samples: list[tuple[float, float]] = []
        at = start
        try:
            while at <= end + 0.001:
                cap.set(cv2.CAP_PROP_POS_MSEC, at * 1000.0)
                ok, frame = cap.read()
                if not ok:
                    at += step
                    continue
                gray = cv2.resize(cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY), (320, 180)).astype(np.float32)
                if previous is not None:
                    shift, _ = cv2.phaseCorrelate(previous, gray)
                    transform = np.float32([[1, 0, shift[0]], [0, 1, shift[1]]])
                    aligned = cv2.warpAffine(previous, transform, (320, 180), flags=cv2.INTER_LINEAR,
                                             borderMode=cv2.BORDER_REFLECT)
                    samples.append((round(at, 3), float(np.mean(np.abs(gray - aligned)))))
                previous = gray
                at += step
        finally:
            cap.release()
        return samples

    def _boundary_advice(
        self,
        path: Path,
        segments: tuple[tuple[float, float], ...],
        gaps: tuple[dict[str, float], ...],
    ) -> dict[str, dict[str, Any]]:
        advice: dict[str, dict[str, Any]] = {}
        for segment_start, segment_end in segments:
            key = f"{segment_start:.3f}:{segment_end:.3f}"
            item: dict[str, Any] = {
                "suggestion_available": False,
                "suggested_start": round(segment_start, 3),
                "suggested_end": round(segment_end, 3),
                "start_risk": "REVIEW",
                "end_risk": "REVIEW",
                "confidence": "LOW",
                "reason": "起止动作完整性需要人工确认",
            }
            ends_at_loss = any(abs(float(gap["start"]) - segment_end) <= 0.3 for gap in gaps)
            starts_after_loss = any(abs(float(gap["end"]) - segment_start) <= 0.3 for gap in gaps)
            if ends_at_loss and segment_end - segment_start >= 8.0:
                # Only a small rollback is allowed.  Larger corrections should
                # remain a human editing decision, not an algorithmic guess.
                search_start = max(segment_start + 5.0, segment_end - 3.5)
                search_end = segment_end - 1.0
                valley = select_motion_valley(self._motion_samples(path, search_start, search_end))
                item.update({
                    "end_risk": "HIGH",
                    "confidence": "LOW",
                    "reason": "主体离场时动作可能尚未完成；未找到可靠转折点时仅提示风险，不自动建议裁剪",
                })
                if valley:
                    item.update({
                        "suggestion_available": True,
                        "suggested_end": round(valley[0], 3),
                        "confidence": "MEDIUM",
                        "reason": "主体离场时动作可能尚未完成；检测到离场前的低运动转折点，请播放确认后再采用",
                    })
                    item["motion_score"] = valley[1]
            if starts_after_loss:
                item["start_risk"] = "HIGH"
                item["reason"] += "；该段从主体重新出现处开始，需确认不是动作中段"
            advice[key] = item
        return advice

    def analyze(self, path: Path, start: float, end: float, unit: str | None) -> SubjectContinuity:
        original = ((round(start, 3), round(end, 3)),)
        if not self.supports(unit):
            return SubjectContinuity(False, False, "NOT_APPLICABLE", original,
                                     evidence={"method": "prominent_person", "reason": "该单元未启用人物主体切分"})
        if not self.available:
            return SubjectContinuity(False, False, "UNAVAILABLE", original,
                                     evidence={"method": "prominent_person", "reason": "缺少 OpenCV 或离线人物模型"})
        if end - start < 5.0:
            return SubjectContinuity(True, False, "TOO_SHORT", original,
                                     evidence={"method": "mobilenet_ssd_prominent_person"})

        cv2 = self._cv2
        cap = cv2.VideoCapture(str(path))
        if not cap.isOpened():
            return SubjectContinuity(True, False, "UNREADABLE", original,
                                     evidence={"method": "mobilenet_ssd_prominent_person"})

        sample_interval = self.sample_interval
        samples: list[tuple[float, float]] = []
        at = float(start)
        try:
            while at < end + 0.001:
                cap.set(cv2.CAP_PROP_POS_MSEC, at * 1000.0)
                ok, frame = cap.read()
                if ok:
                    areas = self._person_area_ratios(frame)
                    samples.append((round(at, 3), max(areas, default=0.0)))
                at += sample_interval
        finally:
            cap.release()

        seed_end = min(end, start + 5.0)
        seed_areas = [area for at, area in samples if at <= seed_end and area > 0]
        baseline = float(statistics.median(seed_areas)) if seed_areas else 0.0
        # A prominent tracked athlete should be appreciably larger than distant
        # spectators. The relative floor adapts to the subject's opening scale.
        prominence_floor = max(0.014, baseline * 0.30)
        present = [at for at, area in samples if area >= prominence_floor]
        seed_present = [at for at in present if at <= seed_end]
        # The confidence floor is expressed in seconds of confirmed presence, so
        # a denser sampling grid does not silently weaken it.
        need_seed = max(4, int(round(2.0 / sample_interval)))
        need_total = max(6, int(round(3.0 / sample_interval)))
        reliable = (len(seed_present) >= need_seed and len(present) >= need_total
                    and (not present or present[0] <= start + 1.0))
        evidence = {
            "method": "mobilenet_ssd_prominent_person",
            "sample_interval": sample_interval,
            "baseline_area_ratio": float(round(baseline, 5)),
            "prominence_floor": float(round(prominence_floor, 5)),
            "sample_count": len(samples),
            "present_count": len(present),
        }
        if not reliable:
            return SubjectContinuity(True, False, "LOW_CONFIDENCE", original, evidence=evidence)

        segments, gaps = split_presence_samples(start, end, present, sample_interval=sample_interval,
                                                minimum_loss=self.minimum_loss)
        status = "SPLIT" if len(segments) > 1 or segments != original else "PASS"
        boundary_advice = self._boundary_advice(path, segments, gaps)
        return SubjectContinuity(True, True, status, segments, gaps, evidence, boundary_advice)
