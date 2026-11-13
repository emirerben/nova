#!/usr/bin/env python3
"""PreToolUse hook: an agent may run `gh pr merge` only when every check on the
PR's current head has passed or was skipped. Failed, cancelled and still-running
checks block it; so does anything this script cannot read, and any internal
error (exit 2, never 1, which the harness treats as a non-blocking error).

It sees merges inside `(...)`, `$(...)`, backticks, `bash -c`, `eval`, shell
heredocs and `gh api .../pulls/N/merge`. Text that only mentions the command
(commit messages, `--body`, heredoc data) is not a merge.

`--auto` passes: GitHub then waits for the required checks. It still merges at
once when the required checks are already green and only other checks are red
or running. A person can merge past the guard by running the command themselves.
Exit 2 blocks the tool call and shows stderr to the agent.

Timing: each `gh` call gets GH_TIMEOUT seconds and the whole hook BUDGET seconds.
BUDGET must stay below the hook `timeout` in .claude/settings.json, or the
harness kills the hook before it can block.
"""

import json
import re
import shlex
import subprocess
import sys
import time

OK = {"pass", "skipping"}
GH_TIMEOUT = 20
BUDGET = 35
SHELLS = {"bash", "sh", "zsh", "dash", "ksh"}
SEPARATORS = {"&&", "||", ";", ";;", "|", "|&", "&", "(", ")", "{", "}"}
HEREDOC = re.compile(
    r"<<-?[ \t]*([\"']?)([A-Za-z_]\w*)\1[^\n]*\n(.*?)\n[ \t]*\2[ \t]*(?=\n|$)", re.S
)
SUBSTITUTION = re.compile(r"\$\(([^()]*)\)|`([^`]*)`")
API_MERGE = re.compile(r"(?:repos/([^/\s]+/[^/\s]+)/)?pulls/([^/\s]+)/merge\b")


def _scripts(text):
    """Text with heredoc bodies removed, plus the bodies that still execute."""
    run = []

    def cut(match):
        line = text[text.rfind("\n", 0, match.start()) + 1 : match.start()]
        if SHELLS & set(re.findall(r"\w+", line)):
            run.append(match.group(3))
        elif not match.group(1):  # unquoted delimiter: $(...) in the body runs
            run.extend(m.group(1) or m.group(2) for m in SUBSTITUTION.finditer(match.group(3)))
        return text[match.start() : match.start(3)]

    return HEREDOC.sub(cut, text), run


def _newlines_to_semicolons(text):
    out, quote, escaped = [], None, False
    for ch in text:
        if escaped:
            escaped = False
        elif ch == "\\" and quote != "'":
            escaped = True
        elif quote:
            quote = None if ch == quote else quote
        elif ch in "'\"":
            quote = ch
        elif ch == "\n":
            ch = " ; "
        out.append(ch)
    return "".join(out)


def _commands(text, depth=0):
    """Yield the word list of every simple command in `text`, nested ones too."""
    if depth > 4:
        raise ValueError("shell nesting too deep to check")
    text, bodies = _scripts(text)
    for body in bodies:
        yield from _commands(body, depth + 1)
    lexer = shlex.shlex(_newlines_to_semicolons(text), posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    try:
        tokens = [token.strip("`") for token in lexer]
    except ValueError:
        # Unbalanced quotes: cannot parse, so treat any merge text as a merge.
        for found in re.finditer(r"\bgh\s+(?:pr\s+merge|api)\b[^;&|\n]*", text):
            yield found.group(0).split()
        return
    for token in tokens:
        for found in SUBSTITUTION.finditer(token):
            yield from _commands(found.group(1) or found.group(2), depth + 1)
    words = []
    for token in tokens + [";"]:
        if token not in SEPARATORS:
            words.append(token)
            continue
        if words:
            yield words
            for i, word in enumerate(words):
                if word.rsplit("/", 1)[-1] in SHELLS:
                    flag = next((j for j in range(i + 1, len(words)) if re.fullmatch(r"-\w*c\w*", words[j])), None)
                    if flag is not None and flag + 1 < len(words):
                        yield from _commands(words[flag + 1], depth + 1)
                elif word == "eval":
                    yield from _commands(" ".join(words[i + 1 :]), depth + 1)
        words = []


def merges(command):
    """Yield (args, api) for each `gh pr merge` or `gh api` merge call."""
    for words in _commands(command):
        for i, word in enumerate(words):
            if word == "gh" and words[i + 1 : i + 3] == ["pr", "merge"]:
                yield words[i + 3 :], None
            elif word == "gh" and words[i + 1 : i + 2] == ["api"]:
                rest = words[i + 2 :]
                found = next((m for w in rest if (m := API_MERGE.search(w))), None)
                if found:
                    yield [found.group(2), "--repo=" + (found.group(1) or "")], "api"
                elif any("mergePullRequest" in w for w in rest):
                    yield [], "graphql"


def target(args):
    """The PR number, URL or branch the merge names, and its --repo if any."""
    repo, pr, skip = None, None, False
    for i, word in enumerate(args):
        if skip:
            skip = False
        elif word in ("-R", "--repo"):
            repo, skip = (args[i + 1] if i + 1 < len(args) else None), True
        elif word.startswith("--repo="):
            repo = word.split("=", 1)[1] or None
        elif word in (
            "-b",
            "--body",
            "-F",
            "--body-file",
            "-t",
            "--subject",
            "-A",
            "--author-email",
            "--match-head-commit",
        ):
            skip = True
        elif not word.startswith("-") and pr is None:
            pr = word
    return pr, repo


def blockers(pr, repo, cwd, deadline):
    left = deadline - time.monotonic()
    if left <= 0:
        return ["ran out of time reading the checks"]
    cmd = ["gh", "pr", "checks", pr, "--json", "name,bucket"]
    if repo:
        cmd += ["--repo", repo]
    done = subprocess.run(
        cmd, cwd=cwd, capture_output=True, text=True, timeout=min(GH_TIMEOUT, left)
    )
    try:
        checks = json.loads(done.stdout)
    except ValueError:
        return [
            f"could not read the checks: {(done.stderr or done.stdout).strip()[:200]}"
        ]
    if not checks:
        return ["the PR has no checks on its current head yet"]
    return sorted(
        f"{row['name']}: {row['bucket']}" for row in checks if row["bucket"] not in OK
    )


def check(event):
    command = (event.get("tool_input") or {}).get("command") or ""
    deadline = time.monotonic() + BUDGET
    for args, api in merges(command):
        if api != "api" and ("--auto" in args or "--disable-auto" in args):
            continue
        pr, repo = target(args)
        if not pr:
            print(
                "merge-guard: name the PR number in `gh pr merge <number>` so its checks can be read.",
                file=sys.stderr,
            )
            return 2
        try:
            bad = blockers(pr, repo, event.get("cwd") or None, deadline)
        except (OSError, subprocess.SubprocessError) as error:
            bad = [f"could not run gh: {error}"]
        if bad:
            print(
                f"merge-guard: PR {pr} is not green on its current head, so it was not merged.\n  "
                + "\n  ".join(bad)
                + "\nRun the ci-triage skill now (no need to ask): it separates flakes from real failures and gives the local repro."
                "\nWait for running checks. Fix a failure, or rerun the whole run if it was cancelled."
                " If main is red, merge the repair first. Do not retry this merge until every check passes;"
                " tell the user what is red. Only the user can merge past this, by running the command themselves.",
                file=sys.stderr,
            )
            return 2
    return 0


def main():
    try:
        return check(json.load(sys.stdin))
    except Exception as error:  # fail closed: exit 1 would not block
        print(f"merge-guard: internal error, blocking the command: {error!r}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
