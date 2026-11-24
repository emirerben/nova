"""Throwaway (KRI-1617): red controls. usage: exp-control.py untied|unasked"""
import pathlib, sys
path = pathlib.Path("src/apps/ios/Kria/Features/NativeEditorSession.swift")
text = path.read_text()
if sys.argv[1] == "untied":  # the kept Save is no longer dropped with the edit it was for
    old = " { didSet { if !hasUnsavedChanges { saveRequestedDuringLoad = false } } }"
    new = ""
else:  # the end of a load saves any unsaved edit, asked or not
    old = "        guard saveRequestedDuringLoad else { return }\n        saveRequestedDuringLoad = false\n"
    new = "        saveRequestedDuringLoad = false\n"
assert text.count(old) == 1
path.write_text(text.replace(old, new))
