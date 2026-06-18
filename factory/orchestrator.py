import subprocess
import tarfile
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
from pathlib import Path

from . import log
from .config import Config
from .deps import (
    get_dependencies,
    get_dependents,
    run_dependency_analysis,
    run_llm_dependency_analysis,
    topo_sort,
)
from .pipeline import run_story_pipeline, run_story_pipeline_incremental
from .profile import Profile
from .runner import run_agent
from .state import State, spec_hash, specs_combined_hash
from .triage import compute_spec_diff, run_holistic_triage


def run_factory(config: Config, profile: Profile):
    """Main entry point: validate, init, deps, process, summarize."""
    _validate(config)
    _init_workspace(config, profile)
    story_ids = _discover_stories(config)
    state = State(config.state_dir)

    profile.check_prerequisites(config.cmd)

    # Store profile and phase list in state for the monitor
    state.set_metadata("profile", profile.name)
    state.set_metadata("phase_list", profile.phase_list())

    # Handle --rerun / --rerun-all: reset specified stories so they are reprocessed
    if config.rerun_all:
        config.rerun = list(story_ids)
    if config.rerun:
        for sid in config.rerun:
            if sid not in story_ids:
                log.warn(f"--rerun: unknown story '{sid}', skipping")
                continue
            state.clear_phases(sid)
            state.set_story_status(sid, "pending")
            log.info(f"--rerun: reset {sid} for reprocessing")

    log.info("Factory starting")
    log.info(f"  Project:    {config.project_dir}")
    log.info(f"  Specs:      {config.specs_dir}")
    log.info(f"  State:      {config.state_dir}")
    log.info(f"  Stories:    {len(story_ids)}")
    log.info(f"  Parallel:   {config.max_parallel}")
    log.info(f"  Profile:    {profile.name}")
    log.info(f"  Models:     {config.strong_model} (strong), {config.default_model} (default), {config.fast_model} (fast)")
    print("", flush=True)

    # Phase 1: dependency analysis
    if config.llm_deps:
        run_llm_dependency_analysis(config, story_ids, state)
    else:
        run_dependency_analysis(config, story_ids, state)

    # Phase 2: process stories
    if config.max_parallel > 1:
        _process_parallel(config, state, profile)
    else:
        _process_sequential(config, state, profile)

    # Phase 3: summary
    _generate_summary(config, story_ids, state, profile)

    # Cost report
    costs = state.get_total_costs()
    total_in = costs["input_tokens"] + costs["cache_creation_tokens"] + costs["cache_read_tokens"]
    if total_in or costs["output_tokens"]:
        log.info(
            f"Total tokens: {total_in:,} input "
            f"({costs['cache_creation_tokens']:,} cache write, {costs['cache_read_tokens']:,} cache read), "
            f"{costs['output_tokens']:,} output"
        )

    # Remove orphan spec snapshots for deleted stories
    _cleanup_orphan_snapshots(config, story_ids)

    # Auto-commit state if state_dir has its own git context
    _auto_commit_state(config)

    print("", flush=True)
    log.info(f"Factory complete. Results in {config.output_dir}/")


# ── Setup ────────────────────────────────────────────────────────


def _validate(config: Config):
    if not config.specs_dir.is_dir():
        raise SystemExit(f"Specs directory not found: {config.specs_dir}")

    specs = list(config.specs_dir.glob("*.md"))
    if not specs:
        raise SystemExit(f"No .md files found in {config.specs_dir}/")

    log.info(f"Found {len(specs)} specification(s)")


_STATE_GITIGNORE = """\
state.json.lock
.specs_hash
.specs-prev/
stories/*/log/
stories/*/log-archive/
stories/*/commit_msg.txt
output/*.log
"""


def _init_workspace(config: Config, profile: Profile):
    config.state_dir.mkdir(parents=True, exist_ok=True)
    config.stories_dir.mkdir(parents=True, exist_ok=True)
    config.project_dir.mkdir(parents=True, exist_ok=True)
    config.output_dir.mkdir(parents=True, exist_ok=True)

    gitignore = config.state_dir / ".gitignore"
    if not gitignore.exists():
        gitignore.write_text(_STATE_GITIGNORE)

    # Exclude state dir from the project repo so `git add -A` never stages it.
    if config.state_dir.is_relative_to(config.project_dir):
        project_gitignore = config.project_dir / ".gitignore"
        entry = f"/{config.state_dir.relative_to(config.project_dir)}/"
        if project_gitignore.exists():
            content = project_gitignore.read_text()
            if entry not in content.splitlines():
                project_gitignore.write_text(content.rstrip("\n") + "\n" + entry + "\n")
        else:
            project_gitignore.write_text(entry + "\n")

    # Init project using the profile's init command
    profile.init_project(config.project_dir)

    # Init git repo for the project
    if not (config.project_dir / ".git").exists():
        log.info("Initializing git repository")
        author = f"{config.git_author_name} <{config.git_author_email}>"
        subprocess.run(["git", "init", "-q", "-b", "main"], cwd=config.project_dir, capture_output=True)
        subprocess.run(["git", "add", "-A"], cwd=config.project_dir, capture_output=True)
        subprocess.run(
            ["git", "commit", "-q", "-m", "Initial project scaffold", "--author", author],
            cwd=config.project_dir, capture_output=True,
        )


def _discover_stories(config: Config) -> list[str]:
    story_ids = sorted(f.stem for f in config.specs_dir.glob("*.md"))
    log.info(f"Stories: {' '.join(story_ids)}")
    return story_ids


# ── Sequential processing ────────────────────────────────────────


def _detect_trigger(story_id: str, config: Config, state: State) -> str:
    """Determine why a story needs processing."""
    if story_id in config.rerun:
        return "rerun"
    status = state.get_story_status(story_id)
    if status in ("quarantined", "skipped"):
        return "quarantine_retry"
    old_hash = state.get_spec_hash(story_id)
    new_hash = spec_hash(config.specs_dir / f"{story_id}.md")
    if old_hash and old_hash != new_hash:
        return "spec_changed"
    if status == "in_progress":
        return "interrupted_retry"
    return "initial"


def _compute_needs_processing(config, state, order):
    """Pre-compute which stories need reprocessing, their triggers, and pipeline modes.

    Returns (needs_processing, triggers, pipeline_modes) where pipeline_modes
    maps story_id -> "full" or "incremental".
    """
    needs_processing = set()
    triggers = {}
    pipeline_modes = {}
    triage_candidates = {}

    # Pass 1: identify stories that always need full processing
    for sid in order:
        status = state.get_story_status(sid)

        if sid in config.rerun:
            needs_processing.add(sid)
            triggers[sid] = "rerun"
            pipeline_modes[sid] = "full"
            continue

        if status in ("pending", "in_progress"):
            needs_processing.add(sid)
            triggers[sid] = _detect_trigger(sid, config, state)
            pipeline_modes[sid] = "full"
            continue

        if status in ("quarantined", "skipped"):
            needs_processing.add(sid)
            triggers[sid] = _detect_trigger(sid, config, state)
            pipeline_modes[sid] = "full"
            continue

    # Pass 2: identify triage candidates (completed stories with changes)
    diff_cache = {}
    for sid in order:
        if sid in needs_processing:
            continue

        old_hash = state.get_spec_hash(sid)
        new_hash = spec_hash(config.specs_dir / f"{sid}.md")
        own_changed = old_hash != new_hash

        changed_deps = [
            d for d in get_dependencies(sid, config.deps_file)
            if d in needs_processing
        ]

        if not own_changed and not changed_deps:
            continue

        # Build diff info for triage
        own_diff = None
        if own_changed:
            own_diff = compute_spec_diff(sid, config)
            if own_diff is None:
                # No snapshot (first run for this story) — must do full
                needs_processing.add(sid)
                triggers[sid] = "spec_changed"
                pipeline_modes[sid] = "full"
                continue

        dep_diffs = {}
        for dep in changed_deps:
            if dep not in diff_cache:
                diff_cache[dep] = compute_spec_diff(dep, config)
            diff = diff_cache[dep]
            if diff is None:
                # No snapshot for dependency — must do full
                needs_processing.add(sid)
                triggers[sid] = "cascade"
                pipeline_modes[sid] = "full"
                break
            if diff:
                dep_diffs[dep] = diff

        if sid in needs_processing:
            continue

        if own_diff or dep_diffs:
            triage_candidates[sid] = {
                "own_diff": own_diff,
                "dep_diffs": dep_diffs,
                "own_changed": own_changed,
            }

    # Pass 3: holistic triage for all candidates
    if triage_candidates:
        log.phase(f"Running holistic triage for {len(triage_candidates)} candidate(s)...")
        decisions = run_holistic_triage(triage_candidates, config)
        for sid, decision in decisions.items():
            if decision.action == "skip":
                log.info(f"Triage: skip {sid} — {decision.reason}")
            else:
                needs_processing.add(sid)
                info = triage_candidates[sid]
                triggers[sid] = "spec_changed" if info["own_changed"] else "cascade"
                pipeline_modes[sid] = decision.action
                log.info(f"Triage: {decision.action} for {sid} — {decision.reason}")

    return needs_processing, triggers, pipeline_modes


def _process_sequential(config: Config, state: State, profile: Profile):
    order = topo_sort(config.deps_file)
    total = len(order)
    done_count = skip_count = quar_count = uptodate_count = 0

    needs_processing, triggers, pipeline_modes = _compute_needs_processing(config, state, order)

    for i, story_id in enumerate(order, 1):
        log.info(f"{'━' * 3} Story: {story_id} ({i}/{total}) {'━' * 3}")

        # Check failed dependencies
        failed_dep = _find_failed_dependency(story_id, config.deps_file, state)
        if failed_dep:
            log.warn(f"Skipping {story_id}: dependency '{failed_dep}' failed")
            state.skip(story_id, f"dependency {failed_dep} failed")
            skip_count += 1
            continue

        # Skip if up-to-date
        if story_id not in needs_processing:
            log.info(f"Skipping {story_id}: already up to date")
            uptodate_count += 1
            continue

        spec_file = config.specs_dir / f"{story_id}.md"
        new_hash = spec_hash(spec_file)
        state.set_spec_hash(story_id, new_hash)
        state.set_story_status(story_id, "in_progress")

        trigger = triggers.get(story_id, "initial")
        mode = pipeline_modes.get(story_id, "full")
        success = _run_story(config, story_id, spec_file, state, profile,
                             trigger, new_hash, mode)

        if success:
            state.set_story_status(story_id, "done")
            state.end_run(story_id, "done")
            _snapshot_story_spec(config, story_id)
            _archive_run_logs(config, state, story_id)
            done_count += 1
            log.info(f"Story {story_id}: DONE")
        else:
            state.quarantine(story_id, "Pipeline failed")
            state.end_run(story_id, "quarantined", "Pipeline failed")
            _archive_run_logs(config, state, story_id)
            quar_count += 1
            log.error(f"Story {story_id}: QUARANTINED")

    print("", flush=True)
    log.info(
        f"Results: {done_count} done, {uptodate_count} up-to-date, "
        f"{skip_count} skipped, {quar_count} quarantined"
    )


def _run_story(config, story_id, spec_file, state, profile, trigger, new_hash, mode):
    """Run the appropriate pipeline for a story, with auto-escalation."""
    if mode == "incremental":
        for phase in ["plan_delta", "implement", "verify", "commit"]:
            state.set_phase_status(story_id, phase, "pending")
        state.begin_run(story_id, trigger, new_hash, pipeline_mode="incremental")

        success = run_story_pipeline_incremental(
            config, story_id, spec_file, state, profile)

        if not success:
            log.warn(f"[{story_id}] Incremental pipeline failed, escalating to full")
            state.end_run(story_id, "escalated")
            state.clear_phases(story_id)
            state.begin_run(story_id, "escalation", new_hash, pipeline_mode="full")
            success = run_story_pipeline(
                config, story_id, spec_file, state, profile)
    else:
        state.clear_phases(story_id)
        state.begin_run(story_id, trigger, new_hash, pipeline_mode="full")
        success = run_story_pipeline(config, story_id, spec_file, state, profile)

    return success


def _archive_run_logs(config: Config, state: State, story_id: str):
    """Archive the current log directory into a compressed tarball keyed by run number."""
    runs = state.get_runs(story_id)
    if not runs:
        return
    run_number = runs[-1]["run"]

    story_dir = config.stories_dir / story_id
    log_files = sorted(story_dir.glob("log/*.log"))
    if not log_files:
        return

    archive_dir = story_dir / "log-archive"
    archive_dir.mkdir(parents=True, exist_ok=True)
    archive_path = archive_dir / f"run-{run_number}.tar.gz"

    with tarfile.open(archive_path, "w:gz") as tar:
        for lf in log_files:
            tar.add(lf, arcname=lf.name)

    log.info(f"[{story_id}] Archived logs → {archive_path}")


def _find_failed_dependency(story_id: str, deps_file: Path, state: State) -> str | None:
    for dep in get_dependencies(story_id, deps_file):
        status = state.get_story_status(dep)
        if status in ("quarantined", "skipped"):
            return dep
    return None


# ── Parallel processing ──────────────────────────────────────────


def _process_parallel(config: Config, state: State, profile: Profile):
    order = topo_sort(config.deps_file)

    needs_processing, triggers, pipeline_modes = _compute_needs_processing(config, state, order)

    status_map = {}
    for sid in order:
        if sid in needs_processing:
            status_map[sid] = "pending"
        else:
            status_map[sid] = "done"

    futures = {}  # future -> story_id

    def _run_parallel_story(sid, spec_file, trigger, new_hash, mode):
        state.set_story_status(sid, "in_progress")
        return _run_story(config, sid, spec_file, state, profile,
                          trigger, new_hash, mode)

    with ThreadPoolExecutor(max_workers=config.max_parallel) as executor:
        while True:
            # Launch runnable stories
            for sid in order:
                if len(futures) >= config.max_parallel:
                    break
                if status_map[sid] != "pending":
                    continue

                deps = get_dependencies(sid, config.deps_file)
                if all(status_map.get(d) == "done" for d in deps):
                    spec_file = config.specs_dir / f"{sid}.md"
                    new_hash = spec_hash(spec_file)
                    state.set_spec_hash(sid, new_hash)

                    trigger = triggers.get(sid, "initial")
                    mode = pipeline_modes.get(sid, "full")

                    status_map[sid] = "running"
                    log.info(f"Launching parallel {mode} pipeline for {sid}")
                    future = executor.submit(
                        _run_parallel_story, sid, spec_file, trigger, new_hash, mode
                    )
                    futures[future] = sid

            if not futures:
                break

            # Wait for at least one completion
            done_set, _ = wait(futures, return_when=FIRST_COMPLETED)

            for future in done_set:
                sid = futures.pop(future)
                try:
                    success = future.result()
                except Exception as e:
                    success = False
                    log.error(f"Story {sid}: exception: {e}")

                if success:
                    status_map[sid] = "done"
                    state.set_story_status(sid, "done")
                    state.end_run(sid, "done")
                    _snapshot_story_spec(config, sid)
                    _archive_run_logs(config, state, sid)
                    log.info(f"Story {sid}: DONE")
                else:
                    status_map[sid] = "quarantined"
                    state.quarantine(sid, "Pipeline failed")
                    state.end_run(sid, "quarantined", "Pipeline failed")
                    _archive_run_logs(config, state, sid)
                    log.error(f"Story {sid}: QUARANTINED")
                    # Cascade skip
                    for dep_sid in get_dependents(sid, config.deps_file):
                        if status_map.get(dep_sid) == "pending":
                            status_map[dep_sid] = "skipped"
                            state.skip(dep_sid, f"dependency {sid} failed")
                            log.warn(f"Story {dep_sid}: SKIPPED (dependency {sid} failed)")

    # Summary counts
    done_c = sum(1 for s in status_map.values() if s == "done")
    skip_c = sum(1 for s in status_map.values() if s == "skipped")
    quar_c = sum(1 for s in status_map.values() if s == "quarantined")
    print("", flush=True)
    log.info(f"Results: {done_c} done, {skip_c} skipped, {quar_c} quarantined")


# ── Summary ──────────────────────────────────────────────────────


def _generate_summary(config: Config, story_ids: list[str], state: State, profile: Profile):
    log.phase("Generating final summary...")

    template = profile.get_prompt_path("summarize").read_text()

    results_section = ""
    for story_id in story_ids:
        status = state.get_story_status(story_id)
        results_section += f"\n### {story_id} (status: {status})\n\n"
        results_file = config.stories_dir / story_id / "results.md"
        if results_file.exists():
            results_section += results_file.read_text() + "\n"
        else:
            results_section += "No results file.\n"

    prompt = (
        f"{template}\n\n"
        f"## Workspace\n\n"
        f"- Project code: {config.project_dir}/\n"
        f"- Story results: {config.stories_dir}/\n"
        f"- Write the summary to: {config.output_dir}/summary.md\n\n"
        f"## Story Results\n{results_section}"
    )

    log_file = config.output_dir / "summary.log"
    success, usage, _stalled = run_agent(
        prompt=prompt,
        log_file=log_file,
        model=config.fast_model,
        max_turns=config.max_turns,
        workdir=config.project_dir,
        cmd=config.cmd,
        skip_permissions=config.skip_permissions,
        verbose=config.verbose,
        stall_timeout=config.stall_timeout,
    )

    summary_file = config.output_dir / "summary.md"
    if summary_file.exists():
        log.info(f"Summary written to {summary_file}")
    else:
        log.warn("Summary generation did not produce output file")


# ── Spec snapshot & state auto-commit ───────────────────────────


def _snapshot_story_spec(config: Config, story_id: str):
    """Copy a single story's spec to .specs-prev/ after successful processing."""
    import shutil

    spec_file = config.specs_dir / f"{story_id}.md"
    if not spec_file.exists():
        return

    prev_dir = config.state_dir / ".specs-prev"
    prev_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(spec_file, prev_dir / spec_file.name)


def _cleanup_orphan_snapshots(config: Config, story_ids: list[str]):
    """Remove .specs-prev/ entries for specs that no longer exist."""
    prev_dir = config.state_dir / ".specs-prev"
    if not prev_dir.exists():
        return

    current_ids = set(story_ids)
    for snapshot in prev_dir.glob("*.md"):
        if snapshot.stem not in current_ids:
            snapshot.unlink()
            log.info(f"Removed orphan spec snapshot: {snapshot.name}")


def _auto_commit_state(config: Config):
    """Auto-commit state_dir if it has its own git context (separate from project)."""
    try:
        state_root = subprocess.run(
            ["git", "-C", str(config.state_dir), "rev-parse", "--show-toplevel"],
            capture_output=True, text=True,
        )
        project_root = subprocess.run(
            ["git", "-C", str(config.project_dir), "rev-parse", "--show-toplevel"],
            capture_output=True, text=True,
        )
    except FileNotFoundError:
        return

    if state_root.returncode != 0:
        return

    state_git_root = state_root.stdout.strip()
    project_git_root = project_root.stdout.strip() if project_root.returncode == 0 else ""

    # Only auto-commit if state dir has its own separate git context
    if state_git_root == project_git_root:
        return

    status = subprocess.run(
        ["git", "-C", str(config.state_dir), "status", "--porcelain"],
        capture_output=True, text=True,
    )
    if status.returncode != 0 or not status.stdout.strip():
        return

    subprocess.run(
        ["git", "-C", str(config.state_dir), "add", "-A"],
        capture_output=True, text=True,
    )
    subprocess.run(
        ["git", "-C", str(config.state_dir), "commit", "-q", "-m", "Update factory state"],
        capture_output=True, text=True,
    )
    log.info("Auto-committed factory state")
