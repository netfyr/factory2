# factory2

An AI software factory that turns user stories into working Rust code. It reads specifications from a directory, analyzes dependencies between them, and processes each story through a multi-phase pipeline powered by Claude Code.

## How it works

```
specs/*.md ──► dependency analysis ──► per-story pipeline ──► working Rust project
                                            │
                                            ├─ understand (gap analysis)
                                            ├─ plan (implementation design)
                                            ├─ implement (write code)
                                            ├─ review (spec adherence + correctness)
                                            ├─ write-tests (from acceptance criteria)
                                            ├─ verify (run tests, fix, repeat)
                                            └─ commit (git commit with summary)
```

1. **Dependency analysis** — the factory parses `## Depends on` sections from each spec file to build a dependency graph (`deps.json`), determining which stories must be implemented first. This is deterministic and instant — no LLM call required.

2. **Per-story pipeline** — each story passes through six phases, each a separate Claude Code invocation:

   | Phase | Input | Output | Purpose |
   |-------|-------|--------|---------|
   | understand | spec + codebase | `understand.md` | Gap analysis: what exists, what's missing |
   | plan | spec + understand.md | `plan.md` | Concrete implementation plan with file paths and signatures |
   | implement | spec + plan.md | Rust code | Write the code, ensure it compiles |
   | review | spec + plan + code | `review.md` | Check spec adherence, correctness, edge cases |
   | write-tests | spec + code | Test code | Tests derived from acceptance criteria |
   | verify | spec + code + tests | `results.md` | Run tests, fix failures, report results |
   | commit | results.md | git commit | Commit with message explaining what and why |

   The review phase produces a verdict (PASS or NEEDS_REVISION). On NEEDS_REVISION, the implement phase re-runs with the review feedback, then review runs again — up to `--review-iterations` times (default 2). After exhausting iterations, the pipeline proceeds to write-tests regardless. This catches design-level issues before the more expensive test-fix-retry loop in verify.

3. **Summary** — after all stories are processed, a final LLM run produces a combined summary of what was built.

### Failure handling

- If a story fails any phase, it is **quarantined** and all stories that depend on it are **skipped**.
- The verify phase retries up to N times (default 3) before quarantining.
- Other independent stories continue processing.

### Incremental runs

Run the factory again on the same workspace and it will:
- Skip stories that completed successfully and whose spec hasn't changed.
- Re-attempt quarantined and skipped stories (in case specs were fixed).
- Re-run dependency analysis only if any spec file changed.

To force reprocessing of specific stories (even if their spec hasn't changed):

```bash
python3 -m factory ./my-project --rerun 045-rpm-packaging
python3 -m factory ./my-project --rerun 045-rpm-packaging 048-readme
```

This resets their status and phases, then runs the normal pipeline. Stories that depend on a rerun story are automatically invalidated and reprocessed too.

### Cost tracking

Token usage (input and output) is tracked per story and per phase in `state.json`. The monitor displays totals with estimated cost.

## Prerequisites

- Python 3.11+
- [Claude Code CLI](https://docs.anthropic.com/en/docs/claude-code) (`claude`)
- Rust toolchain (`cargo`, `clippy`)

## Quick start

### 1. Create specs

Create a directory with one `.md` file per user story:

```
my-project/
└── specs/
    ├── auth.md
    ├── user-profile.md
    └── api-endpoints.md
```

Each spec should include acceptance criteria and dependency declarations:

```markdown
# User Authentication

As a user, I want to log in with username and password so that I can
access protected resources.

## Acceptance Criteria

- POST /login accepts a JSON body with `username` and `password` fields
- Returns a JWT token on successful authentication
- Returns 401 with an error message on invalid credentials
- Passwords are verified against bcrypt hashes
- Tokens expire after 24 hours

## Depends on

- SPEC-001 (Workspace setup)
- SPEC-003 (Database models)
```

### Dependency format

The `## Depends on` section declares which stories must be implemented before this one. The factory parses this section to build the dependency graph — no LLM call is involved.

**Format rules:**
- Each dependency is a line containing `SPEC-NNN` where `NNN` is the numeric prefix of the story filename (e.g., `SPEC-007` matches `007-policy-types.md`)
- The parenthesized description after the spec reference is optional and ignored by the parser — it's for human readers
- Use `(none)` or leave the section empty for root stories with no dependencies
- If specs form a cycle (A depends on B, B depends on A), the factory breaks it automatically by removing the edge from the higher-numbered story, and logs a warning so you can fix the spec

**Examples:**
```markdown
## Depends on
- SPEC-002 (Entity types)
- SPEC-005

## Depends on
(none)
```

To use the old LLM-based analysis instead (sends all specs to an LLM in a single prompt), pass `--llm-deps`.

### Specification guidelines

Each spec is processed in isolation — when the factory works on a story, it sees only that story's spec and the current codebase. It does not read other specs. This has important consequences for how specs should be written:

**Self-contained stories.** Every rule, convention, or constraint that applies to a story must appear explicitly in that story's spec. If multiple stories share a common pattern (e.g., how integration tests are structured, how errors are handled, what naming conventions to follow), repeat those rules in every spec that needs them. Do not write "see SPEC-001 for test conventions" — the factory will not look at SPEC-001 while implementing SPEC-102. Copy the relevant sections verbatim.

**Explicit test instructions.** If a story requires running integration tests (beyond `cargo test`), the spec must include:
- The exact command to run (e.g., `make integration-test SPEC=102`)
- What tools or prerequisites are needed (e.g., `unshare`, `dnsmasq`)
- What to do if a prerequisite is missing (fail, not skip)
- How to interpret results

Without explicit instructions, the factory may skip tests or report success when test tools are unavailable.

**Scoped verification.** Each story should only run tests related to the functionality it implements and the functionality it depends on. Do not run the entire test suite. Use filtering (e.g., `make integration-test SPEC=NNN` or `cargo test -p crate-name`) to limit test scope.

The reason: when a spec is modified and the factory reprocesses an already-implemented story, the project may now contain code and tests from later stories. If an unrelated test from a later story is broken, it should be fixed when the factory processes that later story — not when it re-verifies the current one. Running the full test suite creates false failures that block progress.

**Example of a well-structured spec:**

```markdown
# SPEC-102: Query Ethernet Interfaces

## What
Implement the query method for ethernet interfaces using rtnetlink.

## Implementation details
- Crate: netfyr-backend
- Files: src/ethernet.rs
- Dependencies: rtnetlink, tokio

## Verification
Run `cargo test -p netfyr-backend` and `make integration-test SPEC=102`.
If `unshare` is not available, the integration test must print
"FAIL: unshare not available" to stderr and exit 1 (never exit 0).

## Acceptance criteria
...

## Depends on
- SPEC-101 (Backend trait)
```

### 2. Run the factory

```bash
# Anthropic API
export ANTHROPIC_API_KEY="sk-ant-..."
python3 -m factory ./my-project --specs ./my-specs

# OR Vertex AI
export CLAUDE_CODE_USE_VERTEX=1
export CLOUD_ML_REGION=us-east5
export ANTHROPIC_VERTEX_PROJECT_ID=your-project-id
python3 -m factory ./my-project --specs ./my-specs
```

### 3. Check results

The generated project lives directly in the project directory. Factory state (pipeline progress, stories, logs) lives in `.factory/` inside the project:

```
my-project/                   # the generated Rust project (git repo)
├── Cargo.toml
├── src/
├── tests/
└── .factory/                 # factory state (add to .gitignore or use a separate branch)
    ├── state.json            # pipeline state + cost tracking
    ├── deps.json             # dependency graph (machine-readable)
    ├── .specs-prev/          # spec snapshot for triage diffing
    ├── stories/
    │   └── auth/
    │       ├── understand.md # gap analysis
    │       ├── plan.md       # implementation plan
    │       ├── results.md    # test results
    │       └── log/          # raw Claude output per phase
    └── output/
        └── summary.md        # combined summary of all stories

my-specs/                     # your input (unchanged, kept separate)
├── auth.md
├── user-profile.md
└── api-endpoints.md
```

### Sharing factory state via a git branch

If you want to version factory state (stories, plans, logs) without cluttering the project's main branch, use a git worktree:

```bash
# Initial setup (once)
cd my-project

# Create a new branch with no history (orphan = no parent commit).
# This branch will hold only factory state, completely separate from
# the project's commit history.
git checkout --orphan factory-state

# The orphan checkout still has the project's files staged.
# Remove them — this branch should start empty.
git rm -rf .
git commit --allow-empty -m "Factory state branch"

# Switch back to main so the project code is visible again.
git checkout main

# Attach the factory-state branch as a "worktree" at .factory/.
# A worktree is a second working directory backed by the same repo
# but checked out to a different branch. Writes to .factory/ are
# tracked by the factory-state branch; writes to the project root
# are tracked by main. One repo, two branches, two directories.
git worktree add .factory factory-state

# Run the factory — state goes to .factory/, code goes to project root
python3 -m factory . --specs ../my-specs

# The factory auto-commits state to the factory-state branch.
# Push it separately:
git -C .factory push origin factory-state
```

On a new machine, restore the worktree:

```bash
git clone <repo-url> my-project
cd my-project
git worktree add .factory factory-state
```

## Options

```
usage: factory [-h] --specs DIR [--state-dir DIR]
               [-j PARALLEL] [-r RETRIES]
               [--strong-model MODEL] [--default-model MODEL] [--fast-model MODEL]
               [--max-turns N] [--verify-turns N] [-v] [--llm-deps]
               [--git-author-name NAME] [--git-author-email EMAIL]
               [--rerun STORY [STORY ...]]
               project-dir

      --specs DIR          Directory containing .md spec files (required)
      --state-dir DIR      Factory state directory (default: <project-dir>/.factory)
  -j, --parallel N         Max parallel story pipelines (default: 1)
  -r, --retries N          Max verify fix attempts per story (default: 3)
      --review-iterations N Max review-implement iterations per story (default: 2)
      --strong-model MODEL Model for plan phase (default: claude-opus-4-6)
      --default-model MODEL Model for understand, implement, write-tests, verify (default: claude-sonnet-4-6)
      --fast-model MODEL   Model for dep analysis, summary, commit messages (default: claude-haiku-4-5)
      --max-turns N        Max turns per Claude run (default: 100)
      --verify-turns N     Max turns for verify phase (default: 120)
  -v, --verbose            Stream Claude output to terminal in real time
      --rerun STORY [STORY ...]  Force reprocessing of specific stories
      --llm-deps           Use LLM for dependency analysis instead of parsing specs
      --git-author-name    Git author name for commits (default: Factory)
      --git-author-email   Git author email for commits (default: factory@localhost)
  -h, --help               Show this help
```

Examples:

```bash
# Watch what Claude is doing in real time
python3 -m factory ./my-project --specs ./specs -v

# More retries for verify, higher turn budget
python3 -m factory ./my-project --specs ./specs -r 5 --verify-turns 150

# Override model for a specific tier
python3 -m factory ./my-project --specs ./specs --default-model claude-opus-4-6

# Set git author for commits
python3 -m factory ./my-project --specs ./specs --git-author-name "My Bot" --git-author-email "bot@example.com"

# Use a custom state directory
python3 -m factory ./my-project --specs ./specs --state-dir ./my-state
```

## Monitoring

While the factory is running, open a second terminal:

```bash
# Live dashboard — refreshes every 2s with per-story, per-phase status and costs
# (auto-detects .factory/ inside the project directory)
python3 -m factory.monitor ./my-project

# Tail logs for a specific story
python3 -m factory.monitor ./my-project tail auth

# One-shot status (for scripting)
python3 -m factory.monitor ./my-project once
```

The dashboard shows per-story phase status, token usage, and estimated cost:
```
Factory Status  14:23:07
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  Total: 3  done:2  in_progress:1

  STORY                 STATUS         understand  plan        implement   write_tests verify      TOKENS   REQS
  ─────────────────────────────────────────────────────────────────────────────────────────────────────────────────
  auth                  done           done        done        done        done        done      120K/25K    42
  profile               in_progress    done        done        running     -           -          68K/12K    23
    └─ implement: Writing profile validation logic
  api                   pending        -           -           -           -           -

  Total: 188K input (45K write, 95K read), 37K output  42 requests  (~$1.24)

  Active logs:
    stories/profile/log/implement.log (52KB)
```

## Running in a container

For isolated execution (or when tests need root capabilities like network namespaces):

```bash
# Build and run
export ANTHROPIC_API_KEY="sk-ant-..."  # or set Vertex AI vars
./run_container.sh ./specs-dir

# Options
./run_container.sh \
  -o ./output-dir \        # project/output location (default: ./factory-output)
  -i my-image \            # image name (default: factory2)
  -b \                     # force rebuild image
  -R podman \              # runtime: docker or podman (default: auto-detect)
  ./specs-dir \
  -- --strong-model claude-opus-4-6   # factory options after --

# Stop a running factory
podman kill factory2-run    # or: docker kill factory2-run
```

The container runs with `--cap-add=NET_ADMIN --cap-add=SYS_ADMIN` instead of `--privileged`. A non-root user (`factory`) runs Claude Code, with passwordless `sudo` for commands that need root (e.g., network namespaces in tests).

### Vertex AI in containers

The container script auto-copies GCP credentials into the project directory:

```bash
export CLAUDE_CODE_USE_VERTEX=1
export CLOUD_ML_REGION=us-east5
export ANTHROPIC_VERTEX_PROJECT_ID=your-project-id

# Uses ~/.config/gcloud/application_default_credentials.json automatically.
# Or set GOOGLE_APPLICATION_CREDENTIALS to a service account key file.
./run_container.sh ./specs-dir
```

## Environment variables

| Variable | Purpose |
|----------|---------|
| `ANTHROPIC_API_KEY` | Anthropic API authentication |
| `CLAUDE_CODE_USE_VERTEX` | Set to `1` to use Vertex AI |
| `CLOUD_ML_REGION` | Vertex AI region |
| `ANTHROPIC_VERTEX_PROJECT_ID` | GCP project ID for Vertex AI |
| `CLAUDE_CMD` | Path to Claude CLI binary (default: `claude`) |
| `SKIP_PERMISSIONS` | Set to `0` to enable Claude tool permission prompts (default: `1`) |

## Project structure

```
factory2/
├── factory/                # Python package
│   ├── __main__.py         # CLI entry point (argparse)
│   ├── cargo.py            # Cargo check/test/clippy wrappers
│   ├── config.py           # Config dataclass
│   ├── context.py          # Codebase context snapshot generator
│   ├── deps.py             # Dependency analysis + topological sort
│   ├── log.py              # Colored logging
│   ├── monitor.py          # Live status dashboard
│   ├── orchestrator.py     # Main loop (sequential + parallel)
│   ├── pipeline.py         # 5-phase per-story pipeline + git commit
│   ├── runner.py           # Claude CLI wrapper (streams output, parses usage)
│   ├── state.py            # JSON state with file locking + cost tracking
│   └── triage.py           # Spec diff relevance check for cascade invalidation
├── prompts/                # Prompt templates (plain markdown)
│   ├── understand.md
│   ├── plan.md
│   ├── implement.md
│   ├── write_tests.md
│   ├── verify.md
│   ├── summarize.md
│   └── analyze_deps.md
├── pyproject.toml          # Python project config (no external deps)
├── Containerfile           # Container image definition
├── entrypoint.sh           # Container entry point
└── run_container.sh        # Container launcher
```

## Customizing prompts

Edit the files in `prompts/` to adjust how Claude approaches each phase. The prompts are plain markdown — the orchestrator appends task-specific context (spec content, file paths, previous phase outputs) at the end before sending to Claude.

## Scalability

The factory is designed to handle large spec sets (100+ stories) efficiently.

### What scales well

- **Dependency analysis** is deterministic parsing, not an LLM call. It runs in milliseconds regardless of how many specs you have — no context window limits, no token cost.
- **Incremental runs** skip stories whose spec hasn't changed. Editing one spec only re-runs that story and its downstream dependents.
- **Parallel pipelines** (`-j N`) process independent stories concurrently, bounded by the dependency graph.
- **Per-story prompts** include only the current spec and its prior phase output — adding more stories to the project doesn't increase prompt size for unrelated stories.

### What to watch

- **Codebase context snapshot.** Each phase prompt includes an auto-generated snapshot of the project's module tree, public API signatures, and dependencies (from `factory/context.py`). This grows with the total codebase size across all stories. At 100+ stories with many crates, this can become a significant portion of the context window. Consider splitting very large projects into separate workspaces.
- **Dependency graph depth.** If you change a foundational story (e.g., the one that defines core types), every downstream story is invalidated. The factory uses a triage step (Haiku) to check whether the spec diff is actually relevant to each dependent before reprocessing — irrelevant changes are skipped automatically. Still, keep the graph shallow where possible.
- **Parallel conflicts.** With `-j N > 1`, two stories modifying the same file can conflict. Sequential mode is safer for tightly coupled stories.

### Tips for large projects

- **Keep specs focused.** One concern per spec. Smaller specs = smaller transitive dependency closures = faster incremental rebuilds.
- **Declare dependencies accurately.** The factory trusts your `## Depends on` sections. Missing a dependency may cause build failures; adding unnecessary ones slows incremental runs by over-invalidating.
- **Use `--rerun` sparingly.** Re-running a story cascades to all dependents. Target specific stories rather than forcing a full rebuild.
- **Scope tests per story.** Each spec's verification section should run only its own tests (e.g., `make integration-test SPEC=NNN`), not the full suite. This prevents false failures from later stories when the factory reprocesses an earlier one.

## Limitations

- Designed for Rust projects. Supporting other languages requires adjusting the prompts and the `cargo check` post-conditions in `factory/pipeline.py`.
- Parallel mode (`-j N > 1`) can produce merge conflicts if two stories modify the same file. Sequential mode is safer for tightly coupled stories.
