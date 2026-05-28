import argparse
import os
import sys
from pathlib import Path

from .config import Config
from .orchestrator import run_factory


def main():
    parser = argparse.ArgumentParser(
        prog="factory",
        description="AI Software Factory — turns user stories into working Rust code.",
    )
    parser.add_argument(
        "project_dir", type=Path,
        help="Project directory (where generated code lives)",
    )
    parser.add_argument(
        "--specs", type=Path, required=True,
        help="Directory containing .md user story specifications",
    )
    parser.add_argument(
        "--state-dir", type=Path, default=None,
        help="Factory state directory (default: <project-dir>/.factory)",
    )
    parser.add_argument(
        "-j", "--parallel", type=int, default=1,
        help="Max parallel story pipelines (default: 1)",
    )
    parser.add_argument(
        "-r", "--retries", type=int, default=3,
        help="Max verify fix attempts per story (default: 3)",
    )
    parser.add_argument(
        "--strong-model", default=None,
        help="Model for plan + implement (default: claude-opus-4-6)",
    )
    parser.add_argument(
        "--default-model", default=None,
        help="Model for understand, write-tests, verify (default: claude-sonnet-4-6)",
    )
    parser.add_argument(
        "--fast-model", default=None,
        help="Model for dep analysis + summary (default: claude-haiku-4-5)",
    )
    parser.add_argument(
        "--max-turns", type=int, default=100,
        help="Max turns per agent run (default: 100)",
    )
    parser.add_argument(
        "--verify-turns", type=int, default=120,
        help="Max turns for verify phase (default: 120)",
    )
    parser.add_argument(
        "--review-iterations", type=int, default=2,
        help="Max review-implement iterations per story (default: 2)",
    )
    parser.add_argument(
        "--stall-timeout", type=int, default=600,
        help="Kill agent after N seconds without output (default: 600, 0=disabled)",
    )
    parser.add_argument(
        "-v", "--verbose", action="store_true",
        help="Stream agent output to terminal in real time",
    )
    parser.add_argument(
        "--rerun", nargs="+", metavar="STORY",
        help="Force reprocessing of the given story IDs (resets their status and phases)",
    )
    parser.add_argument(
        "--llm-deps", action="store_true",
        help="Use LLM to analyze dependencies instead of parsing from spec files",
    )
    parser.add_argument(
        "--git-author-name", default=None,
        help="Git author name for commits (default: $GIT_AUTHOR_NAME or 'Factory')",
    )
    parser.add_argument(
        "--git-author-email", default=None,
        help="Git author email for commits (default: $GIT_AUTHOR_EMAIL or 'factory@localhost')",
    )

    args = parser.parse_args()

    project_dir = args.project_dir.resolve()
    specs_dir = args.specs.resolve()
    state_dir = args.state_dir.resolve() if args.state_dir else project_dir / ".factory"

    cmd = os.environ.get("FACTORY_CMD", os.environ.get("CLAUDE_CMD", "claude"))

    config = Config(
        project_dir=project_dir,
        specs_dir=specs_dir,
        state_dir=state_dir,
        max_parallel=args.parallel,
        max_retries=args.retries,
        max_review_iterations=args.review_iterations,
        strong_model=args.strong_model or Config.strong_model,
        default_model=args.default_model or Config.default_model,
        fast_model=args.fast_model or Config.fast_model,
        max_turns=args.max_turns,
        verify_turns=args.verify_turns,
        stall_timeout=args.stall_timeout,
        verbose=args.verbose,
        cmd=cmd,
        skip_permissions=os.environ.get("SKIP_PERMISSIONS", "1") == "1",
        rerun=args.rerun or [],
        llm_deps=args.llm_deps,
        git_author_name=args.git_author_name or os.environ.get("GIT_AUTHOR_NAME", "Factory"),
        git_author_email=args.git_author_email or os.environ.get("GIT_AUTHOR_EMAIL", "factory@localhost"),
    )

    try:
        run_factory(config)
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
