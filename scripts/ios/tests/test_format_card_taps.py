"""UI tests pick a creation format through `chooseFormat`, never with a bare tap.

A format card is on screen while the chat's formats are still loading, and disabled until they have. A tap sent
then is dropped without an error and the test fails later on something unrelated (KRI-1604). `FormatCards.swift`
waits for the card to accept taps.

What this catches: a new `….buttons["format-…"].tap()` in any UI test file.
What it cannot catch: a card held in a variable and tapped later (`let card = …; card.tap()`); use
`chooseFormatCard(card)` there.
"""

from pathlib import Path
import re
import unittest


UI_TESTS = Path(__file__).resolve().parents[3] / "src/apps/ios/Tests/KriaUITests"
HELPER = UI_TESTS / "FormatCards.swift"
BARE_TAP = re.compile(r'buttons\["format-[a-z_]+"\]\.tap\(\)')


class FormatCardTapTests(unittest.TestCase):
    def test_the_helper_waits_for_the_card_to_be_enabled_before_tapping(self):
        source = HELPER.read_text()
        self.assertIn("func chooseFormat(", source)
        self.assertIn("enabled == true", source)
        self.assertLess(source.index("enabled == true"), source.index("card.tap()"))

    def test_no_ui_test_taps_a_format_card_directly(self):
        bare = [
            f"{path.name}:{number}"
            for path in sorted(UI_TESTS.glob("*.swift"))
            if path != HELPER
            for number, line in enumerate(path.read_text().splitlines(), 1)
            if BARE_TAP.search(line)
        ]
        self.assertEqual(bare, [], "tap format cards with app.chooseFormat(…): " + ", ".join(bare))


if __name__ == "__main__":
    unittest.main()
