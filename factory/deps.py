import json
import re
from graphlib import TopologicalSorter, CycleError
from pathlib import Path

from . import log
from .config import Config
from .runner import run_agent
from .state import State, specs_combined_hash

_SPEC_REF = re.compile(r"SPEC-(\d+)")


def parse_deps_from_specs(specs_dir: Path, story_ids: list[str]) -> dict:
    """Parse dependency declarations directly from spec files.

    Each spec is expected to have a ``## Depends on`` section containing
    lines like ``- SPEC-007 (description)`` or ``- SPEC-007``.  The
    three-digit number is matched to story IDs by prefix.

    Returns the same dict shape as deps.json:
    ``{"stories": [...], "dependencies": {"id": [...], ...}}``.
    """
    # Build prefix map: "007" -> "007-policy-types-static-factory"
    prefix_map: dict[str, str] = {}
    for sid in story_ids:
        prefix = sid.split("-", 1)[0]
        prefix_map[prefix] = sid

    dependencies: dict[str, list[str]] = {}

    for spec_file in sorted(specs_dir.glob("*.md")):
        sid = spec_file.stem
        if sid not in story_ids:
            continue

        deps = _extract_dep_refs(spec_file, prefix_map)
        dependencies[sid] = deps

    # Ensure every story has an entry even if its spec wasn't found
    for sid in story_ids:
        dependencies.setdefault(sid, [])

    return {"stories": story_ids, "dependencies": dependencies}


def _extract_dep_refs(spec_file: Path, prefix_map: dict[str, str]) -> list[str]:
    """Extract SPEC-NNN references from the ## Depends on section."""
    try:
        text = spec_file.read_text()
    except OSError:
        return []

    deps: list[str] = []
    in_section = False

    for line in text.splitlines():
        stripped = line.strip()

        if stripped == "## Depends on":
            in_section = True
            continue

        if in_section:
            # Next heading ends the section
            if stripped.startswith("## "):
                break

            for m in _SPEC_REF.finditer(stripped):
                num = m.group(1)
                target = prefix_map.get(num)
                if target and target not in deps:
                    deps.append(target)

    return deps


def run_dependency_analysis(config: Config, story_ids: list[str], state: State):
    """Parse deps from spec files, or skip if specs unchanged."""
    deps_file = config.deps_file
    hash_file = config.state_dir / ".specs_hash"

    # Incremental: skip if specs haven't changed
    if deps_file.exists() and hash_file.exists():
        old_hash = hash_file.read_text().strip()
        new_hash = specs_combined_hash(config.specs_dir)
        if old_hash == new_hash:
            log.info("Dependencies unchanged, skipping analysis")
            return

    log.phase(f"Parsing dependencies from {len(story_ids)} specs...")

    data = parse_deps_from_specs(config.specs_dir, story_ids)

    # Validate: warn about unknown refs
    _warn_unknown_refs(data)

    # Break cycles before writing
    _break_cycles(data)

    config.output_dir.mkdir(parents=True, exist_ok=True)
    deps_file.write_text(json.dumps(data, indent=2))

    log.info(f"Dependencies written to {deps_file}")
    hash_file.write_text(specs_combined_hash(config.specs_dir))


def run_llm_dependency_analysis(config: Config, story_ids: list[str], state: State):
    """Run LLM to produce deps.json (optional validation mode)."""
    deps_file = config.deps_file

    log.phase(f"Running LLM dependency analysis across {len(story_ids)} stories...")

    prompt_template = (config.prompts_dir / "analyze_deps.md").read_text()

    specs_section = ""
    for spec_file in sorted(config.specs_dir.glob("*.md")):
        sid = spec_file.stem
        specs_section += f"\n### {sid}\n\n{spec_file.read_text()}\n\n---\n"

    prompt = (
        f"{prompt_template}\n\n"
        f"## Specifications\n{specs_section}\n\n"
        f"Write the dependency files to:\n"
        f"- {config.state_dir}/deps.md (human-readable analysis)\n"
        f"- {deps_file} (machine-readable, format specified above)\n"
    )

    config.output_dir.mkdir(parents=True, exist_ok=True)
    log_file = config.output_dir / "deps_analysis.log"

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

    if not success:
        log.warn("LLM dependency analysis failed")

    if not deps_file.exists():
        log.warn("deps.json not produced, creating fallback with no dependencies")
        _create_fallback_deps(deps_file, story_ids)
    else:
        try:
            data = json.loads(deps_file.read_text())
            assert "stories" in data and "dependencies" in data
        except (json.JSONDecodeError, AssertionError):
            log.warn("deps.json has invalid structure, creating fallback")
            _create_fallback_deps(deps_file, story_ids)

    hash_file = config.state_dir / ".specs_hash"
    hash_file.write_text(specs_combined_hash(config.specs_dir))


def _warn_unknown_refs(data: dict):
    """Log warnings for references to stories not in the story list."""
    stories = set(data["stories"])
    for sid, dep_list in data["dependencies"].items():
        for dep in dep_list:
            if dep not in stories:
                log.warn(f"  {sid}: depends on unknown story '{dep}'")


def _break_cycles(data: dict):
    """Detect and break dependency cycles by removing the weakest edge.

    When a cycle is found, the edge FROM the higher-numbered story is
    removed (higher number = declared later = likely the weaker dep).
    Repeats until the graph is acyclic.
    """
    deps = data["dependencies"]

    while True:
        graph = {s: set(deps.get(s, [])) for s in data["stories"]}
        try:
            list(TopologicalSorter(graph).static_order())
            return  # no cycles
        except CycleError as e:
            # e.args[1] is the cycle list: [a, b, ..., a]
            cycle = e.args[1]
            # Remove edge from the highest-numbered node in the cycle
            # to its dependency that's also in the cycle
            cycle_set = set(cycle)
            # Pick the node with the highest numeric prefix
            highest = max(
                (n for n in cycle_set),
                key=lambda n: n.split("-", 1)[0],
            )
            # Find which of its deps is in the cycle and remove that edge
            for dep in list(deps.get(highest, [])):
                if dep in cycle_set:
                    deps[highest].remove(dep)
                    log.warn(
                        f"  Cycle broken: removed {highest} -> {dep} "
                        f"(fix the '## Depends on' in one of these specs)"
                    )
                    break


def _create_fallback_deps(deps_file: Path, story_ids: list[str]):
    data = {
        "stories": story_ids,
        "dependencies": {sid: [] for sid in story_ids},
    }
    deps_file.write_text(json.dumps(data, indent=2))


def topo_sort(deps_file: Path) -> list[str]:
    """Return story IDs in dependency order (dependencies first)."""
    data = json.loads(deps_file.read_text())
    stories = data["stories"]
    dependencies = data["dependencies"]

    graph = {s: set(dependencies.get(s, [])) for s in stories}

    sorter = TopologicalSorter(graph)
    try:
        return list(sorter.static_order())
    except CycleError as e:
        log.warn(f"Cycle detected in dependencies: {e}")
        return stories


def get_dependencies(story_id: str, deps_file: Path) -> list[str]:
    data = json.loads(deps_file.read_text())
    return data.get("dependencies", {}).get(story_id, [])


def get_dependents(story_id: str, deps_file: Path) -> list[str]:
    data = json.loads(deps_file.read_text())
    return [
        sid
        for sid, deps in data.get("dependencies", {}).items()
        if story_id in deps
    ]
