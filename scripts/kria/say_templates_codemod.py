#!/usr/bin/env python3
"""Rewrite ``say(en=f"...", tr=f"...")`` calls into templates (KRI-1362, "B").

A chat sentence is looked up in ``app/kria/copy/<language>.json`` by its English text, so the
English text must be the same on every turn. An f-string is not: ``f"No pop-in for {names}"``
differs per turn. This codemod turns each such call into a template plus keyword values,

    say(en=f"No pop-in for {names}", tr=f"{names} için pop-in yok")
    ->
    say(en="No pop-in for {names}", tr="{names} için pop-in yok", names=names)

which ``say()`` fills with ``str.format`` after picking the language. Behaviour is identical:
``str.format`` applies the same conversion (``!r``) and format spec (``:.1f``) through the same
``format()`` call an f-string does, and literal braces are doubled so they come out single.

Structure comes from ``ast`` + ``tokenize`` positions only (no regex over source). Every converted
call is self-checked: the f-string rebuilt from the new template and keyword values must unparse
exactly like the original; the run aborts on any mismatch. ``--verify REV`` re-runs that proof for
the working tree against a git revision (after ``ruff format``, say): every module must unparse
identically to REV once each converted call is folded back into its f-strings.

Stdlib only.

    python3 scripts/kria/say_templates_codemod.py [--dry-run] [--report out.json]
    python3 scripts/kria/say_templates_codemod.py --verify HEAD
"""

from __future__ import annotations

import argparse
import ast
import copy
import io
import json
import keyword
import string
import subprocess
import sys
import tokenize
from dataclasses import dataclass, field
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
API = REPO / "src" / "apps" / "api"
APP = API / "app"
SAY_MODULE = "app.kria.reply_language"
RESERVED = frozenset({"en", "tr", "self", "cls"})
MAX_NAME = 32

# Calls the report treats as side-effect free. Anything else is converted too, but listed.
PURE_FUNCS = frozenset(
    {
        "len",
        "str",
        "int",
        "float",
        "round",
        "getattr",
        "sorted",
        "min",
        "max",
        "abs",
        "bool",
        "repr",
        "list",
        "tuple",
        "set",
        "frozenset",
        "sum",
        "any",
        "all",
        "format",
        "isinstance",
        "reversed",
        "enumerate",
        "zip",
        "dict",
    }
)
PURE_METHODS = frozenset(
    {
        "join",
        "strip",
        "lstrip",
        "rstrip",
        "lower",
        "upper",
        "title",
        "capitalize",
        "casefold",
        "replace",
        "get",
        "split",
        "rsplit",
        "format",
        "removeprefix",
        "removesuffix",
        "startswith",
        "endswith",
        "count",
        "index",
        "keys",
        "values",
        "items",
        "isoformat",
        "total_seconds",
    }
)
# Formatting helpers by name shape (``_names``, ``_fmt_seconds``, ``format_list``, ``_plural``…).
PURE_HELPER_PREFIXES = (
    "_names",
    "names",
    "_join",
    "join_",
    "_fmt",
    "fmt_",
    "_format",
    "format_",
    "_plural",
    "plural",
    "_human",
    "human",
    "_quote",
    "quote",
    "_label",
    "_seconds",
    "_secs",
    "_pct",
    "_list",
    "_short",
    "_clean",
)


@dataclass
class Report:
    files_touched: list[str] = field(default_factory=list)
    converted: list[dict] = field(default_factory=list)
    skipped: dict[str, list[str]] = field(default_factory=dict)
    non_pure: list[dict] = field(default_factory=list)

    def skip(self, reason: str, where: str) -> None:
        self.skipped.setdefault(reason, []).append(where)

    def as_json(self) -> dict:
        return {
            "files_touched": sorted(self.files_touched),
            "files_touched_count": len(self.files_touched),
            "calls_converted": len(self.converted),
            "calls_skipped": sum(len(v) for v in self.skipped.values()),
            "skipped_by_reason": {k: sorted(v) for k, v in sorted(self.skipped.items())},
            "non_pure_expressions": self.non_pure,
            "converted": self.converted,
        }


class CodemodError(RuntimeError):
    pass


# ---------------------------------------------------------------- AST helpers


def _imports_say(tree: ast.Module) -> bool:
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == SAY_MODULE:
            if any(a.name == "say" and a.asname in (None, "say") for a in node.names):
                return True
    return False


def _is_say_call(node: ast.AST) -> bool:
    return isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "say"


def say_calls(tree: ast.AST) -> list[ast.Call]:
    """Every ``say(...)`` call, in source order."""
    calls = [n for n in ast.walk(tree) if _is_say_call(n)]
    return sorted(calls, key=lambda c: (c.lineno, c.col_offset))


def _kw(call: ast.Call, name: str) -> ast.keyword | None:
    return next((k for k in call.keywords if k.arg == name), None)


def _str_const(node: ast.AST) -> bool:
    return isinstance(node, ast.Constant) and isinstance(node.value, str)


def _fields(node: ast.JoinedStr) -> list[ast.FormattedValue]:
    return [v for v in node.values if isinstance(v, ast.FormattedValue)]


def _spec_text(fv: ast.FormattedValue) -> str | None:
    """The literal format spec, "" for none, None when the spec is itself formatted."""
    if fv.format_spec is None:
        return ""
    spec = fv.format_spec
    if not isinstance(spec, ast.JoinedStr):
        return None
    if any(not _str_const(v) for v in spec.values):
        return None
    return "".join(v.value for v in spec.values)


def _derive(expr: ast.AST) -> str | None:
    """A readable snake_case name for a simple expression, else None."""
    if isinstance(expr, ast.Name):
        return expr.id
    if isinstance(expr, ast.Attribute):
        if isinstance(expr.value, ast.Name) and expr.value.id in ("self", "cls"):
            return expr.attr
        base = _derive(expr.value)
        return f"{base}_{expr.attr}" if base else None
    if isinstance(expr, ast.Subscript):
        base = _derive(expr.value)
        key = expr.slice
        if base and isinstance(key, ast.Constant):
            if isinstance(key.value, str) and key.value.isidentifier():
                return f"{base}_{key.value}"
            if isinstance(key.value, int) and not isinstance(key.value, bool):
                return f"{base}_{key.value}"
        return None
    if isinstance(expr, ast.Call):
        func = expr.func
        if isinstance(func, ast.Name):
            head = func.id
        elif isinstance(func, ast.Attribute):
            owner = func.value
            if isinstance(owner, ast.Constant) or (
                isinstance(owner, ast.Name) and owner.id in ("self", "cls")
            ):
                head = func.attr
            else:
                base = _derive(owner)
                if not base:
                    return None
                head = f"{base}_{func.attr}"
        else:
            return None
        parts = [head]
        for arg in expr.args:
            if isinstance(arg, ast.Constant):
                continue
            sub = _derive(arg)
            if not sub:
                return None
            parts.append(sub)
        return "_".join(parts)
    return None


def _clean(raw: str | None) -> str | None:
    if not raw:
        return None
    segments = [s.strip("_").lower() for s in raw.split("_")]
    name = "_".join(s for s in segments if s)
    if not name or len(name) > MAX_NAME or not name.isidentifier() or keyword.iskeyword(name):
        return None
    if name[0].isdigit():
        return None
    return name


def _call_name(func: ast.AST) -> str:
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return f".{func.attr}"
    return ast.unparse(func)


def _non_pure_calls(expr: ast.AST) -> list[str]:
    found = []
    for node in ast.walk(expr):
        if isinstance(node, ast.Await | ast.Yield | ast.YieldFrom):
            found.append(type(node).__name__.lower())
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Name):
            if func.id in PURE_FUNCS or func.id.startswith(PURE_HELPER_PREFIXES):
                continue
        elif isinstance(func, ast.Attribute):
            if func.attr in PURE_METHODS or func.attr.startswith(PURE_HELPER_PREFIXES):
                continue
        found.append(_call_name(func))
    return found


def _escape(text: str) -> str:
    return text.replace("{", "{{").replace("}", "}}")


def _field_text(name: str, fv: ast.FormattedValue) -> str:
    out = name
    if fv.conversion != -1:
        out += "!" + chr(fv.conversion)
    spec = _spec_text(fv)
    if spec:
        out += ":" + spec
    return "{" + out + "}"


def _template(node: ast.AST, names: dict[str, str], templated: bool) -> str:
    """The ``str.format`` template for one en/tr value (JoinedStr or str Constant)."""
    if _str_const(node):
        return _escape(node.value) if templated else node.value
    assert isinstance(node, ast.JoinedStr)
    out = []
    for part in node.values:
        if _str_const(part):
            out.append(_escape(part.value) if templated else part.value)
        else:
            assert isinstance(part, ast.FormattedValue)
            out.append(_field_text(names[ast.unparse(part.value)], part))
    return "".join(out)


def _rebuild(template: str, values: dict[str, ast.AST], templated: bool) -> ast.AST:
    """The f-string a template + values stands for (inverse of ``_template``)."""
    if not templated:
        return ast.Constant(template)
    parts: list[ast.AST] = []
    for literal, name, spec, conv in string.Formatter().parse(template):
        if literal:
            if parts and _str_const(parts[-1]):
                parts[-1] = ast.Constant(parts[-1].value + literal)
            else:
                parts.append(ast.Constant(literal))
        if name is None:
            continue
        if name not in values:
            raise CodemodError(f"template field {name!r} has no value")
        fmt = ast.JoinedStr([ast.Constant(spec)]) if spec else None
        parts.append(
            ast.FormattedValue(
                value=copy.deepcopy(values[name]),
                conversion=ord(conv) if conv else -1,
                format_spec=fmt,
            )
        )
    return ast.JoinedStr(parts)


class _FoldConstantFStrings(ast.NodeTransformer):
    """An f-string with no fields is just a string: compare both forms the same way."""

    def visit_JoinedStr(self, node: ast.JoinedStr) -> ast.AST:
        self.generic_visit(node)
        if all(_str_const(v) for v in node.values):
            return ast.copy_location(ast.Constant("".join(v.value for v in node.values)), node)
        return node


def _norm(node: ast.AST) -> str:
    return ast.unparse(_FoldConstantFStrings().visit(copy.deepcopy(node)))


# ---------------------------------------------------------------- source positions


class Source:
    def __init__(self, text: str) -> None:
        self.text = text
        self.lines = text.splitlines(keepends=True)
        self.starts = [0]
        for line in self.lines:
            self.starts.append(self.starts[-1] + len(line))
        self.tokens = list(tokenize.generate_tokens(io.StringIO(text).readline))

    def char(self, lineno: int, col: int) -> int:
        """Absolute char offset of a tokenize (row, char-col) position."""
        return self.starts[lineno - 1] + col

    def ast_offset(self, lineno: int, col_bytes: int) -> int:
        line = self.lines[lineno - 1]
        return self.starts[lineno - 1] + len(line.encode("utf-8")[:col_bytes].decode("utf-8"))

    def span(self, node: ast.AST) -> tuple[int, int]:
        return (
            self.ast_offset(node.lineno, node.col_offset),
            self.ast_offset(node.end_lineno, node.end_col_offset),
        )

    def string_pieces(self, node: ast.AST) -> list[tuple[int, int]]:
        """Char spans of each string literal token making up ``node`` (implicit concatenation)."""
        lo, hi = self.span(node)
        pieces: list[tuple[int, int]] = []
        depth = 0
        fstart = getattr(tokenize, "FSTRING_START", None)
        fend = getattr(tokenize, "FSTRING_END", None)
        open_at = 0
        for tok in self.tokens:
            a = self.char(*tok.start)
            b = self.char(*tok.end)
            if a < lo or b > hi:
                continue
            if fstart is not None and tok.type == fstart:
                if depth == 0:
                    open_at = a
                depth += 1
            elif fend is not None and tok.type == fend:
                depth -= 1
                if depth == 0:
                    pieces.append((open_at, b))
            elif tok.type == tokenize.STRING and depth == 0:
                pieces.append((a, b))
        return pieces


# ---------------------------------------------------------------- conversion


def _skip_reason(call: ast.Call, has_import: bool) -> str | None:
    """Why a candidate call is left alone; None to convert. Only called for candidates."""
    if not has_import:
        return "say not imported from app.kria.reply_language"
    if any(k.arg is None for k in call.keywords) or call.args:
        return "call has **kwargs or positional args"
    for name in ("en", "tr"):
        kw = _kw(call, name)
        if kw is None:
            return f"{name} missing"
        if not (_str_const(kw.value) or isinstance(kw.value, ast.JoinedStr)):
            return f"{name} is not a literal ({type(kw.value).__name__})"
    if any(k.arg not in ("en", "tr") for k in call.keywords):
        return "call already has template values"
    for name in ("en", "tr"):
        value = _kw(call, name).value
        if not isinstance(value, ast.JoinedStr):
            continue
        for fv in _fields(value):
            for sub in ast.walk(fv.value):
                if isinstance(sub, ast.JoinedStr):
                    return "nested f-string"
                if isinstance(sub, ast.Lambda | ast.NamedExpr):
                    return "lambda or walrus in f-string"
                if _is_say_call(sub):
                    return "nested say() call"
            if _spec_text(fv) is None:
                return "formatted format spec"
            spec = _spec_text(fv)
            if "{" in spec or "}" in spec:
                return "brace in format spec"
    return None


def _is_candidate(call: ast.Call) -> bool:
    return any(
        k.arg in ("en", "tr") and isinstance(k.value, ast.JoinedStr) for k in call.keywords
    ) or any(
        k.arg in ("en", "tr") and not (_str_const(k.value) or isinstance(k.value, ast.JoinedStr))
        for k in call.keywords
    )


def _assign_names(call: ast.Call) -> tuple[dict[str, str], dict[str, ast.AST]]:
    """expression source -> placeholder name, and placeholder -> expression node (ordered)."""
    taken = set(RESERVED) | {k.arg for k in call.keywords if k.arg}
    names: dict[str, str] = {}
    exprs: dict[str, ast.AST] = {}
    pending: list[tuple[str, ast.AST]] = []
    for kwname in ("en", "tr"):
        value = _kw(call, kwname).value
        if isinstance(value, ast.JoinedStr):
            for fv in _fields(value):
                key = ast.unparse(fv.value)
                if key not in names and key not in dict(pending):
                    pending.append((key, fv.value))
    unnamed: list[tuple[str, ast.AST]] = []
    for key, expr in pending:
        name = _clean(_derive(expr))
        if name is None or name in RESERVED:
            unnamed.append((key, expr))
            names[key] = ""
            continue
        base, n = name, 2
        while name in taken:
            name = f"{base}_{n}"
            n += 1
        taken.add(name)
        names[key] = name
        exprs[name] = expr
    counter = 1
    for key, expr in unnamed:
        while f"v{counter}" in taken:
            counter += 1
        name = f"v{counter}"
        taken.add(name)
        names[key] = name
        exprs[name] = expr
    # keep first-appearance order for the keyword arguments
    ordered = {names[key]: expr for key, expr in pending}
    return names, ordered


def _literal(text: str) -> str:
    return repr(text)


def convert_source(path: Path, text: str, report: Report) -> str | None:
    tree = ast.parse(text)
    calls = say_calls(tree)
    if not calls:
        return None
    rel = str(path.relative_to(API))
    has_import = _imports_say(tree)
    src = Source(text)
    edits: list[tuple[int, int, str]] = []
    for call in calls:
        if not _is_candidate(call):
            continue
        where = f"{rel}:{call.lineno}"
        reason = _skip_reason(call, has_import)
        if reason:
            report.skip(reason, where)
            continue
        names, values = _assign_names(call)
        templated = bool(values)
        new_texts: dict[str, str] = {}
        for kwname in ("en", "tr"):
            node = _kw(call, kwname).value
            whole = _template(node, names, templated)
            # self-check: the template must stand for exactly the original f-string
            rebuilt = _rebuild(whole, values, templated)
            if _norm(rebuilt) != _norm(node):
                raise CodemodError(
                    f"{where}: {kwname} round-trip mismatch\n"
                    f"  original: {_norm(node)}\n  rebuilt:  {_norm(rebuilt)}"
                )
            new_texts[kwname] = whole
            if whole == (node.value if _str_const(node) else None):
                continue  # plain literal with no braces: leave its source alone
            pieces = src.string_pieces(node)
            if not pieces:
                raise CodemodError(f"{where}: no string tokens found for {kwname}")
            piece_templates = []
            for a, b in pieces:
                piece = ast.parse(src.text[a:b].strip(), mode="eval").body
                piece_template = _template(piece, names, templated)
                piece_templates.append(piece_template)
                edits.append((a, b, _literal(piece_template)))
            if "".join(piece_templates) != whole:
                raise CodemodError(f"{where}: {kwname} pieces do not add up to the template")
        last = max(
            (*call.args, *[k.value for k in call.keywords]),
            key=lambda n: (n.end_lineno, n.end_col_offset),
        )
        insert_at = src.ast_offset(last.end_lineno, last.end_col_offset)
        # a keyword value that is a parenthesised string ends before its ")"; insert after the
        # closing paren of that argument instead by scanning tokens up to the call's ")".
        insert_at = _after_argument(src, insert_at, src.span(call)[1])
        added = "".join(f", {name}={ast.unparse(expr)}" for name, expr in values.items())
        if added:
            edits.append((insert_at, insert_at, added))
        for name, expr in values.items():
            bad = _non_pure_calls(expr)
            if bad:
                report.non_pure.append(
                    {"where": where, "placeholder": name, "expr": ast.unparse(expr), "calls": bad}
                )
        report.converted.append(
            {
                "where": where,
                "placeholders": {n: ast.unparse(e) for n, e in values.items()},
                "en": new_texts["en"],
            }
        )
    if not edits:
        return None
    edits.sort()
    for (a1, b1, _), (a2, _b2, _) in zip(edits, edits[1:], strict=False):
        if a2 < b1:
            raise CodemodError(f"{rel}: overlapping edits at {a1}..{b1} and {a2}")
    out = text
    for a, b, new in reversed(edits):
        out = out[:a] + new + out[b:]
    return out


def _after_argument(src: Source, pos: int, call_end: int) -> int:
    """Move ``pos`` past closing parens that belong to the last argument (``en=("a" "b")``)."""
    depth = 0
    result = pos
    for tok in src.tokens:
        a = src.char(*tok.start)
        if a < pos or a >= call_end - 1:
            continue
        if tok.type == tokenize.OP and tok.string in "([{":
            depth += 1
        elif tok.type == tokenize.OP and tok.string in ")]}":
            if depth == 0:
                result = src.char(*tok.end)
            else:
                depth -= 1
        elif tok.type == tokenize.OP and tok.string == ",":
            break
    return result


# ---------------------------------------------------------------- verification


def _fold_back(new_tree: ast.Module) -> ast.Module:
    """Turn every templated say() back into its f-string form (drop the added values)."""
    tree = copy.deepcopy(new_tree)
    for call in say_calls(tree):
        extra = [k for k in call.keywords if k.arg not in ("en", "tr")]
        if not extra or any(k.arg is None for k in extra):
            continue
        values = {k.arg: k.value for k in extra}
        for kw in call.keywords:
            if kw.arg in ("en", "tr") and _str_const(kw.value):
                kw.value = _rebuild(kw.value.value, values, True)
        call.keywords = [k for k in call.keywords if k.arg in ("en", "tr")]
    return tree


def verify_pair(rel: str, old_text: str, new_text: str) -> None:
    old = ast.parse(old_text)
    new = ast.parse(new_text)
    old, new = _fold_back(old), _fold_back(new)
    if _norm(old) != _norm(new):
        # narrow the failure to a call for the error message
        olds, news = say_calls(old), say_calls(new)
        for o, n in zip(olds, news, strict=False):
            if _norm(o) != _norm(n):
                raise CodemodError(
                    f"{rel}:{o.lineno}: verify mismatch\n  old: {_norm(o)}\n  new: {_norm(n)}"
                )
        raise CodemodError(f"{rel}: module differs beyond say() calls")


def verify(rev: str) -> int:
    changed = subprocess.run(
        ["git", "diff", "--name-only", rev, "--", str(APP)],
        cwd=REPO,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.split()
    count = 0
    for name in changed:
        path = REPO / name
        if path.suffix != ".py" or not path.exists():
            continue
        old_text = subprocess.run(
            ["git", "show", f"{rev}:{name}"], cwd=REPO, check=True, capture_output=True, text=True
        ).stdout
        verify_pair(name, old_text, path.read_text(encoding="utf-8"))
        count += 1
    print(json.dumps({"verified_files": count, "against": rev}))
    return 0


# ---------------------------------------------------------------- main


def run(dry_run: bool, report_path: Path | None) -> int:
    report = Report()
    outputs: dict[Path, tuple[str, str]] = {}
    for path in sorted(APP.rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        if "say(" not in text:
            continue
        new = convert_source(path, text, report)
        if new is None:
            continue
        rel = str(path.relative_to(API))
        ast.parse(new)  # must still be Python
        verify_pair(rel, text, new)
        outputs[path] = (text, new)
        report.files_touched.append(rel)
    if not dry_run:
        for path, (_old, new) in outputs.items():
            path.write_text(new, encoding="utf-8")
        for path in outputs:
            ast.parse(path.read_text(encoding="utf-8"))
    data = report.as_json()
    rendered = json.dumps(data, ensure_ascii=False, indent=2)
    if report_path:
        report_path.write_text(rendered + "\n", encoding="utf-8")
        summary = {k: v for k, v in data.items() if k != "converted"}
        print(json.dumps(summary, ensure_ascii=False, indent=2))
    else:
        print(rendered)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--report", type=Path)
    parser.add_argument("--verify", metavar="REV")
    args = parser.parse_args()
    try:
        if args.verify:
            return verify(args.verify)
        return run(args.dry_run, args.report)
    except CodemodError as exc:
        print(f"ABORT: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
