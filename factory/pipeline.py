import re
import subprocess
from pathlib import Path

from . import cargo, log
from .config import Config
from .context import generate_context
from .runner import run_agent
from .state import State


def _format_model_display(model: str) -> str:
    """Format model ID as display name: 'claude-sonnet-4-6' → 'Claude Sonnet 4.6'."""
    m = model.lower()
    for family in ("opus", "sonnet", "haiku"):
        if family in m:
            # Extract version: digits/hyphens after the family name
            match = re.search(rf"{family}-(.+)", m)
            version = match.group(1).replace("-", ".") if match else ""
            name = f"Claude {family.capitalize()}"
            if version:
                name += f" {version}"
            return name
    return model


def _co_author_trailer(model: str) -> str:
    """Build a Co-Authored-By trailer for the given model."""
    display = _format_model_display(model)
    return f"Co-Authored-By: {display} <noreply@anthropic.com>"


def run_story_pipeline(config: Config, story_id: str, spec_file: Path, state: State) -> bool:
    """Run all phases for a story. Returns True on success."""
    story_dir = config.stories_dir / story_id
    log_dir = story_dir / "log"
    log_dir.mkdir(parents=True, exist_ok=True)

    log.phase(f"[{story_id}] Starting pipeline")

    phases = [
        ("understand", _run_understand),
        ("plan", _run_plan),
        ("implement", _run_implement),
        ("write_tests", _run_write_tests),
        ("verify", _run_verify),
    ]

    for phase_name, phase_fn in phases:
        if not phase_fn(config, story_id, spec_file, story_dir, log_dir, state):
            log.error(f"[{story_id}] Phase '{phase_name}' failed")
            return False

    # Commit (non-fatal)
    if not _run_commit(config, story_id, spec_file, story_dir, log_dir, state):
        log.warn(f"[{story_id}] Commit failed (non-fatal)")

    log.info(f"[{story_id}] Pipeline complete")
    return True


# ── Phase helpers ────────────────────────────────────────────────


def _phase_done(state: State, story_id: str, phase: str, output_file: Path | None = None) -> bool:
    """Check if a phase is already done. Optionally require output file exists."""
    if state.get_phase_status(story_id, phase) != "done":
        return False
    if output_file and not output_file.exists():
        return False
    log.info(f"[{story_id}] {phase}: already done, skipping")
    return True


_MAX_STALL_RETRIES = 2


def _run_phase(
    config: Config,
    state: State,
    story_id: str,
    phase: str,
    prompt: str,
    log_file: Path,
    model: str,
    max_turns: int,
    output_file: Path | None = None,
    post_check: callable = None,
) -> bool:
    """Generic phase runner with stall retry. Returns True on success."""

    for stall_attempt in range(_MAX_STALL_RETRIES + 1):
        state.set_phase_status(story_id, phase, "running")
        log.info(f"[{story_id}] {phase}: starting")

        activity_file = config.stories_dir / story_id / "activity"
        success, usage, stalled = run_agent(
            prompt=prompt,
            log_file=log_file,
            model=model,
            max_turns=max_turns,
            workdir=config.project_dir,

            cmd=config.cmd,
            skip_permissions=config.skip_permissions,
            verbose=config.verbose,
            activity_file=activity_file,
            stall_timeout=config.stall_timeout,
        )

        # Track costs
        if usage.input_tokens or usage.output_tokens or usage.cache_creation_tokens or usage.cache_read_tokens:
            state.add_cost(
                story_id, phase, usage.input_tokens, usage.output_tokens,
                cache_creation_tokens=usage.cache_creation_tokens,
                cache_read_tokens=usage.cache_read_tokens,
                num_turns=usage.num_turns,
                model=model,
            )

        if stalled and stall_attempt < _MAX_STALL_RETRIES:
            log.warn(
                f"[{story_id}] {phase}: stalled, retrying "
                f"({stall_attempt + 1}/{_MAX_STALL_RETRIES})"
            )
            continue

        if not success:
            state.set_phase_status(story_id, phase, "failed")
            return False

        # Check output file was created
        if output_file and not output_file.exists():
            log.error(f"[{story_id}] {phase}: agent ran but did not produce {output_file}")
            state.set_phase_status(story_id, phase, "failed")
            return False

        # Run optional post-check (e.g., cargo check)
        if post_check and not post_check():
            log.warn(f"[{story_id}] {phase}: post-check failed")
            state.set_phase_status(story_id, phase, "failed")
            return False

        state.set_phase_status(story_id, phase, "done")
        log.info(f"[{story_id}] {phase}: complete")
        return True

    state.set_phase_status(story_id, phase, "failed")
    return False


# ── Phase implementations ────────────────────────────────────────


def _run_understand(config, story_id, spec_file, story_dir, log_dir, state):
    output_file = story_dir / "understand.md"
    if _phase_done(state, story_id, "understand", output_file):
        return True

    template = (config.prompts_dir / "understand.md").read_text()
    context = generate_context(config)
    prompt = (
        f"{template}\n\n"
        f"## Your Task\n\n"
        f"- Story ID: {story_id}\n"
        f"- Write your analysis to: {output_file}\n"
        f"- The project code is in: {config.project_dir}/\n\n"
        f"{context}"
        f"## Specification\n\n{spec_file.read_text()}"
    )

    return _run_phase(
        config, state, story_id, "understand", prompt,
        log_dir / "understand.log", config.default_model, config.max_turns,
        output_file=output_file,
    )


def _run_plan(config, story_id, spec_file, story_dir, log_dir, state):
    output_file = story_dir / "plan.md"
    understand_file = story_dir / "understand.md"
    if _phase_done(state, story_id, "plan", output_file):
        return True

    template = (config.prompts_dir / "plan.md").read_text()
    context = generate_context(config)
    prompt = (
        f"{template}\n\n"
        f"## Your Task\n\n"
        f"- Story ID: {story_id}\n"
        f"- Write your plan to: {output_file}\n"
        f"- The project code is in: {config.project_dir}/\n\n"
        f"{context}"
        f"## Specification\n\n{spec_file.read_text()}\n\n"
        f"## Understanding (from previous analysis)\n\n{understand_file.read_text()}"
    )

    return _run_phase(
        config, state, story_id, "plan", prompt,
        log_dir / "plan.log", config.strong_model, config.max_turns,
        output_file=output_file,
    )


def _run_implement(config, story_id, spec_file, story_dir, log_dir, state):
    if _phase_done(state, story_id, "implement"):
        return True

    plan_file = story_dir / "plan.md"
    template = (config.prompts_dir / "implement.md").read_text()
    context = generate_context(config)
    prompt = (
        f"{template}\n\n"
        f"## Your Task\n\n"
        f"- Story ID: {story_id}\n"
        f"- The project code is in: {config.project_dir}/\n"
        f"- Work inside the project directory. Create or modify files as needed.\n\n"
        f"{context}"
        f"## Specification\n\n{spec_file.read_text()}\n\n"
        f"## Implementation Plan\n\n{plan_file.read_text()}"
    )

    def cargo_check():
        result = cargo.check(config.project_dir)
        if not result.success:
            log.warn(f"[{story_id}] implement: cargo check failed — {result.summary()}")
            if result.format_errors():
                log.warn(result.format_errors())
        return result.success

    return _run_phase(
        config, state, story_id, "implement", prompt,
        log_dir / "implement.log", config.default_model, config.max_turns,
        post_check=cargo_check,
    )


def _run_write_tests(config, story_id, spec_file, story_dir, log_dir, state):
    if _phase_done(state, story_id, "write_tests"):
        return True

    template = (config.prompts_dir / "write_tests.md").read_text()
    context = generate_context(config)
    prompt = (
        f"{template}\n\n"
        f"## Your Task\n\n"
        f"- Story ID: {story_id}\n"
        f"- The project code is in: {config.project_dir}/\n"
        f"- Write tests that validate the acceptance criteria in the specification\n\n"
        f"{context}"
        f"## Specification\n\n{spec_file.read_text()}"
    )

    def cargo_check_tests():
        result = cargo.check(config.project_dir, tests=True)
        if not result.success:
            log.warn(f"[{story_id}] write_tests: cargo check --tests failed — {result.summary()}")
            if result.format_errors():
                log.warn(result.format_errors())
        return result.success

    return _run_phase(
        config, state, story_id, "write_tests", prompt,
        log_dir / "write_tests.log", config.default_model, config.max_turns,
        post_check=cargo_check_tests,
    )


def _run_verify(config, story_id, spec_file, story_dir, log_dir, state):
    output_file = story_dir / "results.md"
    if _phase_done(state, story_id, "verify", output_file):
        return True

    template = (config.prompts_dir / "verify.md").read_text()

    for attempt in range(1, config.max_retries + 1):
        # Remove stale results so the agent can't be misled
        output_file.unlink(missing_ok=True)

        # 1. Environment snapshot — so the agent knows what's available
        env_info = _probe_environment(config.project_dir)

        # 2. Pre-run cargo test — give the agent structured failure info upfront
        test_passed, test_output = cargo.test_verbose(config.project_dir)

        # 3. Git diff — so the agent knows what changed
        diff_stat = subprocess.run(
            ["git", "diff", "--stat", "HEAD"],
            cwd=config.project_dir, capture_output=True, text=True,
        ).stdout.strip()

        prompt = (
            f"{template}\n\n"
            f"## Your Task\n\n"
            f"- Story ID: {story_id}\n"
            f"- The project code is in: {config.project_dir}/\n"
            f"- Write your results summary to: {output_file}\n"
            f"- Maximum fix attempts: {config.max_retries}\n\n"
            f"## Environment\n\n{env_info}\n\n"
        )

        if diff_stat:
            prompt += f"## Changed Files (since last commit)\n\n```\n{diff_stat}\n```\n\n"

        if test_passed:
            prompt += (
                "## Initial Test Run\n\n"
                "All tests passed. Run `cargo clippy`, fix any warnings, "
                "and write the results summary.\n\n"
            )
        else:
            prompt += (
                f"## Initial Test Run (FAILED)\n\n"
                f"Tests were run before your session. Here are the results:\n\n"
                f"```\n{test_output}\n```\n\n"
                f"Fix the failures above. Do NOT re-discover them — start fixing immediately.\n\n"
            )

        prompt += f"## Specification (for reference)\n\n{spec_file.read_text()}"

        if attempt > 1:
            log.phase(f"[{story_id}] verify: attempt {attempt}/{config.max_retries}")

        log_name = "verify.log" if attempt == 1 else f"verify.{attempt - 1}.log"
        ok = _run_phase(
            config, state, story_id, "verify", prompt,
            log_dir / log_name, config.default_model, config.verify_turns,
            output_file=output_file,
        )

        if ok:
            return True

        if attempt < config.max_retries:
            log.warn(
                f"[{story_id}] verify: attempt {attempt}/{config.max_retries} failed, retrying"
            )
            state.set_phase_status(story_id, "verify", "pending")

    return False


# ── Commit ───────────────────────────────────────────────────────


def _run_commit(config, story_id, spec_file, story_dir, log_dir, state):
    if _phase_done(state, story_id, "commit"):
        return True

    project_dir = config.project_dir
    log.info(f"[{story_id}] Commit: starting")

    # Check for changes
    status = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=project_dir, capture_output=True, text=True,
    )
    if status.returncode != 0:
        log.error(f"[{story_id}] Commit: git status failed: {status.stderr.strip()}")
        return False
    if not status.stdout.strip():
        log.info(f"[{story_id}] Commit: no changes to commit")
        state.set_phase_status(story_id, "commit", "done")
        return True

    # Stage all changes
    subprocess.run(["git", "add", "-A"], cwd=project_dir, check=True)

    # Get the actual diff so the LLM sees what changed, not just which files
    MAX_DIFF_LINES = 200
    diff_full = subprocess.run(
        ["git", "diff", "--cached"],
        cwd=project_dir, capture_output=True, text=True,
    ).stdout.strip()
    diff_lines = diff_full.split("\n")
    if len(diff_lines) > MAX_DIFF_LINES:
        diff_text = "\n".join(diff_lines[:MAX_DIFF_LINES]) + "\n[... truncated]"
    else:
        diff_text = diff_full

    spec_title = spec_file.read_text().split("\n")[0].lstrip("# ").strip()

    commit_msg_file = story_dir / "commit_msg.txt"
    prompt = (
        "Write a git commit message to the file specified below. "
        "Output ONLY the commit message file, nothing else.\n\n"
        "Format:\n"
        "- Line 1: subject (max 72 chars, imperative mood, no period, no prefix)\n"
        "- Line 2: blank\n"
        "- Body: 2-4 sentences explaining what was implemented and why\n"
        "- Describe ONLY what the diff actually changes — do not infer or add details not present in the diff\n"
        "- Wrap all body lines at 72 columns\n"
        f"- Last line: Story: {story_id}\n\n"
        f"Spec title: {spec_title}\n\n"
        f"Diff:\n```\n{diff_text}\n```\n\n"
        f"Write to: {commit_msg_file}\n"
    )

    success, usage, _stalled = run_agent(
        prompt=prompt,
        log_file=log_dir / "commit.log",
        model=config.default_model,
        max_turns=5,
        workdir=config.project_dir,

        cmd=config.cmd,
        skip_permissions=config.skip_permissions,
        verbose=config.verbose,
        stall_timeout=config.stall_timeout,
    )

    if usage.input_tokens or usage.output_tokens or usage.cache_creation_tokens or usage.cache_read_tokens:
        state.add_cost(
            story_id, "commit", usage.input_tokens, usage.output_tokens,
            cache_creation_tokens=usage.cache_creation_tokens,
            cache_read_tokens=usage.cache_read_tokens,
            num_turns=usage.num_turns,
            model=config.fast_model,
        )

    # Read generated commit message, or fall back
    if commit_msg_file.exists():
        commit_msg = commit_msg_file.read_text().strip()
    else:
        commit_msg = f"{spec_title}\n\nStory: {story_id}"
        log.warn(f"[{story_id}] Commit: LLM did not produce message, using fallback")

    # Append Co-Authored-By trailer for the model that wrote the code
    trailer = _co_author_trailer(config.strong_model)
    if trailer not in commit_msg:
        commit_msg += f"\n\n{trailer}"

    author = f"{config.git_author_name} <{config.git_author_email}>"
    result = subprocess.run(
        ["git", "commit", "-q", "-m", commit_msg, "--author", author],
        cwd=project_dir, capture_output=True, text=True,
    )

    if result.returncode == 0:
        sha = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=project_dir, capture_output=True, text=True,
        ).stdout.strip()
        state.set_phase_status(story_id, "commit", "done")
        log.info(f"[{story_id}] Commit: {sha}")
        return True
    else:
        log.warn(f"[{story_id}] Commit: failed — {result.stderr.strip()}")
        return False


# ── Environment probe ──────────────────────────────────────────


def _probe_environment(project_dir: Path) -> str:
    """Probe the container environment and return a summary for the agent."""
    lines = []

    def _run(cmd: str) -> str:
        r = subprocess.run(cmd, shell=True, capture_output=True, text=True)
        return r.stdout.strip() or r.stderr.strip()

    # Rust toolchain
    lines.append(f"- Rust: {_run('rustc --version')}")
    lines.append(f"- Cargo: {_run('cargo --version')}")

    # System tools
    for tool in ["gcc", "ip", "unshare", "sudo", "dnsmasq", "rpmbuild", "rpmlint"]:
        which = _run(f"which {tool} 2>/dev/null")
        if which:
            lines.append(f"- {tool}: {which}")
        else:
            lines.append(f"- {tool}: NOT available")

    # User namespace support
    userns = _run("unshare --user --net true 2>&1 && echo supported || echo unsupported")
    lines.append(f"- User namespaces: {userns}")

    return "\n".join(lines)
