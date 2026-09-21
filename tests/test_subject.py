from __future__ import annotations

import unittest
from pathlib import Path

from qt_tool.rules import RuleEngine
from qt_tool.subject import SUPPORTED_PERSON_UNITS, SubjectContinuityAnalyzer


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


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
