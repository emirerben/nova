"""Throwaway (KRI-1605, KRI-1607): red controls."""
import sys

def edit(path, old, new):
    text = open(path).read()
    assert text.count(old) == 1, (path, old[:60], text.count(old))
    open(path, "w").write(text.replace(old, new))

SESSIONS = "src/apps/ios/Kria/Core/DeviceRenderSessions.swift"
SUPPORT = "src/apps/ios/Tests/KriaTests/NativeEditorTestSupport.swift"
arm = sys.argv[1]
if arm == "control-nowait":  # the session's wait step returns at once
    edit(SESSIONS, "        while unawaitedWork > 0 { await withCheckedContinuation { idleWaiters.append($0) } }", "        // control: no wait")
elif arm == "control-relink":  # an automatic relink (retry: true) gets past the creator's Stop
    edit(SESSIONS, "        guard resolveIntent(status, key: key, action: intentAction) else {", "        guard resolveIntent(status, key: key, action: intentAction) || retry else {")
    edit(SESSIONS, "            if pausedHere && intentAction != .creatorResume {", "            if pausedHere && intentAction != .creatorResume && !retry {")
elif arm == "control-admit-all":  # every request reaches the current handler, as on main
    edit(SUPPORT, "            guard let made = request.value(forHTTPHeaderField: Self.header), made != String(number) else { return true }",
         "            guard false, let made = request.value(forHTTPHeaderField: Self.header), made != String(number) else { return true }")
else:
    raise SystemExit(f"unknown arm {arm}")
