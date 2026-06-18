import re
import subprocess
from pathlib import Path

from . import log
from .config import Config
from .profile import Profile
from .runner import run_agent
from .state import State
from .triage import compute_spec_diff


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


def run_story_pipeline(
    config: Config, story_id: str, spec_file: Path, state: State, profile: Profile,
) -> bool:
    """Run all phases for a story. Returns True on success."""
    story_dir = config.stories_dir / story_id
    log_dir = story_dir / "log"
    log_dir.mkdir(parents=True, exist_ok=True)

    log.phase(f"[{story_id}] Starting pipeline")

    phases = [
        ("understand", _run_understand),
        ("plan", _run_plan),
        ("implement", _run_implement),
        ("review", _run_review),
        ("write_tests", _run_write_tests),
        ("verify", _run_verify),
    ]

    for phase_name, phase_fn in phases:
        if not phase_fn(config, story_id, spec_file, story_dir, log_dir, state, profile):
            log.error(f"[{story_id}] Phase '{phase_name}' failed")
            return False

    # Commit (non-fatal)
    if not _run_commit(config, story_id, spec_file, story_dir, log_dir, state, profile):
        log.warn(f"[{story_id}] Commit failed (non-fatal)")

    log.info(f"[{story_id}] Pipeline complete")
    return True


def run_story_pipeline_incremental(
    config: Config, story_id: str, spec_file: Path, state: State, profile: Profile,
) -> bool:
    """Run incremental pipeline for a story with minor changes. Returns True on success."""
    story_dir = config.stories_dir / story_id
    log_dir = story_dir / "log"
    log_dir.mkdir(parents=True, exist_ok=True)

    log.phase(f"[{story_id}] Starting incremental pipeline")

    if not _run_plan_delta(config, story_id, spec_file, story_dir, log_dir, state, profile):
        log.error(f"[{story_id}] Phase 'plan_delta' failed")
        return False

    delta_plan = story_dir / "plan_delta.md"
    if delta_plan.exists() and delta_plan.read_text().startswith("ESCALATE:"):
        log.info(f"[{story_id}] plan_delta requested escalation")
        return False

    if not _run_implement_incremental(config, story_id, spec_file, story_dir, log_dir, state, profile):
        log.error(f"[{story_id}] Phase 'implement' failed (incremental)")
        return False

    if not _run_verify(config, story_id, spec_file, story_dir, log_dir, state, profile):
        log.error(f"[{story_id}] Phase 'verify' failed (incremental)")
        return False

    if not _run_commit(config, story_id, spec_file, story_dir, log_dir, state, profile):
        log.warn(f"[{story_id}] Commit failed (non-fatal)")

    log.info(f"[{story_id}] Incremental pipeline complete")
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


def _run_understand(config, story_id, spec_file, story_dir, log_dir, state, profile):
    output_file = story_dir / "understand.md"
    if _phase_done(state, story_id, "understand", output_file):
        return True

    template = profile.get_prompt_path("understand").read_text()
    context = profile.generate_context(config)
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
        log_dir / "understand.log",
        profile.get_model("understand", config),
        profile.get_max_turns("understand", config),
        output_file=output_file,
    )


def _run_plan(config, story_id, spec_file, story_dir, log_dir, state, profile):
    output_file = story_dir / "plan.md"
    understand_file = story_dir / "understand.md"
    if _phase_done(state, story_id, "plan", output_file):
        return True

    template = profile.get_prompt_path("plan").read_text()
    context = profile.generate_context(config)
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
        log_dir / "plan.log",
        profile.get_model("plan", config),
        profile.get_max_turns("plan", config),
        output_file=output_file,
    )


def _run_plan_delta(config, story_id, spec_file, story_dir, log_dir, state, profile):
    output_file = story_dir / "plan_delta.md"
    if _phase_done(state, story_id, "plan_delta", output_file):
        return True

    template = profile.get_prompt_path("plan_delta").read_text()
    context = profile.generate_context(config)

    understand_file = story_dir / "understand.md"
    plan_file = story_dir / "plan.md"
    understand_content = understand_file.read_text() if understand_file.exists() else "(not available)"
    plan_content = plan_file.read_text() if plan_file.exists() else "(not available)"

    diff = compute_spec_diff(story_id, config)
    diff_section = diff if diff else "(no own-spec diff — triggered by dependency change)"

    prompt = (
        f"{template}\n\n"
        f"## Your Task\n\n"
        f"- Story ID: {story_id}\n"
        f"- Write your delta plan to: {output_file}\n"
        f"- The project code is in: {config.project_dir}/\n\n"
        f"{context}"
        f"## Specification (current)\n\n{spec_file.read_text()}\n\n"
        f"## Spec Changes (diff)\n\n```\n{diff_section}\n```\n\n"
        f"## Previous Understanding\n\n{understand_content}\n\n"
        f"## Previous Plan\n\n{plan_content}"
    )

    return _run_phase(
        config, state, story_id, "plan_delta", prompt,
        log_dir / "plan_delta.log",
        profile.get_model("plan_delta", config),
        profile.get_max_turns("plan_delta", config),
        output_file=output_file,
    )


def _run_implement_incremental(config, story_id, spec_file, story_dir, log_dir, state, profile):
    if _phase_done(state, story_id, "implement"):
        return True

    delta_plan_file = story_dir / "plan_delta.md"
    template = profile.get_prompt_path("implement").read_text()
    context = profile.generate_context(config)
    prompt = (
        f"{template}\n\n"
        f"## Your Task\n\n"
        f"- Story ID: {story_id}\n"
        f"- The project code is in: {config.project_dir}/\n"
        f"- Work inside the project directory. Modify files as needed.\n"
        f"- This is an INCREMENTAL update — existing code already works. "
        f"Apply only the changes described in the delta plan.\n"
        f"- ALSO update or add tests as described in the delta plan's "
        f"Test Updates section.\n\n"
        f"{context}"
        f"## Specification\n\n{spec_file.read_text()}\n\n"
        f"## Delta Plan (changes to apply)\n\n{delta_plan_file.read_text()}"
    )

    def post_check():
        for tool_name in profile.get_phase("implement").post_checks:
            result = profile.run_tool(tool_name, config.project_dir)
            if not result.success:
                log.warn(f"[{story_id}] implement (incremental): {tool_name} failed — {result.summary}")
                if result.details:
                    log.warn(result.details)
                return False
        return True

    return _run_phase(
        config, state, story_id, "implement", prompt,
        log_dir / "implement.log",
        profile.get_model("implement", config),
        profile.get_max_turns("implement", config),
        post_check=post_check,
    )


def _run_implement(config, story_id, spec_file, story_dir, log_dir, state, profile):
    if _phase_done(state, story_id, "implement"):
        return True

    plan_file = story_dir / "plan.md"
    template = profile.get_prompt_path("implement").read_text()
    context = profile.generate_context(config)
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

    def post_check():
        for tool_name in profile.get_phase("implement").post_checks:
            result = profile.run_tool(tool_name, config.project_dir)
            if not result.success:
                log.warn(f"[{story_id}] implement: {tool_name} failed — {result.summary}")
                if result.details:
                    log.warn(result.details)
                return False
        return True

    return _run_phase(
        config, state, story_id, "implement", prompt,
        log_dir / "implement.log",
        profile.get_model("implement", config),
        profile.get_max_turns("implement", config),
        post_check=post_check,
    )


def _parse_review_verdict(review_file: Path) -> str:
    """Parse the verdict from a review.md file. Returns 'PASS' or 'NEEDS_REVISION'."""
    try:
        content = review_file.read_text()
    except OSError:
        return "NEEDS_REVISION"

    in_verdict = False
    for line in content.splitlines():
        stripped = line.strip()
        if stripped.startswith("## Verdict"):
            in_verdict = True
            continue
        if in_verdict and stripped:
            upper = stripped.upper()
            if "NEEDS_REVISION" in upper:
                return "NEEDS_REVISION"
            if upper == "PASS":
                return "PASS"
            break
    return "PASS"


def _run_implement_with_review(config, story_id, spec_file, story_dir, log_dir, state, review_file, profile):
    """Re-run implement with review feedback appended to the prompt."""
    plan_file = story_dir / "plan.md"
    template = profile.get_prompt_path("implement").read_text()
    context = profile.generate_context(config)
    prompt = (
        f"{template}\n\n"
        f"## Your Task\n\n"
        f"- Story ID: {story_id}\n"
        f"- The project code is in: {config.project_dir}/\n"
        f"- Work inside the project directory. Create or modify files as needed.\n\n"
        f"{context}"
        f"## Specification\n\n{spec_file.read_text()}\n\n"
        f"## Implementation Plan\n\n{plan_file.read_text()}\n\n"
        f"## Review Feedback\n\n"
        f"A code review found issues with the current implementation. "
        f"Fix the issues described below while preserving all working code.\n\n"
        f"{review_file.read_text()}"
    )

    def post_check():
        for tool_name in profile.get_phase("implement").post_checks:
            result = profile.run_tool(tool_name, config.project_dir)
            if not result.success:
                log.warn(f"[{story_id}] implement (revision): {tool_name} failed — {result.summary}")
                if result.details:
                    log.warn(result.details)
                return False
        return True

    return _run_phase(
        config, state, story_id, "implement", prompt,
        log_dir / "implement_revision.log",
        profile.get_model("implement", config),
        profile.get_max_turns("implement", config),
        post_check=post_check,
    )


def _run_review(config, story_id, spec_file, story_dir, log_dir, state, profile):
    review_file = story_dir / "review.md"
    plan_file = story_dir / "plan.md"

    if _phase_done(state, story_id, "review", review_file):
        return True

    template = profile.get_prompt_path("review").read_text()
    phase_def = profile.get_phase("review")
    max_iterations = phase_def.max_iterations or config.max_review_iterations

    for iteration in range(1, max_iterations + 1):
        review_file.unlink(missing_ok=True)

        context = profile.generate_context(config)
        prompt = (
            f"{template}\n\n"
            f"## Your Task\n\n"
            f"- Story ID: {story_id}\n"
            f"- Write your review to: {review_file}\n"
            f"- The project code is in: {config.project_dir}/\n\n"
            f"{context}"
            f"## Specification\n\n{spec_file.read_text()}\n\n"
            f"## Implementation Plan\n\n{plan_file.read_text()}"
        )

        if iteration > 1:
            log.phase(f"[{story_id}] review: iteration {iteration}/{max_iterations}")

        log_name = "review.log" if iteration == 1 else f"review.{iteration - 1}.log"
        ok = _run_phase(
            config, state, story_id, "review", prompt,
            log_dir / log_name,
            profile.get_model("review", config),
            profile.get_max_turns("review", config),
            output_file=review_file,
        )

        if not ok:
            return False

        verdict = _parse_review_verdict(review_file)

        if verdict == "PASS":
            return True

        # NEEDS_REVISION: re-run implement with review feedback
        if iteration < max_iterations:
            log.warn(
                f"[{story_id}] review: NEEDS_REVISION "
                f"(iteration {iteration}/{max_iterations}), re-implementing"
            )
            state.set_phase_status(story_id, "implement", "pending")
            if not _run_implement_with_review(
                config, story_id, spec_file, story_dir, log_dir, state, review_file, profile
            ):
                return False
            state.set_phase_status(story_id, "review", "pending")
        else:
            log.warn(
                f"[{story_id}] review: NEEDS_REVISION after "
                f"{max_iterations} iterations, proceeding anyway"
            )
            return True

    return True


def _run_write_tests(config, story_id, spec_file, story_dir, log_dir, state, profile):
    if _phase_done(state, story_id, "write_tests"):
        return True

    template = profile.get_prompt_path("write_tests").read_text()
    context = profile.generate_context(config)
    prompt = (
        f"{template}\n\n"
        f"## Your Task\n\n"
        f"- Story ID: {story_id}\n"
        f"- The project code is in: {config.project_dir}/\n"
        f"- Write tests that validate the acceptance criteria in the specification\n\n"
        f"{context}"
        f"## Specification\n\n{spec_file.read_text()}"
    )

    def post_check():
        for tool_name in profile.get_phase("write_tests").post_checks:
            result = profile.run_tool(tool_name, config.project_dir)
            if not result.success:
                log.warn(f"[{story_id}] write_tests: {tool_name} failed — {result.summary}")
                if result.details:
                    log.warn(result.details)
                return False
        return True

    return _run_phase(
        config, state, story_id, "write_tests", prompt,
        log_dir / "write_tests.log",
        profile.get_model("write_tests", config),
        profile.get_max_turns("write_tests", config),
        post_check=post_check,
    )


def _run_verify(config, story_id, spec_file, story_dir, log_dir, state, profile):
    output_file = story_dir / "results.md"
    if _phase_done(state, story_id, "verify", output_file):
        return True

    template = profile.get_prompt_path("verify").read_text()
    phase_def = profile.get_phase("verify")
    max_retries = phase_def.max_retries or config.max_retries

    for attempt in range(1, max_retries + 1):
        # Remove stale results so the agent can't be misled
        output_file.unlink(missing_ok=True)

        # 1. Environment snapshot — so the agent knows what's available
        env_info = profile.probe_environment(config.project_dir)

        # 2. Pre-run tools (e.g. cargo test) — give the agent structured failure info upfront
        test_passed = True
        test_output = ""
        for tool_name in phase_def.pre_run:
            result = profile.run_tool(tool_name, config.project_dir)
            if not result.success:
                test_passed = False
                test_output = result.output
                break
            elif not test_output:
                test_output = result.output

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
            f"- Maximum fix attempts: {max_retries}\n\n"
            f"## Environment\n\n{env_info}\n\n"
        )

        if diff_stat:
            prompt += f"## Changed Files (since last commit)\n\n```\n{diff_stat}\n```\n\n"

        if test_passed:
            prompt += (
                "## Initial Test Run\n\n"
                "All tests passed. Run any remaining checks (linters, static analysis), "
                "fix any warnings, and write the results summary.\n\n"
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
            log.phase(f"[{story_id}] verify: attempt {attempt}/{max_retries}")

        log_name = "verify.log" if attempt == 1 else f"verify.{attempt - 1}.log"
        ok = _run_phase(
            config, state, story_id, "verify", prompt,
            log_dir / log_name,
            profile.get_model("verify", config),
            profile.get_max_turns("verify", config),
            output_file=output_file,
        )

        if ok:
            return True

        if attempt < max_retries:
            log.warn(
                f"[{story_id}] verify: attempt {attempt}/{max_retries} failed, retrying"
            )
            state.set_phase_status(story_id, "verify", "pending")

    return False


# ── Commit ───────────────────────────────────────────────────────


def _run_commit(config, story_id, spec_file, story_dir, log_dir, state, profile):
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
    commit_msg_file.unlink(missing_ok=True)
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

    commit_model = profile.get_model("commit", config)
    commit_turns = profile.get_max_turns("commit", config)
    success, usage, _stalled = run_agent(
        prompt=prompt,
        log_file=log_dir / "commit.log",
        model=commit_model,
        max_turns=commit_turns,
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
            model=commit_model,
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


