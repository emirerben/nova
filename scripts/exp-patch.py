"""Throwaway (KRI-1617): record how long Save takes to reach Saved, and what the button read just after the tap."""
import pathlib

path = pathlib.Path("src/apps/ios/Tests/KriaUITests/DesignPickerUITests.swift")
old = '''        save.tap()
        XCTAssertTrue(waitFor(save, "label == %@", "Saved", timeout: 20), "Save reaches the saved state; reads \\(save.label)")
'''
new = '''        let preparing = app.staticTexts["Preparing preview"].exists
        let tapped = Date()
        save.tap()
        let after = save.label
        let within20 = waitFor(save, "label == %@", "Saved", timeout: 20)
        let reached = within20 || waitFor(save, "label == %@", "Saved", timeout: 60)
        print("SAVE-ELAPSED seconds=\\(Date().timeIntervalSince(tapped)) within20=\\(within20) reached=\\(reached) previewPreparingAtTap=\\(preparing) labelAfterTap=\\(after) labelAtEnd=\\(save.label)")
        XCTAssertTrue(within20, "Save reaches the saved state; reads \\(save.label)")
'''
text = path.read_text()
assert text.count(old) == 1
path.write_text(text.replace(old, new))
