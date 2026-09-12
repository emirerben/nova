import subprocess
import sys
from pathlib import Path


def test_native_animation_dto_matches_authoritative_schema():
    script = Path(__file__).resolve().parents[2] / "scripts/generate_native_text_contract.py"
    subprocess.run([sys.executable, str(script), "--check"], check=True)


def test_native_variable_fonts_match_cloud_instances():
    script = Path(__file__).resolve().parents[2] / "scripts/generate_native_font_instances.py"
    subprocess.run([sys.executable, str(script), "--check"], check=True)
