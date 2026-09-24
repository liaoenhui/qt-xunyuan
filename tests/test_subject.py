from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from qt_tool.rules import RuleEngine, RuleStatus
from qt_tool.subject import (NO_SEGMENT_NOTE, SAMPLE_INTERVAL_SECONDS, SUPPORTED_PERSON_UNITS,
                             SUSTAINED_LOSS_SECONDS, SubjectContinuityAnalyzer,
                             split_presence_samples)


ROOT = Path(__file__).resolve().parent.parent


class SupportedUnitTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.engine = RuleEngine(ROOT / "rules" / "qt_rules_v4.yaml", ROOT / "rules" / "conflicts.yaml")

    def test_every_supported_unit_exists_in_the_rulebook(self):
        self.assertTrue(SUPPORTED_PERSON_UNITS <= set(self.engine.units))

    def test_person_subject_units_are_covered(self):
        for unit in ("T1.1", "T1.2", "T1.4", "T3.2", "T3.3", "T3.4", "T3.5",
                     "T5.3", "T6.6", "T6.7", "T6.8", "T6.9", "T8.4"):
            self.assertTrue(SubjectContinuityAnalyzer.supports(unit), unit)

    def test_units_whose_subject_is_not_a_person_stay_disabled(self):
        # Vehicles, animals, crowds, places and pure POV hand work would all be
        # cut in the wrong place by a person detector.
        for unit in ("T1.3", "T1.6", "T1.7", "T1.8", "T1.9", "T3.1", "T5.1", "T5.2",
                     "T5.4", "T5.5", "T5.6", "T5.7", "T6.1", "T6.3", "T8.1", "T8.2", "T8.3"):
            self.assertFalse(SubjectContinuityAnalyzer.supports(unit), unit)

    def test_missing_unit_is_not_supported(self):
        self.assertFalse(SubjectContinuityAnalyzer.supports(None))
        self.assertFalse(SubjectContinuityAnalyzer.supports("T9.9"))


CAP_PROP_POS_MSEC = 0
CAP_PROP_POS_FRAMES = 1
CAP_PROP_FPS = 5


class FakeCapture:
    """Minimal cv2.VideoCapture stand-in that records how it was driven."""

    def __init__(self, fps: float, frame_count: int, opened: bool = True):
        self.fps = fps
        self.frame_count = frame_count
        self.opened = opened
        self.position = 0
        self.property_calls: list[tuple[int, float]] = []
        self.reads = 0
        self.grabs = 0
        self.released = False

    def isOpened(self) -> bool:
        return self.opened

    def get(self, prop: int) -> float:
        return float(self.fps) if prop == CAP_PROP_FPS else 0.0

    def set(self, prop: int, value: float) -> bool:
        self.property_calls.append((prop, value))
        if prop == CAP_PROP_POS_FRAMES:
            self.position = int(value)
        return True

    def grab(self) -> bool:
        if self.position >= self.frame_count:
            return False
        self.position += 1
        self.grabs += 1
        return True

    def read(self):
        if self.position >= self.frame_count:
            return False, None
        frame = self.position
        self.position += 1
        self.reads += 1
        return True, frame

    def release(self) -> None:
        self.released = True


class StubAnalyzer(SubjectContinuityAnalyzer):
    """Analyzer wired to a fake capture and a scripted person detector."""

    def __init__(self, capture: FakeCapture, present_until: float | None = None,
                 present_from: float | None = None):
        super().__init__(Path("missing"))
        self.capture = capture
        self.present_until = present_until
        self.present_from = present_from
        self._cv2 = SimpleNamespace(
            CAP_PROP_POS_MSEC=CAP_PROP_POS_MSEC,
            CAP_PROP_POS_FRAMES=CAP_PROP_POS_FRAMES,
            CAP_PROP_FPS=CAP_PROP_FPS,
            VideoCapture=lambda _path: capture,
        )

    @property
    def available(self) -> bool:
        return True

    def _person_area_ratios(self, frame) -> list[float]:
        at = frame / self.capture.fps
        visible = (self.present_until is None or at <= self.present_until) or \
                  (self.present_from is not None and at >= self.present_from)
        return [0.2] if visible else [0.0]


class SequentialDecodeTests(unittest.TestCase):
    def test_samples_are_read_sequentially_without_timestamp_seeks(self):
        capture = FakeCapture(fps=24, frame_count=480)
        analyzer = StubAnalyzer(capture)
        result = analyzer.analyze(Path("clip.mp4"), 0.0, 20.0, "T1.1")

        self.assertEqual(result.status, "PASS")
        self.assertEqual([prop for prop, _ in capture.property_calls], [CAP_PROP_POS_FRAMES])
        self.assertEqual(capture.property_calls[0], (CAP_PROP_POS_FRAMES, 0))
        # One decoded frame per sample, the other five frames only grabbed.
        self.assertEqual(capture.reads, 80)
        self.assertEqual(capture.grabs, 400)
        self.assertTrue(capture.released)

    def test_reported_sample_grid_matches_the_requested_interval(self):
        capture = FakeCapture(fps=24, frame_count=480)
        analyzer = StubAnalyzer(capture)
        result = analyzer.analyze(Path("clip.mp4"), 0.0, 20.0, "T1.1")
        evidence = result.evidence or {}
        self.assertAlmostEqual(evidence["sample_interval"], 0.25)
        self.assertEqual(evidence["sample_count"], 80)

    def test_unknown_frame_rate_is_reported_as_unreadable(self):
        capture = FakeCapture(fps=0, frame_count=480)
        analyzer = StubAnalyzer(capture)
        result = analyzer.analyze(Path("clip.mp4"), 0.0, 20.0, "T1.1")
        self.assertEqual(result.status, "UNREADABLE")
        self.assertTrue(capture.released)


class NoUsableSegmentTests(unittest.TestCase):
    def test_sustained_loss_without_a_usable_side_keeps_the_whole_range(self):
        capture = FakeCapture(fps=24, frame_count=336)
        analyzer = StubAnalyzer(capture, present_until=4.0)
        result = analyzer.analyze(Path("clip.mp4"), 0.0, 14.0, "T1.1")

        self.assertEqual(result.status, "NO_SEGMENT")
        self.assertTrue(result.reliable)
        self.assertEqual(result.segments, ((0.0, 14.0),))
        self.assertEqual(len(result.gaps), 1)

        facts = result.facts_for(result.segments[0])
        self.assertEqual(facts["subject_check"], "NO_SEGMENT")
        self.assertEqual(facts["subject_note"], NO_SEGMENT_NOTE)
        self.assertFalse(facts["subject_split_applied"])
        self.assertTrue(facts["subject_loss_intervals"])

    def test_a_usable_side_is_still_reported_as_a_split(self):
        capture = FakeCapture(fps=24, frame_count=480)
        analyzer = StubAnalyzer(capture, present_until=6.0, present_from=11.0)
        result = analyzer.analyze(Path("clip.mp4"), 0.0, 20.0, "T1.1")

        self.assertEqual(result.status, "SPLIT")
        self.assertEqual(result.segments, ((0.0, 6.125), (11.0, 20.0)))
        facts = result.facts_for(result.segments[0])
        self.assertEqual(facts["subject_check"], "PASS")
        self.assertEqual(facts["subject_note"], "")
        self.assertTrue(facts["subject_split_applied"])

    def test_no_segment_is_an_unknown_not_a_rejection(self):
        engine = RuleEngine(ROOT / "rules" / "qt_rules_v4.yaml", ROOT / "rules" / "conflicts.yaml")
        facts = {"duration": 14.0, "width": 3840, "height": 2160, "fps": 24, "has_audio": True,
                 "silence_ratio": 0.0, "playable": True, "shot_count": 1, "video_codec": "h264",
                 "subject_check": "NO_SEGMENT", "subject_note": NO_SEGMENT_NOTE,
                 "subject_loss_intervals": [{"start": 4.125, "end": 14.0, "duration": 9.875}]}
        result = next(r for r in engine.evaluate(facts) if r.rule_id == "R11")
        self.assertEqual(result.status, RuleStatus.UNKNOWN)
        self.assertIn("待人工", result.reason)
        self.assertFalse(engine.automatic_reject([result]))


class ModelLoadingTests(unittest.TestCase):
    def test_model_is_loaded_from_bytes_not_from_a_path(self):
        calls: list[dict[str, bytes]] = []
        with tempfile.TemporaryDirectory() as tmp:
            model_dir = Path(tmp) / "模型"
            model_dir.mkdir()
            (model_dir / "mobilenet_ssd_deploy.prototxt").write_bytes(b"proto")
            (model_dir / "mobilenet_ssd.caffemodel").write_bytes(b"weights")

            class Analyzer(SubjectContinuityAnalyzer):
                @property
                def available(self) -> bool:
                    return True

            analyzer = Analyzer(model_dir)
            analyzer._cv2 = SimpleNamespace(dnn=SimpleNamespace(
                readNetFromCaffe=lambda **kwargs: calls.append(kwargs) or "net"))
            self.assertEqual(analyzer._network(), "net")
            self.assertEqual(analyzer._network(), "net")

        self.assertEqual(calls, [{"bufferProto": b"proto", "bufferModel": b"weights"}])

    def test_missing_model_still_reports_unavailable(self):
        analyzer = SubjectContinuityAnalyzer(Path("missing"))
        self.assertFalse(analyzer.available)
        with self.assertRaises(RuntimeError):
            analyzer._network()
        result = analyzer.analyze(Path("clip.mp4"), 0.0, 20.0, "T1.1")
        self.assertEqual(result.status, "UNAVAILABLE")


class SustainedLossThresholdTests(unittest.TestCase):
    def presence(self, until: float, resume: float, end: float) -> list[float]:
        steps = int(round(until / SAMPLE_INTERVAL_SECONDS)) + 1
        times = [index * SAMPLE_INTERVAL_SECONDS for index in range(steps)]
        while resume <= end:
            times.append(round(resume, 3))
            resume += SAMPLE_INTERVAL_SECONDS
        return times

    def test_calibrated_defaults(self):
        self.assertEqual(SAMPLE_INTERVAL_SECONDS, 0.25)
        self.assertEqual(SUSTAINED_LOSS_SECONDS, 3.5)
        analyzer = SubjectContinuityAnalyzer(Path("missing"))
        self.assertEqual(analyzer.sample_interval, 0.25)
        self.assertEqual(analyzer.minimum_loss, 3.5)

    def test_absence_of_exactly_the_threshold_splits(self):
        segments, gaps = split_presence_samples(0.0, 20.0, self.presence(10.0, 13.75, 20.0))
        self.assertEqual(segments, ((0.0, 10.125), (13.75, 20.0)))
        self.assertEqual(len(gaps), 1)
        self.assertEqual(gaps[0]["duration"], 3.625)

    def test_absence_below_the_threshold_keeps_one_segment(self):
        segments, gaps = split_presence_samples(0.0, 20.0, self.presence(10.0, 13.5, 20.0))
        self.assertEqual(segments, ((0.0, 20.0),))
        self.assertEqual(gaps, ())


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
