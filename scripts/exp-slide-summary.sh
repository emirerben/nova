#!/usr/bin/env bash
log="$1"
t=testClipPickerSlideNearBottomEdgeAutoScrollsAndVerticalDragStillScrolls
echo "bottom-edge test: passed $(grep -c "$t\]' passed" "$log" || true), failed $(grep -c "$t\]' failed" "$log" || true)"
echo "clips selected after the 6 s hold:"; grep -o "SLIDE-COUNT [0-9]*" "$log" | sort | uniq -c || true
echo "failure messages:"; grep -o "error: -\[.*" "$log" | cut -c1-260 | sort | uniq -c | head -8 || true
