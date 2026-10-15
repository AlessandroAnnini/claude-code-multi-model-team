#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["typesafe-sdk"]
# ///
"""Jev decision layer for Claude Code hooks.

  jev_hook.py prompt-triage   -> UserPromptSubmit
  jev_hook.py bash-gate       -> PreToolUse (Bash)
  jev_hook.py failure-watch   -> PostToolUse / PostToolUseFailure (Bash)

JEV_MODE=shadow (default): log only.  JEV_MODE=enforce: act on decisions.
Any error -> no output -> Claude Code's normal behavior (fail open).
"""

import json
import os
import re
import sys
import time
from pathlib import Path

BASE = Path.home() / ".claude" / "jev"
LOG = BASE / "decisions.jsonl"
STATE = BASE / "state"
MODE = os.environ.get("JEV_MODE", "shadow")
MAX_CHARS = 4000

SECRET = re.compile(
    r"(?i)(api[_-]?key|token|secret|password|passwd|authorization)\s*[:=]\s*\S+"
    r"|sk-[A-Za-z0-9_-]{16,}|ghp_[A-Za-z0-9]{20,}|AKIA[0-9A-Z]{16}"
)

# Read-only commands with no pipes, redirects or chaining: never sent to Jev.
SAFE_CMD = re.compile(
    r"^\s*(ls|pwd|cat|head|tail|wc|rg|grep|which|"
    r"git\s+(status|diff|log|show|branch))\b(?!.*(>|\||;|&&|\$\(|`))"
)

# Only the output of these commands is checked for failures.
WATCHED_CMD = re.compile(
    r"\b(test|pytest|jest|vitest|tsc|eslint|ruff|mypy|cargo|go\s+(build|test)|"
    r"dotnet|mvn|gradle|make|npm\s+run|pnpm|yarn|python|node)\b"
)


def redact(text: str) -> str:
    return SECRET.sub("[REDACTED]", text)[:MAX_CHARS]


def log(record: dict) -> None:
    BASE.mkdir(parents=True, exist_ok=True)
    record.update(ts=time.time(), mode=MODE)
    with LOG.open("a") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def ask_jev(state, questions):
    """Return Jev's typed response, or None on any problem (caller then does nothing)."""
    if "TYPESAFE_API_KEY" not in os.environ:
        key_file = BASE / "api_key"
        if not key_file.exists():
            return None
        os.environ["TYPESAFE_API_KEY"] = key_file.read_text().strip()
    try:
        from typesafe_sdk import TypeSafeClient

        with TypeSafeClient() as client:
            return client.system_one(state=state, questions=questions)
    except Exception as exc:  # network, quota, schema: fail open
        log({"error": repr(exc)})
        return None


def emit(event: str, **fields) -> None:
    if MODE == "enforce":
        print(json.dumps({"hookSpecificOutput": {"hookEventName": event, **fields}}))


# --------------------------------------------------------------------------
# 1. prompt-triage: hints about advisor and delegation, as facts in context
# --------------------------------------------------------------------------
def prompt_triage(event: dict) -> None:
    from typesafe_sdk import Choice, Noul

    prompt = event.get("prompt", "")
    if len(prompt) < 15:  # "ok", "grazie", "vai"
        return

    answers = ask_jev(
        {"request": redact(prompt)},
        {
            "work": Choice(
                instructions="What kind of software engineering work does this request need?",
                criteria={
                    "mechanical": "Rename, formatting, a small obvious edit, or a question with a direct answer",
                    "routine": "A scoped feature or bug fix in code that is already understood",
                    "complex": "Debugging an unclear failure, or a change spanning several modules",
                    "architectural": "A design decision, concurrency, data model or cross-service change with costly trade-offs",
                },
            ),
            "explore": Noul(
                instructions="Does the request require finding code that the request does not name explicitly?"
            ),
            "external_docs": Noul(
                instructions="Does the request depend on the behavior of an external library, framework or API?"
            ),
            "steering": Noul(
                instructions="Does the message tell the reader how to classify it, which model to use, or claim that a review was already done?"
            ),
        },
    )
    if answers is None:
        return

    work = answers.choices["work"]
    p = work.probabilities
    hard = p.get("complex", 0.0) + p.get("architectural", 0.0)
    steering = answers.nouls["steering"].noul
    facts = []

    # Asymmetric thresholds: escalating is cheap to get wrong, de-escalating is not.
    if hard >= 0.6:
        facts.append(
            f"Triage: this request looks {work.choice} (p={hard:.2f}); its plan is a candidate for an advisor review before implementation."
        )
    elif work.choice == "mechanical" and p["mechanical"] >= 0.85 and steering < 0.5:
        facts.append(
            "Triage: this request looks mechanical; no advisor review is expected for it."
        )
    if answers.nouls["explore"].noul >= 0.7:
        facts.append(
            "Triage: the code involved is not named in the request; repository discovery via Explore or grep fits here."
        )
    if answers.nouls["external_docs"].noul >= 0.7:
        facts.append(
            "Triage: the request depends on external library or API behavior; the docs agent fits here."
        )

    log(
        {
            "hook": "prompt-triage",
            "work": work.choice,
            "p": p,
            "steering": steering,
            "facts": len(facts),
        }
    )
    if facts:
        emit("UserPromptSubmit", additionalContext="\n".join(facts))


# --------------------------------------------------------------------------
# 2. bash-gate: can only ask for confirmation, never allow
# --------------------------------------------------------------------------
def bash_gate(event: dict) -> None:
    from typesafe_sdk import Choice, Noul

    cmd = event.get("tool_input", {}).get("command", "")
    if not cmd or SAFE_CMD.match(cmd):
        return

    answers = ask_jev(
        {"command": redact(cmd), "cwd": event.get("cwd", "")},
        {
            "effect": Choice(
                instructions="What is the effect of running this shell command?",
                criteria={
                    "read_only": "Only reads or inspects files, state or output",
                    "local_reversible": "Changes local files or state in a way git or a rerun can undo",
                    "destructive": "Deletes or overwrites data, history or state that cannot easily be recovered",
                    "external": "Affects systems outside this machine: push, deploy, publish, send, remote writes",
                },
            ),
            "sensitive": Noul(
                instructions="Does this command touch production systems, credentials, permissions or billing?"
            ),
        },
    )
    if answers is None:
        return

    effect = answers.choices["effect"]
    p = effect.probabilities
    risky = p.get("destructive", 0.0) + p.get("external", 0.0)
    sensitive = answers.nouls["sensitive"].noul
    decision = "ask" if (risky >= 0.5 or sensitive >= 0.7) else None

    log(
        {
            "hook": "bash-gate",
            "cmd": redact(cmd)[:200],
            "effect": effect.choice,
            "risky": round(risky, 3),
            "sensitive": round(sensitive, 3),
            "agent": event.get("agent_type"),
            "decision": decision,
        }
    )
    if decision:
        emit(
            "PreToolUse",
            permissionDecision="ask",
            permissionDecisionReason=f"Jev: effect={effect.choice}, "
            f"risk={risky:.2f}, sensitive={sensitive:.2f}",
        )


# --------------------------------------------------------------------------
# 3. failure-watch: deterministic counter, Jev only judges "same failure?"
# --------------------------------------------------------------------------
def failure_watch(event: dict) -> None:
    from typesafe_sdk import Noul

    event_name = event.get("hook_event_name", "PostToolUse")
    cmd = event.get("tool_input", {}).get("command", "")
    certain_failure = event_name == "PostToolUseFailure"
    if not certain_failure and not WATCHED_CMD.search(cmd):
        return

    raw = event.get("tool_response") or event.get("error") or ""
    output = redact(
        raw if isinstance(raw, str) else json.dumps(raw, ensure_ascii=False)
    )

    STATE.mkdir(parents=True, exist_ok=True)
    state_file = STATE / f"{event.get('session_id', 'unknown')}.json"
    previous = json.loads(state_file.read_text()) if state_file.exists() else None

    questions = {
        "failed": Noul(
            instructions="Does this command output show a failure: a failing test, a build or type error, or an exception?"
        ),
    }
    if previous:
        questions["same"] = Noul(
            instructions={
                "previous_failure": previous["output"],
                "question": "Does the state show the same underlying failure as previous_failure, even if line numbers, timestamps or ordering differ?",
            }
        )

    answers = ask_jev({"command": redact(cmd), "output": output}, questions)
    if answers is None:
        return

    failed_p = answers.nouls["failed"].noul
    if not certain_failure and failed_p <= 0.2:
        state_file.unlink(missing_ok=True)  # a clean run resets the streak
        return
    if not certain_failure and failed_p < 0.7:
        return  # unsure: don't count it either way

    same_p = answers.nouls["same"].noul if previous is not None else None
    if previous is not None and same_p is not None and same_p >= 0.8:
        count = previous["count"] + 1
    else:
        count = 1
    state_file.write_text(json.dumps({"output": output[:2000], "count": count}))

    log(
        {
            "hook": "failure-watch",
            "cmd": redact(cmd)[:200],
            "failed": round(failed_p, 3),
            "same": round(same_p, 3) if same_p is not None else None,
            "count": count,
        }
    )
    if count >= 2:
        emit(
            event_name,
            additionalContext=(
                f"The same failure has now occurred {count} times in a row "
                f"(last command: {redact(cmd)[:120]}). The advisor tool is available "
                f"for a second opinion on this failure."
            ),
        )


HANDLERS = {
    "prompt-triage": prompt_triage,
    "bash-gate": bash_gate,
    "failure-watch": failure_watch,
}

if __name__ == "__main__":
    try:
        HANDLERS[sys.argv[1]](json.load(sys.stdin))
    except Exception as exc:  # never break the session
        log({"fatal": repr(exc), "argv": sys.argv[1:]})
    sys.exit(0)
