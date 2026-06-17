import subprocess
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
from .pipeline import run_story_pipeline
from .profile import Profile
from .runner import run_agent
from .state import State, spec_hash, specs_combined_hash
from .triage import compute_spec_diff, should_reprocess


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

    # Handle --rerun: reset specified stories so they are reprocessed
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

    # Snapshot specs for next-run triage diffing
    _snapshot_specs(config)

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


def _story_needs_processing(story_id: str, config: Config, state: State) -> bool:
    status = state.get_story_status(story_id)
    if status in ("pending", "quarantined", "skipped", "in_progress"):
        return True
    old_hash = state.get_spec_hash(story_id)
    new_hash = spec_hash(config.specs_dir / f"{story_id}.md")
    return old_hash != new_hash


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
    """Pre-compute which stories need reprocessing and their triggers."""
    needs_processing = set()
    triggers = {}
    diff_cache = {}
    for sid in order:
        if _story_needs_processing(sid, config, state):
            needs_processing.add(sid)
            triggers[sid] = _detect_trigger(sid, config, state)
        elif any(dep in needs_processing for dep in get_dependencies(sid, config.deps_file)):
            changed_deps = [d for d in get_dependencies(sid, config.deps_file) if d in needs_processing]
            if _should_cascade(sid, changed_deps, config, diff_cache):
                needs_processing.add(sid)
                triggers[sid] = "cascade"
                log.info(f"Invalidating {sid}: dependency change is relevant")
            else:
                log.info(f"Skipping {sid}: dependency change is irrelevant (triage)")
    return needs_processing, triggers


def _process_sequential(config: Config, state: State, profile: Profile):
    order = topo_sort(config.deps_file)
    total = len(order)
    done_count = skip_count = quar_count = uptodate_count = 0

    needs_processing, triggers = _compute_needs_processing(config, state, order)

    for i, story_id in enumerate(order, 1):
        log.info(f"{'━' * 3} Story: {story_id} ({i}/{total}) {'━' * 3}")

        # Check failed dependencies
        failed_dep = _find_failed_dependency(story_id, config.deps_file, state)
        if failed_dep:
            log.warn(f"Skipping {story_id}: dependency '{failed_dep}' failed")
            state.skip(story_id, f"dependency {failed_dep} failed")
            skip_count += 1
            continue

        # Incremental: skip if up-to-date
        if story_id not in needs_processing:
            log.info(f"Skipping {story_id}: already up to date")
            uptodate_count += 1
            continue

        # Process
        spec_file = config.specs_dir / f"{story_id}.md"
        new_hash = spec_hash(spec_file)
        if new_hash != state.get_spec_hash(story_id):
            state.clear_phases(story_id)
        state.set_spec_hash(story_id, new_hash)
        state.set_story_status(story_id, "in_progress")

        trigger = triggers.get(story_id, "initial")
        state.begin_run(story_id, trigger, new_hash)

        if run_story_pipeline(config, story_id, spec_file, state, profile):
            state.set_story_status(story_id, "done")
            state.end_run(story_id, "done")
            done_count += 1
            log.info(f"Story {story_id}: DONE")
        else:
            state.quarantine(story_id, "Pipeline failed")
            state.end_run(story_id, "quarantined", "Pipeline failed")
            quar_count += 1
            log.error(f"Story {story_id}: QUARANTINED")

    print("", flush=True)
    log.info(
        f"Results: {done_count} done, {uptodate_count} up-to-date, "
        f"{skip_count} skipped, {quar_count} quarantined"
    )


def _should_cascade(story_id: str, changed_deps: list[str], config: Config, diff_cache: dict) -> bool:
    """Check whether any changed dependency's diff is relevant to this story."""
    for dep in changed_deps:
        if dep not in diff_cache:
            diff_cache[dep] = compute_spec_diff(dep, config)

        diff = diff_cache[dep]
        if diff is None:
            # No stored copy — first run or new spec, must reprocess
            return True
        if diff == "":
            # Spec unchanged (cascade-only invalidation), check transitive
            continue

        reprocess, reason = should_reprocess(story_id, dep, diff, config)
        log.info(f"  Triage {story_id} vs {dep}: {'YES' if reprocess else 'NO'} — {reason}")
        if reprocess:
            return True

    return False


def _find_failed_dependency(story_id: str, deps_file: Path, state: State) -> str | None:
    for dep in get_dependencies(story_id, deps_file):
        status = state.get_story_status(dep)
        if status in ("quarantined", "skipped"):
            return dep
    return None


# ── Parallel processing ──────────────────────────────────────────


def _process_parallel(config: Config, state: State, profile: Profile):
    order = topo_sort(config.deps_file)

    needs_processing, triggers = _compute_needs_processing(config, state, order)

    status_map = {}
    for sid in order:
        if sid in needs_processing:
            status_map[sid] = "pending"
        else:
            status_map[sid] = "done"

    futures = {}  # future -> story_id

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
                    if new_hash != state.get_spec_hash(sid):
                        state.clear_phases(sid)
                    state.set_spec_hash(sid, new_hash)
                    state.set_story_status(sid, "in_progress")

                    trigger = triggers.get(sid, "initial")
                    state.begin_run(sid, trigger, new_hash)

                    status_map[sid] = "running"
                    log.info(f"Launching parallel pipeline for {sid}")
                    future = executor.submit(
                        run_story_pipeline, config, sid, spec_file, state, profile
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
                    log.info(f"Story {sid}: DONE")
                else:
                    status_map[sid] = "quarantined"
                    state.quarantine(sid, "Pipeline failed")
                    state.end_run(sid, "quarantined", "Pipeline failed")
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


def _snapshot_specs(config: Config):
    """Copy current specs to state_dir/.specs-prev/ for next-run triage diffing."""
    import shutil

    prev_dir = config.state_dir / ".specs-prev"
    if prev_dir.exists():
        shutil.rmtree(prev_dir)
    prev_dir.mkdir(parents=True, exist_ok=True)

    for spec_file in config.specs_dir.glob("*.md"):
        shutil.copy2(spec_file, prev_dir / spec_file.name)


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
