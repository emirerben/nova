#!/usr/bin/env python3
"""Validate project agent routing; --runtime also checks installed Codex (no LLM calls)."""

import argparse
import json
import os
from pathlib import Path
import queue
import subprocess
import tempfile
import threading
import time
import tomllib

ROOT = Path(__file__).resolve().parents[1]
EXPECTED = {
    "default": ("cheap-worker", "gpt-5.6-luna"),
    "cheap_worker": ("cheap-worker", "gpt-5.6-luna"),
    "implementer": ("implementer", "gpt-5.6-terra"),
    "debugger": ("debugger", "gpt-6-astra"),
    "explorer": ("explorer", "gpt-5.6-luna"),
}


def require(condition, message):
    if not condition:
        raise ValueError(message)


def static_check():
    config = tomllib.loads((ROOT / ".codex/config.toml").read_text())
    require(config["model"] == "gpt-6-astra", "parent must default to Astra")
    require(config["model_reasoning_effort"] == "low", "parent effort must be low")
    require(config["features"]["multi_agent"] is True, "multi_agent must be enabled")
    require(set(config["agents"]) == set(EXPECTED), "unexpected/missing roles")
    roles = {}
    for name, (filename, model) in EXPECTED.items():
        registration = config["agents"][name]
        require(registration["description"].strip(), f"{name}: missing description")
        relative = f"agents/{filename}.toml"
        require(registration["config_file"] == relative, f"{name}: wrong role path")
        path = ROOT / ".codex" / relative
        role = tomllib.loads(path.read_text())
        require(role["model"] == model, f"{name}: wrong model")
        require(role["model_reasoning_effort"] == "medium", f"{name}: wrong effort")
        require(role["developer_instructions"].strip(), f"{name}: missing instructions")
        sandbox = "read-only" if name == "explorer" else None
        require(role.get("sandbox_mode") == sandbox, f"{name}: wrong sandbox")
        roles[path] = role
    agents = ROOT / "AGENTS.md"
    require(
        agents.is_symlink() and agents.readlink() == Path("CLAUDE.md"),
        "AGENTS.md symlink changed",
    )
    instructions = agents.read_text()
    require(
        "## Codex agent routing" in instructions[:2000], "routing must appear early"
    )
    for marker in (
        "Routine delegation is explicitly authorized",
        'reasoning_effort="medium"',
        'fork_turns="none"',
        "Never silently inherit",
    ):
        require(marker in instructions[:2000], f"routing policy missing {marker}")
    require(len(instructions.encode()) <= 38000, "CLAUDE.md exceeds size budget")
    require(
        (ROOT / "docs/runbooks/codex-agent-routing.md").is_file(), "runbook missing"
    )
    print(
        "PASS static: five registrations, four overlays, models, sandbox, early instructions"
    )
    return config, roles


def runtime_config(cwd, env):
    """Read strict effective config using an isolated local app-server process."""
    with tempfile.TemporaryFile(mode="w+") as errors:
        process = subprocess.Popen(
            ["codex", "app-server", "--strict-config"],
            cwd=cwd,
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=errors,
            text=True,
        )
        incoming = queue.Queue()

        def read_output():
            for line in process.stdout:
                incoming.put(line)
            incoming.put(None)

        reader = threading.Thread(target=read_output, daemon=True)
        reader.start()

        def send(message):
            process.stdin.write(json.dumps(message) + "\n")
            process.stdin.flush()

        def call(identifier, method, params):
            send({"id": identifier, "method": method, "params": params})
            # Bound the whole response wait, even if unsolicited events arrive.
            deadline = time.monotonic() + 25
            while True:
                line = incoming.get(timeout=max(0, deadline - time.monotonic()))
                if line is None:
                    errors.seek(0)
                    raise RuntimeError("app-server exited: " + errors.read()[-2000:])
                result = json.loads(line)
                if result.get("id") == identifier:
                    require("error" not in result, f"{method}: {result.get('error')}")
                    return result["result"]

        try:
            call(
                1,
                "initialize",
                {"clientInfo": {"name": "nova-routing-check", "version": "1"}},
            )
            send({"method": "initialized"})
            return call(2, "config/read", {"cwd": str(cwd), "includeLayers": True})
        finally:
            process.terminate()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
            reader.join(timeout=1)
            process.stdin.close()
            process.stdout.close()


def runtime_check(config, roles):
    with tempfile.TemporaryDirectory(prefix="nova-routing-check-") as home:
        env = dict(os.environ, CODEX_HOME=home)
        home_path = Path(home)
        (home_path / "config.toml").write_text(
            f'[projects.{json.dumps(str(ROOT))}]\ntrust_level = "trusted"\n'
        )
        effective = runtime_config(ROOT, env)["config"]
        for key in ("model", "model_reasoning_effort"):
            require(effective[key] == config[key], f"effective parent {key} mismatch")
        require(
            effective["features"]["multi_agent"] is True,
            "effective multi_agent disabled",
        )
        for name, registration in config["agents"].items():
            actual = effective["agents"][name]
            require(
                Path(actual["config_file"]).resolve()
                == (ROOT / ".codex" / registration["config_file"]).resolve(),
                f"{name}: effective path mismatch",
            )
        print("PASS runtime: strict project config loaded in temporary trusted home")
        prompt = subprocess.run(
            ["codex", "debug", "prompt-input", "Report the agent routing policy."],
            cwd=ROOT,
            env=env,
            capture_output=True,
            text=True,
            timeout=45,
        )
        require(prompt.returncode == 0, "fresh prompt failed: " + prompt.stderr[-2000:])
        rendered = json.dumps(json.loads(prompt.stdout))
        for marker in (
            "## Codex agent routing",
            "Routine delegation is explicitly authorized",
            "Luna Medium",
            "fork_turns",
        ):
            require(marker in rendered, f"fresh prompt missing {marker}")
        print(
            "PASS runtime: fresh CLI prompt includes routing authorization and fallback"
        )
        for path, role in roles.items():
            (home_path / "config.toml").write_text(path.read_text())
            effective = runtime_config(home_path, env)["config"]
            for key, expected in role.items():
                require(
                    effective[key] == expected, f"{path.name}: effective {key} mismatch"
                )
            print(
                f"PASS runtime overlay: {path.name} (configured {role['model']} medium)"
            )
    print(
        "Desktop model selection must be verified separately from actual child metadata."
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--runtime",
        action="store_true",
        help="also validate strict CLI loading and fresh prompt",
    )
    args = parser.parse_args()
    configuration, overlays = static_check()
    if args.runtime:
        runtime_check(configuration, overlays)
