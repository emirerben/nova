"""Advertised transport capacity for complete editor-copilot draft snapshots."""

# Generated narration can contain hundreds of individually editable text bars.
# The old 20 KiB envelope made clients hide otherwise-supported edit families.
# Advertise this bound so a new browser never sends it to an older API.
COPILOT_SNAPSHOT_MAX_BYTES = 512 * 1024
