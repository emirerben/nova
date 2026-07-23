"""Prefork child-recycle invariant (2026-07-21 OOM incident, job e8173a25).

A burst of analyze_pool_asset tasks left CLIP/torch/Whisper residency in the
single long-lived prefork child; the next render's ffmpeg peak stacked on top
and the kernel OOM-killed the worker mid-reframe (silent death → 30-min
acks_late redelivery gap). `worker_max_memory_per_child` recycles the child
BETWEEN tasks once its RSS exceeds the threshold, so residency never carries
into a render's peak-memory window.

Two pins:
  1. The Celery conf actually carries the settings value (a typo'd conf key
     silently disables recycling — Celery ignores unknown keys).
  2. The threshold stays strictly below the worker VM's memory (fly.toml);
     a threshold above the machine is dead config that can never fire.
"""

import os
import re

from app.config import settings
from app.worker import celery_app


def _worker_vm_memory_mb() -> int:
    repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), *[os.pardir] * 5))
    with open(os.path.join(repo_root, "fly.toml"), encoding="utf-8") as f:
        fly = f.read()
    # The worker [[vm]] block is the one that declares BOTH processes=["worker"]
    # and a memory_mb. ("[[vm]]" also appears in prose comments and the
    # [[services]] worker block repeats the processes line — require both
    # markers so neither false-positives.)
    for block in fly.split("[[vm]]")[1:]:
        if re.search(r'processes\s*=\s*\[\s*"worker"\s*\]', block):
            m = re.search(r"memory_mb\s*=\s*(\d+)", block)
            if m:
                return int(m.group(1))
    raise AssertionError("no worker [[vm]] block with memory_mb found in fly.toml")


def test_conf_carries_settings_value() -> None:
    assert settings.worker_max_memory_per_child_kb > 0, (
        "worker_max_memory_per_child_kb default must stay enabled — it is the "
        "residency backstop for the 2026-07-21 OOM class. Disable per-env via "
        "WORKER_MAX_MEMORY_PER_CHILD_KB=0, not by changing the default."
    )
    assert celery_app.conf.worker_max_memory_per_child == settings.worker_max_memory_per_child_kb


def test_threshold_below_worker_vm_memory() -> None:
    vm_kb = _worker_vm_memory_mb() * 1024
    threshold_kb = settings.worker_max_memory_per_child_kb
    assert threshold_kb < vm_kb, (
        f"worker_max_memory_per_child_kb ({threshold_kb}KB) >= worker VM memory "
        f"({vm_kb}KB) — the recycle can never fire before the kernel OOM-killer "
        "does. Keep the threshold under the machine size with ffmpeg headroom."
    )
