from __future__ import annotations

import unittest
from pathlib import Path

from qt_tool.rules import RuleEngine, RuleStatus


ROOT = Path(__file__).resolve().parent.parent


class RuleEngineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.engine = RuleEngine(ROOT / "rules" / "qt_rules_v4.yaml", ROOT / "rules" / "conflicts.yaml")

    def result(self, rule: str, **facts):
        defaults = {"duration": 10.0, "width": 3840, "height": 2160, "fps": 30,
                    "has_audio": True, "silence_ratio": 0.0, "playable": True,
                    "shot_count": 1, "video_codec": "h264"}
        defaults.update(facts)
        return next(r for r in self.engine.evaluate(defaults) if r.rule_id == rule)

    def test_4_9_seconds_fails_r17(self):
        self.assertEqual(self.result("R17", duration=4.9).status, RuleStatus.FAIL)

    def test_duration_buckets(self):
        self.assertEqual(self.engine.duration_bucket(10)[0], "short")
        self.assertEqual(self.engine.duration_bucket(20)[0], "medium")
        self.assertEqual(self.engine.duration_bucket(45)[0], "long")
        self.assertEqual(self.engine.duration_bucket(90)[0], "long")

    def test_duration_boundaries_are_conflicts(self):
        self.assertEqual(self.engine.duration_bucket(15.0)[1], RuleStatus.CONFLICT)
        self.assertEqual(self.engine.duration_bucket(30.0)[1], RuleStatus.CONFLICT)

    def test_material_type_rules(self):
        self.assertEqual(self.result("R16", bucket="T1", unit="T1.1", material_type="animation").status, RuleStatus.FAIL)
        self.assertEqual(self.result("R16", bucket="T9", unit="T9.1", material_type="live_action").status, RuleStatus.FAIL)
        for bucket in ("T1", "T5", "T9"):
            self.assertEqual(self.result("R16", bucket=bucket, material_type="gameplay").status, RuleStatus.FAIL)

    def test_aspect_ratio(self):
        self.assertEqual(self.result("R2", width=1080, height=1920).status, RuleStatus.FAIL)
        self.assertEqual(self.result("R2", width=1920, height=1080).status, RuleStatus.PASS)

    def test_t6_orbit_exemptions_and_requirement(self):
        exempt = self.result("T6_ORBIT", bucket="T6", unit="T6.3", material_type="live_action", has_orbit_or_revisit=False)
        self.assertEqual(exempt.status, RuleStatus.NOT_APPLICABLE)
        required = self.result("T6_ORBIT", bucket="T6", unit="T6.6", material_type="live_action", has_orbit_or_revisit=False)
        self.assertEqual(required.status, RuleStatus.FAIL)
        unknown = self.result("T6_ORBIT", bucket="T6", unit="T6.6", material_type="live_action")
        self.assertEqual(unknown.status, RuleStatus.UNKNOWN)

    def test_t8_4_slow_motion_exemption(self):
        self.assertEqual(self.result("R10", bucket="T8", unit="T8.4", material_type="live_action", intentional_slow_motion=True).status,
                         RuleStatus.NOT_APPLICABLE)
        self.assertEqual(self.result("R10", bucket="T8", unit="T8.3", material_type="live_action", intentional_slow_motion=True).status,
                         RuleStatus.FAIL)

    def test_r13_exemption_and_conflict(self):
        self.assertEqual(self.result("R13", bucket="T1", unit="T1.7", material_type="live_action", aerial=True).status,
                         RuleStatus.NOT_APPLICABLE)
        self.assertEqual(self.result("R13", bucket="T8", unit="T8.2", material_type="live_action", aerial=True).status,
                         RuleStatus.CONFLICT)

    def test_unit_scope_conflict(self):
        for unit in ("T5.6", "T5.7", "T7.6", "T7.7", "T7.8", "T9.5"):
            self.assertEqual(self.engine.unit_gate(unit).status, RuleStatus.PASS)
            self.assertFalse(self.engine.units[unit]["requires_confirmation"])
        self.assertEqual(self.engine.unit_gate("T3.6").status, RuleStatus.CONFLICT)
        self.assertEqual(self.engine.unit_gate("T1.1").status, RuleStatus.PASS)

    def test_only_deterministic_fail_auto_rejects(self):
        unknown = self.result("R12", bucket="T1", unit="T1.1", material_type="live_action")
        self.assertFalse(self.engine.automatic_reject([unknown]))
        hard_fail = self.result("R17", duration=4.9)
        self.assertTrue(self.engine.automatic_reject([hard_fail]))

    def test_r11_subject_split_is_a_reviewable_pass_not_a_rejection(self):
        result = self.result("R11", subject_check="PASS", subject_split_applied=True,
                             subject_loss_intervals=[{"start": 14.25, "end": 19.0}])
        self.assertEqual(result.status, RuleStatus.PASS)
        self.assertFalse(result.deterministic)
        self.assertFalse(self.engine.automatic_reject([result]))

    def test_r11_legacy_clip_needing_trim_is_not_marked_failed(self):
        result = self.result("R11", subject_check="TRIM_REQUIRED", subject_trim_required=True)
        self.assertEqual(result.status, RuleStatus.UNKNOWN)
        self.assertFalse(result.deterministic)

    def test_r9_defers_proxy_resolution_and_checks_final_source(self):
        proxy = self.result("R9", bucket="T1", source_type="PROXY", width=854, height=480)
        self.assertEqual(proxy.status, RuleStatus.UNKNOWN)
        self.assertFalse(proxy.deterministic)

        low_final = self.result("R9", bucket="T1", source_type="FINAL", width=1920, height=1080)
        self.assertEqual(low_final.status, RuleStatus.FAIL)
        self.assertTrue(low_final.deterministic)

        qualified_final = self.result("R9", bucket="T1", source_type="FINAL", width=3840, height=2160)
        self.assertEqual(qualified_final.status, RuleStatus.UNKNOWN)
        self.assertFalse(qualified_final.deterministic)

    def test_ntsc_24p_passes_the_frame_rate_spec(self):
        result = self.result("SPEC_FPS", bucket="T1", unit="T1.1", source_type="FINAL",
                             material_type="live_action", fps=23.976)
        self.assertEqual(result.status, RuleStatus.PASS)
        self.assertTrue(result.deterministic)

    def test_frame_rates_below_24p_still_fail(self):
        for fps in (23.0, 23.5, 15.0):
            result = self.result("SPEC_FPS", bucket="T1", unit="T1.1", source_type="FINAL",
                                 material_type="live_action", fps=fps)
            self.assertEqual(result.status, RuleStatus.FAIL, fps)

    def test_frame_rate_threshold_comes_from_the_rules_file(self):
        self.assertEqual(self.engine.live_action_minimum_fps(), 23.9)


if __name__ == "__main__":
    unittest.main()
