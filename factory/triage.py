"""Holistic triage: decide per-story whether to run FULL, INCREMENTAL, or SKIP."""

import difflib
import json
import subprocess
from dataclasses import dataclass
from pathlib import Path

from . import log
from .config import Config


@dataclass
class TriageDecision:
    action: str   # "full", "incremental", or "skip"
    reason: str


def compute_spec_diff(spec_id: str, config: Config) -> str | None:
    """Compute unified diff between the pre-update snapshot and the current spec.

    Returns the diff string, or None if there is no snapshot (first run).
    """
    current = config.specs_dir / f"{spec_id}.md"
    snapshot = config.state_dir / ".specs-prev" / f"{spec_id}.md"

    if not snapshot.exists():
        return None
    if not current.exists():
        return None

    old_lines = snapshot.read_text().splitlines(keepends=True)
    new_lines = current.read_text().splitlines(keepends=True)

    diff = list(difflib.unified_diff(
        old_lines, new_lines,
        fromfile=f"specs/{spec_id}.md (previous)",
        tofile=f"specs/{spec_id}.md (current)",
    ))

    return "".join(diff) if diff else ""


def run_holistic_triage(
    candidates: dict[str, dict],
    config: Config,
) -> dict[str, TriageDecision]:
    """Run a single LLM call to triage all candidate stories.

    candidates maps story_id -> {
        "own_diff": str | None,     # diff of the story's own spec, or None
        "dep_diffs": dict[str, str] # dep_id -> diff for changed dependencies
    }

    Returns a dict of story_id -> TriageDecision.
    Defaults to FULL on any failure (conservative).
    """
    if not candidates:
        return {}

    template = (config.factory_dir / "prompts" / "triage.md").read_text()

    spec_diffs_section = _build_diffs_section(candidates, config)
    candidate_stories_section = _build_candidates_section(candidates, config)

    prompt = template.format(
        spec_diffs_section=spec_diffs_section,
        candidate_stories_section=candidate_stories_section,
    )

    log_dir = config.output_dir
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / "triage.log"

    try:
        result = subprocess.run(
            [
                config.cmd, "-p",
                "--output-format", "text",
                "--model", config.fast_model,
                "--max-turns", "1",
                "--allowedTools", "",
                "--dangerously-skip-permissions",
                "-",
            ],
            input=prompt,
            capture_output=True,
            text=True,
            cwd=config.project_dir,
            timeout=120,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError) as e:
        log.warn(f"Triage call failed: {e}")
        _log_triage(log_file, prompt, f"FAILED: {e}", "")
        return _default_full(candidates)

    _log_triage(log_file, prompt, result.stdout, result.stderr)

    if result.returncode != 0:
        log.warn(f"Triage call failed: exit {result.returncode}")
        return _default_full(candidates)

    return _parse_triage_response(result.stdout, candidates)


def _build_diffs_section(candidates: dict[str, dict], config: Config) -> str:
    seen_diffs = set()
    parts = []

    for story_id, info in candidates.items():
        if info.get("own_diff"):
            key = f"own:{story_id}"
            if key not in seen_diffs:
                seen_diffs.add(key)
                parts.append(
                    f"### {story_id} (own spec changed)\n\n"
                    f"```\n{info['own_diff']}\n```\n"
                )

        for dep_id, dep_diff in info.get("dep_diffs", {}).items():
            key = f"dep:{dep_id}"
            if key not in seen_diffs:
                seen_diffs.add(key)
                parts.append(
                    f"### {dep_id} (dependency spec changed)\n\n"
                    f"```\n{dep_diff}\n```\n"
                )

    return "\n".join(parts) if parts else "(no diffs available)\n"


def _build_candidates_section(candidates: dict[str, dict], config: Config) -> str:
    parts = []
    for story_id, info in candidates.items():
        spec_file = config.specs_dir / f"{story_id}.md"
        if not spec_file.exists():
            continue

        triggers = []
        if info.get("own_diff"):
            triggers.append("own spec changed")
        dep_names = list(info.get("dep_diffs", {}).keys())
        if dep_names:
            triggers.append(f"depends on changed: {', '.join(dep_names)}")

        parts.append(
            f"### {story_id}\n\n"
            f"**Reason for candidacy:** {'; '.join(triggers)}\n\n"
            f"{spec_file.read_text()}\n"
        )

    return "\n".join(parts)


def _parse_triage_response(
    output: str, candidates: dict[str, dict],
) -> dict[str, TriageDecision]:
    text = output.strip()

    # Strip markdown fences if the model wrapped the JSON
    if text.startswith("```"):
        lines = text.split("\n")
        lines = [l for l in lines if not l.strip().startswith("```")]
        text = "\n".join(lines)

    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        log.warn("Triage: failed to parse JSON response, defaulting all to FULL")
        return _default_full(candidates)

    decisions = {}
    for story_id in candidates:
        entry = data.get(story_id)
        if not entry or not isinstance(entry, dict):
            log.warn(f"Triage: missing decision for {story_id}, defaulting to FULL")
            decisions[story_id] = TriageDecision(action="full", reason="missing from triage response")
            continue

        action = entry.get("action", "FULL").upper()
        reason = entry.get("reason", "")

        if action not in ("FULL", "INCREMENTAL", "SKIP"):
            log.warn(f"Triage: invalid action '{action}' for {story_id}, defaulting to FULL")
            action = "FULL"

        decisions[story_id] = TriageDecision(action=action.lower(), reason=reason)

    return decisions


def _default_full(candidates: dict[str, dict]) -> dict[str, TriageDecision]:
    return {
        sid: TriageDecision(action="full", reason="triage fallback")
        for sid in candidates
    }


def _log_triage(log_file: Path, prompt: str, stdout: str, stderr: str):
    with open(log_file, "a") as f:
        f.write("=== Holistic Triage ===\n")
        f.write(f"--- prompt ---\n{prompt}\n")
        f.write(f"--- response ---\n{stdout or '(empty)'}\n")
        if stderr:
            f.write(f"--- stderr ---\n{stderr}\n")
        f.write("\n\n")
