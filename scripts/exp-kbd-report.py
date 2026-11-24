"""Throwaway (KRI-1604): match each never-idle wait in an xcodebuild log to the app's probe lines."""
import datetime as dt
import re
import sys

label, test_log, probe_log = sys.argv[1:4]
lines = open(test_log, errors="replace").read().splitlines()
probe = []
for line in open(probe_log, errors="replace").read().splitlines():
    found = re.match(r"(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d\.\d+)", line)
    if found and "KBDPROBE" in line:
        probe.append((dt.datetime.strptime(found.group(1), "%Y-%m-%d %H:%M:%S.%f"), line[line.index("KBDPROBE"):]))

passed = sum("]' passed" in line for line in lines)
failed = sum("]' failed" in line for line in lines)
limit = sum("exceeded execution time allowance" in line for line in lines)
exits = [line.split()[-1] for line in lines if line.startswith("=== PASS") and " exit " in line]
print(f"[{label}] passes {len(exits)}, exits not 0: {sum(code != '0' for code in exits)}; tests passed {passed}, failed {failed}, time-limit hits {limit}")
print(f"[{label}] probe lines {len(probe)} (started {sum('started' in text for _, text in probe)}, beats {sum(' beat ' in text for _, text in probe)}, keyboard {sum(' keyboard ' in text for _, text in probe)}, running {sum(' running ' in text for _, text in probe)})")

test = start = step = None
hangs = 0
for line in lines:
    found = re.search(r"Test Case '-\[KriaUITests\.(\S+ \S+)\]' started", line)
    if found:
        test, start, step = found.group(1), None, None
    found = re.search(r"Start Test at (\d{4}-\d\d-\d\d \d\d:\d\d:\d\d\.\d+)", line)
    if found:
        start = dt.datetime.strptime(found.group(1), "%Y-%m-%d %H:%M:%S.%f")
    found = re.match(r"\s+t =\s+([\d.]+)s (Tap|Type|Press|Swipe|Adjust) (.*)", line)
    if found:
        step = f"{found.group(2)} {found.group(3)[:70]}"
    found = re.match(r"\s+t =\s+([\d.]+)s\s+App animations complete notification not received", line)
    if found and start:
        hangs += 1
        end = start + dt.timedelta(seconds=float(found.group(1)))
        begin = end - dt.timedelta(seconds=62)
        print(f"[{label}] NEVER-IDLE #{hangs}: {test} | step: {step} | {begin.time()} to {end.time()}")
        for when, text in probe:
            if begin - dt.timedelta(seconds=8) <= when <= end:
                print(f"[{label}]     {when.time()} {text[:330]}")
print(f"[{label}] never-idle waits: {hangs}")
if True:
    seen = {}
    for _, text in probe:
        if " running " in text:
            key = re.sub(r"running \d+s", "running Ns", text)[:260]
            seen[key] = seen.get(key, 0) + 1
    print(f"[{label}] long-running animations over the whole arm (distinct {len(seen)}):")
    for key, count in sorted(seen.items(), key=lambda item: -item[1])[:15]:
        print(f"[{label}]     {count}x {key}")
