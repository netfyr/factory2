"""Live monitoring dashboard for the factory pipeline.

Usage:
    python -m factory.monitor <project-dir>              # live dashboard
    python -m factory.monitor <project-dir> tail          # tail all active logs
    python -m factory.monitor <project-dir> tail story-id # tail one story's logs
    python -m factory.monitor <project-dir> once          # single status snapshot
    python -m factory.monitor <project-dir> history       # run history summary
    python -m factory.monitor <project-dir> history story  # detailed history for one story

The monitor auto-detects the state directory: it checks <path>/.factory/
first, then falls back to <path> itself.
"""

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

# ── Colors ───────────────────────────────────────────────────────

RED = "\033[0;31m"
GREEN = "\033[0;32m"
YELLOW = "\033[0;33m"
BLUE = "\033[0;34m"
CYAN = "\033[0;36m"
BOLD = "\033[1m"
DIM = "\033[2m"
NC = "\033[0m"

STATUS_COLORS = {
    "done": GREEN,
    "in_progress": BLUE,
    "running": BLUE,
    "pending": DIM,
    "quarantined": RED,
    "failed": RED,
    "skipped": YELLOW,
    "-": DIM,
}


def _format_duration(seconds: float) -> str:
    return f"{int(seconds)}s"


def colored_status(s: str, duration_s: float | None = None) -> str:
    color = STATUS_COLORS.get(s, "")
    label = s
    if s == "done" and duration_s is not None:
        label = f"✓ ({_format_duration(duration_s)})"
    return f"{color}{label}{NC}"


def _visible_len(s: str, duration_s: float | None = None) -> int:
    """Return the visible (non-ANSI) length of colored_status output."""
    label = s
    if s == "done" and duration_s is not None:
        label = f"✓ ({_format_duration(duration_s)})"
    return len(label)


def _ansi_overhead(s: str, duration_s: float | None = None) -> int:
    """Return the number of non-visible ANSI bytes added by colored_status()."""
    color = STATUS_COLORS.get(s, "")
    return len(color) + len(NC) if color else 0


def _read_live_usage(path) -> dict | None:
    """Read live usage stats written by the runner, or None if unavailable."""
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None


def format_tokens(n: int) -> str:
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.0f}K"
    return str(n)


# Per-million-token pricing (input, cache_write, cache_read, output)
_MODEL_RATES = {
    "opus": (15, 18.75, 1.50, 75),
    "sonnet": (3, 3.75, 0.30, 15),
    "haiku": (0.80, 1.00, 0.08, 4),
}


def _model_tier(model: str) -> str:
    """Map a model ID like 'claude-opus-4-6' to a tier name."""
    m = model.lower()
    for tier in ("opus", "sonnet", "haiku"):
        if tier in m:
            return tier
    return "sonnet"  # default fallback


def _estimate_cost(cost_entry: dict) -> float:
    tier = _model_tier(cost_entry.get("model", ""))
    inp_rate, cache_w_rate, cache_r_rate, out_rate = _MODEL_RATES[tier]
    inp = cost_entry.get("input_tokens", 0)
    out = cost_entry.get("output_tokens", 0)
    cache_w = cost_entry.get("cache_creation_tokens", 0)
    cache_r = cost_entry.get("cache_read_tokens", 0)
    return (
        inp / 1_000_000 * inp_rate
        + cache_w / 1_000_000 * cache_w_rate
        + cache_r / 1_000_000 * cache_r_rate
        + out / 1_000_000 * out_rate
    )


# ── Status display ───────────────────────────────────────────────


def _terminal_height() -> int:
    """Return terminal height in lines, defaulting to 50."""
    try:
        return os.get_terminal_size().lines
    except (OSError, ValueError):
        return 50


def show_status(state_dir: Path):
    state_file = state_dir / "state.json"
    if not state_file.exists():
        print("Waiting for factory to start (no state.json yet)...")
        return

    data = json.loads(state_file.read_text())
    stories = data.get("stories", {})

    # Summary counts
    counts = {}
    for s in stories.values():
        st = s.get("status", "pending")
        counts[st] = counts.get(st, 0) + 1

    total = len(stories)
    print(f"{BOLD}Factory Status{NC}  {time.strftime('%H:%M:%S')}")
    print(f"{DIM}{'━' * 70}{NC}")

    parts = [f"Total: {BOLD}{total}{NC}"]
    for label, color in [("done", GREEN), ("in_progress", BLUE), ("pending", DIM),
                          ("skipped", YELLOW), ("quarantined", RED)]:
        if counts.get(label, 0):
            parts.append(f"{color}{label}:{counts[label]}{NC}")
    print("  " + "  ".join(parts))
    print()

    # Per-story table
    stories_dir = state_dir / "stories"
    phases = data.get("phase_list", ["understand", "plan", "implement", "review", "write_tests", "verify"])
    # The commit phase is handled separately in pipeline.py and not shown as a column
    phases = [p for p in phases if p != "commit"]

    # Dynamic column width based on longest story ID, capped at 40
    name_width = min(max((len(sid) for sid in stories), default=20) + 2, 40)

    # Header
    header = f"  {BOLD}{'STORY':<{name_width}}{'STATUS':<15}"
    for p in phases:
        header += f"{p:<13}"
    header += f"{'TOKENS':>12}  {'REQS':>5}{NC}"
    print(header)
    print(f"  {DIM}{'─' * (name_width + 15 + 13 * len(phases) + 12 + 7)}{NC}")

    # Decide which stories to show individually vs collapse
    # Reserve lines: header(1) + separator(1) + summary banner(1) + totals(3) + footer(3) + top(5) = ~14
    term_h = _terminal_height()
    overhead_lines = 14
    max_story_rows = max(term_h - overhead_lines, 8)

    sorted_sids = sorted(stories.keys())

    # Separate active (always shown) from inactive (collapsible)
    _collapsible = {"done", "skipped"}
    active_sids = [sid for sid in sorted_sids if stories[sid].get("status", "pending") not in _collapsible]
    inactive_sids = [sid for sid in sorted_sids if stories[sid].get("status", "pending") in _collapsible]

    # Each active story may need 2 lines (activity/reason), budget accordingly
    active_lines = len(active_sids) * 2
    remaining = max(max_story_rows - active_lines, 0)

    def _story_lines(sid):
        """Actual display lines: 2 for skipped-with-reason, 1 otherwise."""
        s = stories[sid]
        if s.get("status") == "skipped" and s.get("skip_reason"):
            return 2
        return 1

    inactive_lines = sum(_story_lines(sid) for sid in inactive_sids)
    if inactive_lines + active_lines <= max_story_rows:
        # Everything fits — show all
        visible_sids = sorted_sids
        collapsed_sids = []
    else:
        # Collapse oldest inactive stories, keep most recent visible
        # Allocate from the end, counting actual lines per story
        visible_inactive = []
        lines_used = 0
        for sid in reversed(inactive_sids):
            needed = _story_lines(sid)
            if lines_used + needed > remaining:
                break
            visible_inactive.append(sid)
            lines_used += needed
        visible_inactive.reverse()
        collapsed_sids = inactive_sids[:len(inactive_sids) - len(visible_inactive)]
        visible_sids = visible_inactive + active_sids
        # Re-sort to maintain order
        order = {sid: i for i, sid in enumerate(sorted_sids)}
        visible_sids.sort(key=lambda sid: order[sid])

    if collapsed_sids:
        n_done = sum(1 for sid in collapsed_sids if stories[sid].get("status") == "done")
        n_skipped = sum(1 for sid in collapsed_sids if stories[sid].get("status") == "skipped")
        parts = []
        if n_done:
            parts.append(f"{n_done} completed")
        if n_skipped:
            parts.append(f"{n_skipped} skipped")
        print(f"  {DIM}... {', '.join(parts)} stories hidden ...{NC}")

    for sid in visible_sids:
        s = stories[sid]
        status = s.get("status", "pending")

        line = f"  {sid:<{name_width}}{colored_status(status):<{15 + _ansi_overhead(status)}}"

        for p in phases:
            phase_data = s.get("phases", {}).get(p, {})
            ps = phase_data.get("status", "-")
            dur = None
            if ps == "done" and phase_data.get("started_at") and phase_data.get("timestamp"):
                try:
                    t0 = datetime.fromisoformat(phase_data["started_at"])
                    t1 = datetime.fromisoformat(phase_data["timestamp"])
                    dur = (t1 - t0).total_seconds()
                except (ValueError, TypeError):
                    pass
            vis = _visible_len(ps, dur)
            pad = 13 - vis
            line += colored_status(ps, dur) + " " * max(pad, 1)

        # Tokens (input = uncached + cache_write + cache_read)
        costs = s.get("costs", {})
        total_in = sum(
            c.get("input_tokens", 0) + c.get("cache_creation_tokens", 0) + c.get("cache_read_tokens", 0)
            for c in costs.values()
        )
        total_out = sum(c.get("output_tokens", 0) for c in costs.values())
        story_turns = sum(c.get("num_turns", 0) for c in costs.values())

        # Add live usage from currently running phase
        live = _read_live_usage(stories_dir / sid / "live_usage")
        if live:
            total_in += live.get("input_tokens", 0) + live.get("cache_creation_tokens", 0) + live.get("cache_read_tokens", 0)
            total_out += live.get("output_tokens", 0)
            story_turns += live.get("num_turns", 0)
        if total_in or total_out:
            line += f"{format_tokens(total_in)}/{format_tokens(total_out):>6}"
        else:
            line += f"{'':>12}"
        if story_turns:
            line += f"  {story_turns:>5}"

        print(line)

        # Show reason if quarantined/skipped, or activity if in progress
        if status == "quarantined":
            reason = s.get("quarantine_reason", "")
            if reason:
                print(f"  {DIM}  └─ {reason}{NC}")
        elif status == "skipped":
            reason = s.get("skip_reason", "")
            if reason:
                print(f"  {DIM}  └─ {reason}{NC}")
        elif status == "in_progress":
            activity_file = stories_dir / sid / "activity"
            if activity_file.exists():
                try:
                    activity = activity_file.read_text().strip()
                    if activity:
                        cur_phase = ""
                        for p in phases:
                            ps = s.get("phases", {}).get(p, {}).get("status", "")
                            if ps == "running":
                                cur_phase = p
                                break
                        prefix = f"{cur_phase}: " if cur_phase else ""
                        full = prefix + activity
                        try:
                            term_w = os.get_terminal_size().columns
                        except (OSError, ValueError):
                            term_w = 120
                        usable = term_w - 7  # "  └─ " prefix
                        if len(full) <= usable:
                            print(f"  {DIM}  └─ {full}{NC}")
                        else:
                            print(f"  {DIM}  └─ {full[:usable]}{NC}")
                            remainder = full[usable:]
                            if len(remainder) > usable:
                                remainder = remainder[:usable - 3] + "..."
                            print(f"  {DIM}     {remainder}{NC}")
                except OSError:
                    pass

    # Total costs
    print()
    all_costs = data.get("stories", {})
    grand_in = grand_cache_w = grand_cache_r = grand_out = 0
    grand_turns = 0
    total_est = 0.0
    for sid, s in all_costs.items():
        for c in s.get("costs", {}).values():
            grand_in += c.get("input_tokens", 0)
            grand_cache_w += c.get("cache_creation_tokens", 0)
            grand_cache_r += c.get("cache_read_tokens", 0)
            grand_out += c.get("output_tokens", 0)
            grand_turns += c.get("num_turns", 0)
            total_est += _estimate_cost(c)
        # Add live usage from currently running phase
        live = _read_live_usage(stories_dir / sid / "live_usage")
        if live:
            grand_in += live.get("input_tokens", 0)
            grand_cache_w += live.get("cache_creation_tokens", 0)
            grand_cache_r += live.get("cache_read_tokens", 0)
            grand_out += live.get("output_tokens", 0)
            grand_turns += live.get("num_turns", 0)

    grand_all_in = grand_in + grand_cache_w + grand_cache_r
    if grand_all_in or grand_out:
        cost_or_turns = f"{DIM}(~${total_est:.2f}){NC}" if total_est else ""
        if grand_turns:
            cost_or_turns = f"{BOLD}{grand_turns}{NC} requests  " + cost_or_turns
        print(
            f"  {BOLD}Total:{NC} {format_tokens(grand_all_in)} input "
            f"{DIM}({format_tokens(grand_cache_w)} write, {format_tokens(grand_cache_r)} read){NC}, "
            f"{format_tokens(grand_out)} output  "
            + cost_or_turns
        )

    # Active log files
    if stories_dir.exists():
        active = []
        for logf in sorted(stories_dir.rglob("*.log")):
            try:
                age = time.time() - logf.stat().st_mtime
                if age < 120:  # modified in last 2 minutes
                    size = logf.stat().st_size
                    rel = logf.relative_to(state_dir)
                    active.append((rel, size))
            except OSError:
                pass

        if active:
            print(f"\n  {BOLD}Active logs:{NC}")
            for rel, size in active:
                print(f"    {CYAN}{rel}{NC} ({format_tokens(size)}B)")


def _format_duration_long(seconds: int | float) -> str:
    """Format seconds as human-readable duration: '12m 30s', '1h 05m', etc."""
    s = int(seconds)
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m {s % 60:02d}s"
    h = s // 3600
    m = (s % 3600) // 60
    return f"{h}h {m:02d}m"


_TRIGGER_LABELS = {
    "initial": "initial",
    "rerun": "manual rerun",
    "spec_changed": "spec changed",
    "cascade": "dependency changed",
    "quarantine_retry": "retry after quarantine",
    "interrupted_retry": "retry after interruption",
}

_OUTCOME_COLORS = {
    "done": GREEN,
    "quarantined": RED,
    "interrupted": YELLOW,
}


def show_history(state_dir: Path, story_filter: str = ""):
    state_file = state_dir / "state.json"
    if not state_file.exists():
        print("No state.json found.")
        return

    data = json.loads(state_file.read_text())
    stories = data.get("stories", {})

    if story_filter:
        # Detailed view for a single story
        if story_filter not in stories:
            print(f"Story '{story_filter}' not found.")
            available = [sid for sid in sorted(stories) if stories[sid].get("runs")]
            if available:
                print("Stories with run history:")
                for sid in available:
                    print(f"  {sid}")
            return

        story = stories[story_filter]
        runs = story.get("runs", [])
        if not runs:
            print(f"No run history for {story_filter}.")
            return

        print(f"\n{BOLD}Run history for {story_filter}{NC} ({len(runs)} run{'s' if len(runs) != 1 else ''})")
        print(f"{DIM}{'━' * 60}{NC}\n")

        total_duration = 0
        total_cost = 0.0

        for run in runs:
            num = run.get("run", "?")
            trigger = _TRIGGER_LABELS.get(run.get("trigger", ""), run.get("trigger", "unknown"))
            outcome = run.get("outcome", "unknown")
            outcome_color = _OUTCOME_COLORS.get(outcome, "")

            print(f"{BOLD}Run {num}{NC} — {trigger}", end="")
            print(f"  {outcome_color}{outcome.upper()}{NC}")

            started = run.get("started_at", "")
            if started:
                try:
                    dt = datetime.fromisoformat(started)
                    print(f"  Started:  {dt.strftime('%Y-%m-%d %H:%M:%S')}")
                except ValueError:
                    print(f"  Started:  {started}")

            duration = run.get("duration_s")
            if duration is not None:
                print(f"  Duration: {_format_duration_long(duration)}")
                total_duration += duration
            elif run.get("finished_at") is None and outcome == "interrupted":
                print(f"  Duration: {DIM}(interrupted){NC}")

            reason = run.get("quarantine_reason")
            if reason:
                print(f"  Reason:   {reason}")

            costs = run.get("costs", {})
            if costs:
                run_in = sum(
                    c.get("input_tokens", 0) + c.get("cache_creation_tokens", 0) + c.get("cache_read_tokens", 0)
                    for c in costs.values()
                )
                run_out = sum(c.get("output_tokens", 0) for c in costs.values())
                run_est = sum(_estimate_cost(c) for c in costs.values())
                total_cost += run_est
                print(f"  Tokens:   {format_tokens(run_in)} in / {format_tokens(run_out)} out (~${run_est:.2f})")

            spec_hash = run.get("spec_hash", "")
            if spec_hash:
                print(f"  Spec:     {spec_hash[:12]}...")

            print()

        # Totals
        print(f"{DIM}{'─' * 60}{NC}")
        parts = [f"{len(runs)} run{'s' if len(runs) != 1 else ''}"]
        if total_duration:
            parts.append(_format_duration_long(total_duration))
        if total_cost:
            parts.append(f"~${total_cost:.2f}")
        print(f"{BOLD}Totals:{NC} {', '.join(parts)}")
        print()

    else:
        # Summary table of all stories
        sorted_sids = sorted(stories.keys())
        has_any_runs = any(stories[sid].get("runs") for sid in sorted_sids)

        if not has_any_runs:
            print("No run history recorded yet.")
            return

        print(f"\n{BOLD}Factory Run History{NC}")
        print(f"{DIM}{'━' * 70}{NC}\n")

        name_width = min(max((len(sid) for sid in sorted_sids), default=20) + 2, 40)
        header = f"  {BOLD}{'STORY':<{name_width}}{'RUNS':>6}  {'CURRENT':<15}{'LAST RUN':<20}{'COST':>10}{NC}"
        print(header)
        print(f"  {DIM}{'─' * (name_width + 53)}{NC}")

        for sid in sorted_sids:
            story = stories[sid]
            runs = story.get("runs", [])
            status = story.get("status", "pending")

            if not runs:
                continue

            last_run = runs[-1]
            last_time = ""
            if last_run.get("finished_at"):
                try:
                    dt = datetime.fromisoformat(last_run["finished_at"])
                    last_time = dt.strftime("%Y-%m-%d %H:%M")
                except ValueError:
                    pass
            elif last_run.get("started_at"):
                try:
                    dt = datetime.fromisoformat(last_run["started_at"])
                    last_time = dt.strftime("%Y-%m-%d %H:%M")
                except ValueError:
                    pass

            total_cost = 0.0
            for run in runs:
                for c in run.get("costs", {}).values():
                    total_cost += _estimate_cost(c)

            cost_str = f"~${total_cost:.2f}" if total_cost else ""

            status_vis = _visible_len(status)
            status_pad = 15 - status_vis
            line = (
                f"  {sid:<{name_width}}"
                f"{len(runs):>6}  "
                f"{colored_status(status)}{' ' * max(status_pad, 1)}"
                f"{last_time:<20}"
                f"{cost_str:>10}"
            )
            print(line)

        print()


def dashboard_loop(state_dir: Path):
    try:
        while True:
            os.system("clear")
            show_status(state_dir)
            print(f"\n{DIM}Refreshing every 2s. Ctrl+C to stop.{NC}")
            time.sleep(2)
    except KeyboardInterrupt:
        pass


# ── Tail logs ────────────────────────────────────────────────────


def tail_logs(state_dir: Path, story_filter: str = ""):
    stories_dir = state_dir / "stories"
    target = stories_dir / story_filter if story_filter else stories_dir

    if not target.exists():
        print(f"No directory found at {target}")
        if stories_dir.exists():
            print("Available stories:")
            for d in sorted(stories_dir.iterdir()):
                if d.is_dir():
                    print(f"  {d.name}")
        sys.exit(1)

    print(f"{BOLD}Tailing logs in: {target}{NC}")
    print(f"{DIM}Ctrl+C to stop{NC}\n")

    # Find existing log files
    logs = sorted(target.rglob("*.log"))
    if not logs:
        print("No log files found yet. Waiting...")
        while not logs:
            time.sleep(1)
            logs = sorted(target.rglob("*.log"))

    try:
        subprocess.run(
            ["tail", "-F"] + [str(l) for l in logs],
            cwd=state_dir,
        )
    except KeyboardInterrupt:
        pass


# ── Main ─────────────────────────────────────────────────────────


def _resolve_state_dir(path: Path) -> Path:
    """Auto-detect the state directory from a given path.

    Checks <path>/.factory/ first, then falls back to <path> itself.
    """
    factory_subdir = path / ".factory"
    if (factory_subdir / "state.json").exists():
        return factory_subdir
    return path


def main():
    parser = argparse.ArgumentParser(description="Factory pipeline monitor")
    parser.add_argument("path", type=Path, help="Project directory or state directory")
    parser.add_argument(
        "action", nargs="?", default="status",
        choices=["status", "tail", "once", "history"],
        help="status (live dashboard), tail (follow logs), once (single snapshot), history (run history)",
    )
    parser.add_argument("story_filter", nargs="?", default="", help="Filter to a specific story")

    args = parser.parse_args()
    state_dir = _resolve_state_dir(args.path.resolve())

    if args.action == "status":
        dashboard_loop(state_dir)
    elif args.action == "tail":
        tail_logs(state_dir, args.story_filter)
    elif args.action == "once":
        show_status(state_dir)
    elif args.action == "history":
        show_history(state_dir, args.story_filter)


if __name__ == "__main__":
    main()
