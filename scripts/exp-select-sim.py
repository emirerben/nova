import json
import sys

payload = json.load(sys.stdin)
for runtime, devices in reversed(list(payload["devices"].items())):
    if "iOS" not in runtime:
        continue
    for device in devices:
        if device.get("isAvailable") and device["name"].startswith("iPhone"):
            print(device["udid"])
            raise SystemExit(0)
raise SystemExit("no iPhone simulator")
