"""Guard: the silence-cut stage is unreachable from non-speech orchestrators.

Scope decision D2 (plans/010): music/template/montage paths replace source
audio, and cutting their timelines would corrupt song-absolute beat maps and
lyric timings. This pin makes reaching for the cut engine from those modules
a conscious, reviewed decision instead of a convenient import.
"""

import ast
from pathlib import Path

APP = Path(__file__).resolve().parents[2] / "app"

# Modules that must NEVER touch the silence-cut engine.
EXCLUDED_ORCHESTRATORS = [
    APP / "tasks" / "music_orchestrate.py",
    APP / "tasks" / "auto_music_orchestrate.py",
    APP / "tasks" / "template_orchestrate.py",
    APP / "tasks" / "orchestrate.py",
]

FORBIDDEN = ("silence_cut", "_silence_cut_analysis", "SilenceCutCache")


def _imports_of(path: Path) -> list[str]:
    tree = ast.parse(path.read_text())
    names: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            names.append(node.module or "")
            names.extend(alias.name for alias in node.names)
    return names


class TestSilenceCutIsolation:
    def test_non_speech_orchestrators_never_import_the_engine(self):
        for path in EXCLUDED_ORCHESTRATORS:
            assert path.exists(), path
            for name in _imports_of(path):
                for forbidden in FORBIDDEN:
                    assert forbidden not in name, (
                        f"{path.name} imports {name!r} — silence cutting must "
                        "not reach music/template paths (plans/010 D2)"
                    )

    def test_non_speech_orchestrators_never_reference_the_engine(self):
        # Belt and braces beyond imports: no textual reference at all.
        for path in EXCLUDED_ORCHESTRATORS:
            source = path.read_text()
            for forbidden in FORBIDDEN:
                assert forbidden not in source, (
                    f"{path.name} references {forbidden!r} (plans/010 D2)"
                )
