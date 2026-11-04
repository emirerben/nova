"""The review's frozen inputs must not depend on which process built them.

The API process admits a review and a worker re-derives the same inputs to check them
before touching storage. A recipe's capability set serializes in per-process hash order,
so an unsorted list made the digests differ and every review ended `stale`.
"""

import json
import os
import subprocess
import sys

import pytest

CAPABILITIES = [
    "audioMix",
    "crossfade",
    "basicComposition",
    "animatedText",
    "positionedText",
    "local1080Export",
]

SCRIPT = """
import json, sys
from app.kria.recipes import EditRecipeV1
from app.kria.device_render import canonical_recipe_document
recipe = EditRecipeV1(required_capabilities=set(json.loads(sys.argv[1])))
print(json.dumps(canonical_recipe_document(recipe), sort_keys=True))
"""


def _document_under_hash_seed(seed: int) -> str:
    env = {
        **os.environ,
        "PYTHONHASHSEED": str(seed),
        "STORAGE_BUCKET": os.environ.get("STORAGE_BUCKET", "x"),
        "STORAGE_PROVIDER": os.environ.get("STORAGE_PROVIDER", "local"),
        "REDIS_URL": os.environ.get("REDIS_URL", "redis://x"),
        "OPENAI_API_KEY": os.environ.get("OPENAI_API_KEY", "x"),
        "GEMINI_API_KEY": os.environ.get("GEMINI_API_KEY", "x"),
    }
    done = subprocess.run(
        [sys.executable, "-c", SCRIPT, json.dumps(CAPABILITIES)],
        capture_output=True,
        text=True,
        env=env,
        timeout=120,
    )
    assert done.returncode == 0, done.stderr[-800:]
    return done.stdout.strip().splitlines()[-1]


@pytest.mark.timeout(240)
def test_the_recipe_in_the_review_inputs_is_the_same_in_every_process():
    documents = {_document_under_hash_seed(seed) for seed in range(1, 5)}
    assert len(documents) == 1
    (document,) = documents
    assert json.loads(document)["required_capabilities"] == sorted(CAPABILITIES)
