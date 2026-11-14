#!/usr/bin/env python3
"""Keep the Kria iPhone app's String Catalogs complete in English, German and Turkish.

The app's screen text lives in src/apps/ios/Kria/Resources/Localizable.xcstrings (and the
permission prompts in InfoPlist.xcstrings). Xcode's IDE refreshes the catalog when it builds,
but command-line builds (CI, agents) never do, so this script is the single gate:

  check            Catalog sanity: every string has German and Turkish, placeholders match
                   the English, and every plural form is filled in. No Xcode needed.
  scan             Ratchet on source code: a user-visible looking English literal that is not
                   handed to a localizing API (Text("…"), String(localized: "…"), …) fails unless
                   it is in scripts/ios/localization-allowlist.json. No Xcode needed.
  built DIR        After a build: every string the compiler extracted (SWIFT_EMIT_LOC_STRINGS
                   writes *.stringsdata into DerivedData DIR) must be in the catalog.
  sync DIR         Developer helper: add strings extracted by a build to the catalog as English
                   only (check then lists what still needs German and Turkish).
  import FILE…     Merge translations ({"English": {"de": …, "tr": …}}) into the catalog as
                   needs_review (AI drafts); --state translated after a human review.
  scan --update    Rewrite the allowlist from the current source (only after reviewing that
                   every new entry is really not shown to people).

Run: python3 scripts/ios/localization.py check && python3 scripts/ios/localization.py scan
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
IOS = REPO / "src/apps/ios"
APP = IOS / "Kria"
CATALOG = APP / "Resources/Localizable.xcstrings"
INFOPLIST_CATALOG = APP / "Resources/InfoPlist.xcstrings"
ALLOWLIST = REPO / "scripts/ios/localization-allowlist.json"
LANGUAGES = ("de", "tr")
FILLED_STATES = {"translated", "needs_review"}

# Files that only exist for previews, UI tests, perf runs or debug tooling. Their text never
# reaches a creator, so neither the catalog nor the scan asks for translations.
DEBUG_ONLY_SOURCE = re.compile(
    r"(Fixture|UITest|Mocks|Perf|Benchmark|DesignGallery|BrandPreviewHost|TalkBench|ExportProof|MediaDiagnostic)"
)

# printf-style placeholders as Xcode writes them into catalog keys (%@, %lld, %1$@, %.1f, %%).
PLACEHOLDER = re.compile(r"%(?:(\d+)\$)?[-+ #0]*\d*(?:\.\d+)?(ll|l|h|hh|q|z|t|j)?([@dDiuUxXoOfFeEgGcCsSaAp%])")


def load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def dump(path: Path, catalog: dict) -> None:
    # Xcode writes " : " separators and sorted keys; match it so IDE saves don't churn the diff.
    text = json.dumps(catalog, indent=2, ensure_ascii=False, sort_keys=True, separators=(",", " : "))
    path.write_text(text + "\n", encoding="utf-8")


def placeholders(text: str) -> Counter:
    kinds = Counter()
    for match in PLACEHOLDER.finditer(text):
        conversion = match.group(3)
        if conversion == "%":
            continue
        kind = "object" if conversion == "@" else "int" if conversion in "dDiuUxXoOcC" else "float"
        kinds[kind] += 1
    return kinds


def units(localization: dict) -> list[tuple[str, dict]]:
    """Every (label, stringUnit) in one language, including plural/device variations."""
    found = []
    if "stringUnit" in localization:
        found.append(("", localization["stringUnit"]))
    for axis, cases in localization.get("variations", {}).items():
        for case, value in cases.items():
            found += [(f"{axis}.{case}{label}", unit) for label, unit in units(value)]
    for name, substitution in localization.get("substitutions", {}).items():
        found += [(f"sub.{name}{label}", unit) for label, unit in units(substitution)]
    return found


def check_catalog(path: Path) -> list[str]:
    catalog = load(path)
    problems = []
    for key, entry in sorted(catalog.get("strings", {}).items()):
        if entry.get("shouldTranslate") is False or entry.get("extractionState") == "stale":
            continue
        localizations = entry.get("localizations", {})
        source = key
        english = localizations.get("en")
        if english and "stringUnit" in english:
            source = english["stringUnit"].get("value", key)
        expected = placeholders(source)
        if english and "variations" in english:
            for label, unit in units(english):
                if not unit.get("value"):
                    problems.append(f"{path.name}: {key!r} en {label} is empty")
        for language in LANGUAGES:
            localization = localizations.get(language)
            if not localization:
                problems.append(f"{path.name}: {key!r} has no {language} translation")
                continue
            for label, unit in units(localization):
                value = unit.get("value", "")
                if unit.get("state") not in FILLED_STATES or not value.strip():
                    problems.append(f"{path.name}: {key!r} {language}{' ' + label if label else ''} is not filled in")
                    continue
                if not label and "substitutions" not in localization and placeholders(value) != expected:
                    problems.append(
                        f"{path.name}: {key!r} {language} placeholders {dict(placeholders(value))} "
                        f"!= English {dict(expected)}: {value!r}"
                    )
            if "variations" in localization and "plural" in localization["variations"]:
                if "other" not in localization["variations"]["plural"]:
                    problems.append(f"{path.name}: {key!r} {language} plural has no 'other' form")
    return problems


def stringsdata_keys(derived_data: Path) -> dict[str, dict]:
    # The app target plus the KriaMediaEngine package (its String(localized:) resolves in the app bundle).
    paths = [
        path
        for path in (derived_data / "Build/Intermediates.noindex").rglob("*.stringsdata")
        # .../<project>.build/<config>/<target>.build/Objects-normal/<arch>/<file>.stringsdata
        if len(path.parts) >= 4 and path.parts[-4] in {"Kria.build", "KriaMediaEngine.build"}
        and path.name != "ExtractedAppShortcutsMetadata.stringsdata"
    ]
    # One file per source per platform/configuration; an older build dir can hold stale strings.
    newest: dict[str, tuple[float, dict]] = {}
    for path in paths:
        data = load(path)
        source = data.get("source", str(path))
        if source and not Path(source).exists():
            continue  # a restored build cache can still hold data for a deleted file
        modified = path.stat().st_mtime
        if source not in newest or modified > newest[source][0]:
            newest[source] = (modified, data)
    keys: dict[str, dict] = {}
    for source, (_, data) in sorted(newest.items()):
        if DEBUG_ONLY_SOURCE.search(Path(source).name):
            continue
        for table, entries in data.get("tables", {}).items():
            if table != "Localizable":
                continue
            for entry in entries:
                keys.setdefault(entry["key"], {"comment": entry.get("comment", ""), "source": source})
    return keys


def built(derived_data: Path) -> list[str]:
    keys = stringsdata_keys(derived_data)
    if not keys:
        return [f"no Kria *.stringsdata under {derived_data}; build the Kria scheme first (SWIFT_EMIT_LOC_STRINGS=YES)"]
    strings = load(CATALOG).get("strings", {})
    missing = sorted(key for key in keys if key not in strings)
    return [
        f"Localizable.xcstrings is missing {key!r} (from {Path(keys[key]['source']).name}); "
        "run `python3 scripts/ios/localization.py sync <DerivedData>` and add German + Turkish"
        for key in missing
    ]


def sync(derived_data: Path) -> int:
    keys = stringsdata_keys(derived_data)
    catalog = load(CATALOG)
    strings = catalog.setdefault("strings", {})
    added = 0
    for key, info in sorted(keys.items()):
        if key in strings:
            if strings[key].get("extractionState") == "stale":
                strings[key].pop("extractionState")
            continue
        entry: dict = {}
        if info["comment"]:
            entry["comment"] = info["comment"]
        strings[key] = entry
        added += 1
    stale = 0
    for key, entry in strings.items():
        if key not in keys and entry.get("extractionState") != "manual" and keys:
            if entry.get("extractionState") != "stale":
                entry["extractionState"] = "stale"
                stale += 1
    dump(CATALOG, catalog)
    print(f"added {added} new string(s), marked {stale} unused string(s) stale")
    return 0


def import_translations(files: list[Path], state: str) -> list[str]:
    """Merge {"English key": {"de": …, "tr": …, "en"?: …}} files into the catalog.

    A value is either a string or plural forms {"one": …, "other": …}. Imported text is marked
    needs_review (AI drafts) unless --state translated is given after a human review.
    """
    catalog = load(CATALOG)
    strings = catalog.get("strings", {})
    problems = []
    for file in files:
        for key, languages in load(file).items():
            if key not in strings:
                problems.append(f"{file.name}: {key!r} is not in the catalog (run sync first)")
                continue
            localizations = strings[key].setdefault("localizations", {})
            for language, value in languages.items():
                if language not in ("en", *LANGUAGES):
                    continue
                unit_state = "translated" if language == "en" else state
                if isinstance(value, str):
                    localizations[language] = {"stringUnit": {"state": unit_state, "value": value}}
                elif isinstance(value, dict) and "other" in value:
                    localizations[language] = {
                        "variations": {
                            "plural": {
                                case: {"stringUnit": {"state": unit_state, "value": text}}
                                for case, text in value.items()
                            }
                        }
                    }
                else:
                    problems.append(f"{file.name}: {key!r} {language} has an unreadable value")
    dump(CATALOG, catalog)
    return problems + check_catalog(CATALOG)


# ---- source scan -------------------------------------------------------------------------

# Callees whose unlabeled (or `localized:`) string argument is looked up in the catalog.
LOCALIZING_CALLEES = {
    "Text", "Button", "Label", "Toggle", "Link", "TextField", "SecureField", "Picker", "Section", "Menu",
    "Stepper", "ContentUnavailableView", "LabeledContent", "DisclosureGroup", "ShareLink", "NavigationLink",
    "LocalizedStringKey", "LocalizedStringResource", "navigationTitle", "accessibilityLabel", "accessibilityHint",
    "accessibilityValue", "help", "alert", "confirmationDialog", "badge", "searchable", "accessibilityAction",
    "accessibilityInputLabels", "prompt", "Tab", "ProgressView", "ColorPicker", "DatePicker",
}
# Callees and argument labels whose string is never shown to people.
NON_UI_CALLEES = {
    "accessibilityIdentifier", "Image", "UIImage", "Logger", "print", "debugPrint", "os_log", "NSLog", "fatalError",
    "precondition", "preconditionFailure", "assert", "assertionFailure", "URL", "URLRequest", "custom", "Font",
    "UIFont", "increment", "begin", "end", "event", "signpost", "setValue", "value", "Notification", "Name",
    "NSPredicate", "Selector", "UTType", "CIKernel", "CIColorKernel", "CIWarpKernel", "CIFilter", "Bundle",
    "url", "path", "appending", "appendingPathComponent", "fileURLWithPath", "contains", "hasPrefix", "hasSuffix",
    "range", "replacingOccurrences", "components", "split", "trimmingCharacters", "NSRegularExpression",
    "Regex", "firstMatch", "matches", "decode", "encode", "CodingKeys", "AnyCodingKey", "key", "Key",
    "header", "debug", "info", "notice", "warning", "error", "fault", "trace", "critical", "log", "record",
    "SFSymbol", "named", "uniqueKeysWithValues", "task", "id", "onChange", "Perf", "measure", "interval",
}
NON_UI_LABELS = {
    "systemImage", "systemName", "forKey", "forResource", "withExtension", "identifier", "named", "name",
    "verbatim", "rawValue", "key", "category", "subsystem", "forHTTPHeaderField", "contentType", "fileExtension",
    "suiteName", "queue", "path", "mimeType", "filename", "id", "debugName", "route", "event", "kind",
    "type", "code", "reason", "action", "actionID", "format", "of", "with", "separator", "prefix", "suffix",
    "comment", "table", "tableName", "bundle",
}
WORDS = re.compile(r"[A-Za-z]{2,}")
CODE_LIKE = re.compile(r"^[A-Za-z0-9_.\-/:\\()%@$]+$")
IDENT_BEFORE = re.compile(r"([A-Za-z_][A-Za-z0-9_]*)\s*(?:<[^<>()]*>)?\s*$")
LABEL_BEFORE = re.compile(r"([A-Za-z_][A-Za-z0-9_]*)\s*:\s*$")


def looks_like_prose(value: str) -> bool:
    text = re.sub(r"\\\((?:[^()]|\([^()]*\))*\)", " X ", value)  # drop interpolations
    if not WORDS.search(text):
        return False
    if " " in text.strip():
        return len(WORDS.findall(text)) >= 2 or bool(re.search(r"[A-Z][a-z]", text))
    return bool(re.fullmatch(r"[A-Z][a-z]+[.!?…]?", text.strip()))


class _Literal:
    __slots__ = ("value", "line", "callee", "label", "line_prefix")

    def __init__(self, value, line, callee, label, line_prefix):
        self.value, self.line, self.callee, self.label, self.line_prefix = value, line, callee, label, line_prefix


def swift_literals(text: str) -> list[_Literal]:
    """String literals with the innermost call they are an argument of and their argument label.

    A small Swift lexer: tracks parentheses (with the identifier in front of each), comments, single-line,
    multi-line and raw strings, and string interpolation (whose inner literals get their own context).
    """
    found: list[_Literal] = []
    n = len(text)

    def line_of(index: int) -> int:
        return text.count("\n", 0, index) + 1

    def argument_prefix(start: int, open_index: int) -> str:
        # Text between the innermost `(` / `,` at this depth and the literal.
        return text[open_index + 1 : start]

    def code(i: int, stop_at_close: bool) -> int:
        stack: list[tuple[str, int]] = []  # (callee, index of "(")
        while i < n:
            c = text[i]
            if text.startswith("//", i):
                i = text.find("\n", i)
                if i < 0:
                    return n
                continue
            if text.startswith("/*", i):
                end = text.find("*/", i + 2)
                i = n if end < 0 else end + 2
                continue
            if c == "(" :
                match = IDENT_BEFORE.search(text, max(0, i - 80), i)
                stack.append((match.group(1) if match else "", i))
                i += 1
                continue
            if c == ")":
                if not stack:
                    if stop_at_close:
                        return i + 1
                else:
                    stack.pop()
                i += 1
                continue
            if c == "#" and re.match(r"#+\"", text[i:]):
                hashes = len(re.match(r"#+", text[i:]).group(0))
                quote_start = i + hashes
                triple = text.startswith('"""', quote_start)
                closer = ('"""' if triple else '"') + "#" * hashes
                end = text.find(closer, quote_start + (3 if triple else 1))
                i = n if end < 0 else end + len(closer)
                continue
            if c == '"':
                callee, label, prefix = "", "", ""
                if stack:
                    callee, open_index = stack[-1]
                    segment = argument_prefix(i, open_index)
                    depth, cut = 0, 0
                    for j, ch in enumerate(segment):
                        if ch in "([{":
                            depth += 1
                        elif ch in ")]}":
                            depth -= 1
                        elif ch == "," and depth == 0:
                            cut = j + 1
                    segment = segment[cut:]
                    label_match = re.match(r"\s*([A-Za-z_][A-Za-z0-9_]*)\s*:(?!:)", segment)
                    label = label_match.group(1) if label_match and not segment.lstrip().startswith("?") else ""
                line_start = text.rfind("\n", 0, i) + 1
                prefix = text[line_start:i]
                i = string(i, callee, label, prefix)
                continue
            i += 1
        return n

    def string(i: int, callee: str, label: str, prefix: str) -> int:
        triple = text.startswith('"""', i)
        start = i
        i += 3 if triple else 1
        body = []
        while i < n:
            c = text[i]
            if c == "\\":
                if i + 1 < n and text[i + 1] == "(":
                    end = code(i + 2, stop_at_close=True)
                    body.append(text[i:end])
                    i = end
                    continue
                body.append(text[i : i + 2])
                i += 2
                continue
            if triple and text.startswith('"""', i):
                i += 3
                break
            if not triple and c == '"':
                i += 1
                break
            if not triple and c == "\n":
                break
            body.append(c)
            i += 1
        found.append(_Literal("".join(body), line_of(start), callee, label, prefix))
        return i

    code(0, stop_at_close=False)
    return found


def needs_localizing(literal: _Literal) -> bool:
    value = literal.value
    if not looks_like_prose(value) or CODE_LIKE.match(value):
        return False
    prefix = literal.line_prefix.strip()
    if prefix.startswith("case ") and "return" not in prefix:
        return False  # match patterns compare against server values
    if literal.label in NON_UI_LABELS or literal.callee in NON_UI_CALLEES:
        return False
    if literal.callee == "String" and literal.label == "localized":
        return False
    if literal.callee in LOCALIZING_CALLEES and literal.label in ("", "title", "label", "message", "prompt"):
        return False
    return True


def catalog_key(value: str) -> str:
    """The catalog key Swift would extract for a literal: each interpolation becomes a placeholder."""
    return re.sub(r"\\\((?:[^()]|\([^()]*\))*\)", "%@", value).replace('\\"', '"')


def placeholder_shape(key: str) -> str:
    return PLACEHOLDER.sub("%@", key)


def scan_source() -> Counter:
    # A literal whose text is already a catalog key is localized wherever it is used (for example a
    # LocalizedStringKey or LocalizedStringResource parameter of an app view).
    known = {
        placeholder_shape(key)
        for key, entry in load(CATALOG).get("strings", {}).items()
        if entry.get("extractionState") != "stale"
    }
    flagged: Counter = Counter()
    for path in sorted(APP.rglob("*.swift")):
        if DEBUG_ONLY_SOURCE.search(path.name):
            continue
        relative = path.relative_to(APP).as_posix()
        text = path.read_text(encoding="utf-8")
        preview_ranges = [(m.start(), _block_end(text, m.end())) for m in re.finditer(r"#Preview\b", text)]
        line_offsets = [0] + [m.end() for m in re.finditer("\n", text)]
        for literal in swift_literals(text):
            offset = line_offsets[literal.line - 1]
            if any(a <= offset <= b for a, b in preview_ranges):
                continue
            if needs_localizing(literal) and placeholder_shape(catalog_key(literal.value)) not in known:
                flagged[f"{relative}\t{literal.value}"] += 1
    return flagged


def _block_end(text: str, index: int) -> int:
    open_brace = text.find("{", index)
    if open_brace < 0:
        return index
    depth = 0
    for j in range(open_brace, len(text)):
        if text[j] == "{":
            depth += 1
        elif text[j] == "}":
            depth -= 1
            if depth == 0:
                return j
    return len(text)


def scan(update: bool) -> list[str]:
    current = scan_source()
    if update:
        payload = {
            "about": (
                "English literals in the iPhone app that are NOT shown to people (server values, keys, "
                "debug text, content that goes into the video). scripts/ios/localization.py scan fails "
                "on any literal that looks like screen text and is not listed here. Wrap screen text in "
                "String(localized:) instead of adding it."
            ),
            "literals": {key: count for key, count in sorted(current.items())},
        }
        ALLOWLIST.write_text(json.dumps(payload, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
        print(f"wrote {len(current)} allowlisted literal(s)")
        return []
    allowed = load(ALLOWLIST).get("literals", {}) if ALLOWLIST.exists() else {}
    problems = []
    for key, count in sorted(current.items()):
        if count > allowed.get(key, 0):
            file, value = key.split("\t", 1)
            problems.append(
                f"src/apps/ios/Kria/{file}: {value!r} looks like screen text but is not localized. "
                'Use String(localized: "…") (or Text("…")); if it is never shown to people, add it to '
                "scripts/ios/localization-allowlist.json."
            )
    return problems


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("check")
    scan_parser = sub.add_parser("scan")
    scan_parser.add_argument("--update", action="store_true")
    for name in ("built", "sync"):
        sub.add_parser(name).add_argument("derived_data", type=Path)
    import_parser = sub.add_parser("import")
    import_parser.add_argument("files", type=Path, nargs="+")
    import_parser.add_argument("--state", choices=("needs_review", "translated"), default="needs_review")
    args = parser.parse_args(argv)

    if args.command == "sync":
        return sync(args.derived_data)
    if args.command == "check":
        problems = check_catalog(CATALOG) + check_catalog(INFOPLIST_CATALOG)
    elif args.command == "scan":
        problems = scan(args.update)
    elif args.command == "import":
        problems = import_translations(args.files, args.state)
    else:
        problems = built(args.derived_data)
    for problem in problems:
        print(problem, file=sys.stderr)
    if problems:
        print(f"{len(problems)} localization problem(s)", file=sys.stderr)
        return 1
    print(f"localization {args.command}: OK")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
