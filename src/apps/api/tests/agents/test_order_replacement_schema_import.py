"""Creator schema must import before runtime packages in a fresh interpreter."""

import subprocess
import sys


def test_creator_schema_import_does_not_initialize_order_runtime_cycle():
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "from app.agents._schemas.creator_agent import CreativeStrategy; "
            "from app.schemas.order_replacement import OrderReplacementProof; "
            "from app.kria.order_replacement import OrderReplacementProof as RuntimeProof; "
            "assert RuntimeProof is OrderReplacementProof; "
            "assert 'confirmed_order_replacement' not in "
            "CreativeStrategy.model_json_schema()['properties']",
        ],
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert result.returncode == 0, result.stderr
