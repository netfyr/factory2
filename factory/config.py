from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class Config:
    project_dir: Path
    specs_dir: Path
    state_dir: Path
    factory_dir: Path = field(default_factory=lambda: Path(__file__).resolve().parent.parent)
    profile_name: str = ""     # empty = auto-detect from project files
    max_parallel: int = 1
    max_retries: int = 3
    strong_model: str = "claude-opus-4-6"     # plan
    default_model: str = "claude-sonnet-4-6"  # understand, implement, review, write-tests, verify
    fast_model: str = "claude-haiku-4-5"  # analyze-deps, summarize, commit
    max_turns: int = 100
    verify_turns: int = 120
    max_review_iterations: int = 2
    stall_timeout: int = 600
    verbose: bool = False
    cmd: str = "claude"        # CLI binary name/path
    skip_permissions: bool = True
    rerun: list[str] = field(default_factory=list)
    rerun_all: bool = False
    llm_deps: bool = False
    git_author_name: str = "Factory"
    git_author_email: str = "factory@localhost"

    @property
    def stories_dir(self) -> Path:
        return self.state_dir / "stories"

    @property
    def output_dir(self) -> Path:
        return self.state_dir / "output"

    @property
    def prompts_dir(self) -> Path:
        return self.factory_dir / "prompts"

    @property
    def state_file(self) -> Path:
        return self.state_dir / "state.json"

    @property
    def profiles_dir(self) -> Path:
        return self.factory_dir / "profiles"

    @property
    def deps_file(self) -> Path:
        return self.state_dir / "deps.json"
