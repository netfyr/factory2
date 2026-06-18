"""Profile system for language/toolchain abstraction.

A profile is a TOML file that describes how the factory should work with
a specific project type (Rust, Python, Go, etc.).  It defines tools,
phase configuration, environment probes, and project initialization —
everything that was previously hardcoded for Rust.
"""

import shutil
import subprocess
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from . import cargo, log


@dataclass
class ToolDef:
    name: str
    description: str = ""
    builtin: str | None = None
    command: list[str] | None = None
    timeout: int = 600


@dataclass
class PhaseDef:
    name: str
    model_tier: str = "default"
    max_turns: int | None = None
    post_checks: list[str] = field(default_factory=list)
    pre_run: list[str] = field(default_factory=list)
    max_retries: int | None = None
    max_iterations: int | None = None


@dataclass
class ToolResult:
    success: bool
    summary: str
    details: str
    output: str


@dataclass
class Profile:
    name: str
    description: str
    detect_files: list[str]
    tools: dict[str, ToolDef]
    phases: dict[str, PhaseDef]
    environment: list[dict]
    init_check: str | None
    init_command: list[str] | None
    prerequisites: list[str]
    context_type: str | None
    profile_dir: Path
    factory_prompts_dir: Path
    project_override_dir: Path | None

    @classmethod
    def load(cls, name: str, factory_dir: Path, project_dir: Path | None = None) -> "Profile":
        profiles_dir = factory_dir / "profiles"
        profile_dir = profiles_dir / name

        toml_path = profile_dir / "profile.toml"
        if not toml_path.exists():
            raise SystemExit(f"Profile not found: {toml_path}")

        with open(toml_path, "rb") as f:
            data = tomllib.load(f)

        tools = {}
        for tool_name, tool_data in data.get("tools", {}).items():
            tools[tool_name] = ToolDef(
                name=tool_name,
                description=tool_data.get("description", ""),
                builtin=tool_data.get("builtin"),
                command=tool_data.get("command"),
                timeout=tool_data.get("timeout", 600),
            )

        phases = {}
        for phase_name, phase_data in data.get("phases", {}).items():
            phases[phase_name] = PhaseDef(
                name=phase_name,
                model_tier=phase_data.get("model_tier", "default"),
                max_turns=phase_data.get("max_turns"),
                post_checks=phase_data.get("post_checks", []),
                pre_run=phase_data.get("pre_run", []),
                max_retries=phase_data.get("max_retries"),
                max_iterations=phase_data.get("max_iterations"),
            )

        init_data = data.get("init", {})

        project_override = None
        if project_dir:
            candidate = project_dir / ".factory" / "profile"
            if candidate.is_dir():
                project_override = candidate

        return cls(
            name=data.get("name", name),
            description=data.get("description", ""),
            detect_files=data.get("detect", []),
            tools=tools,
            phases=phases,
            environment=data.get("environment", []),
            init_check=init_data.get("check"),
            init_command=init_data.get("command"),
            prerequisites=data.get("prerequisites", []),
            context_type=data.get("context"),
            profile_dir=profile_dir,
            factory_prompts_dir=factory_dir / "prompts",
            project_override_dir=project_override,
        )

    @classmethod
    def detect(cls, factory_dir: Path, project_dir: Path) -> str:
        """Auto-detect profile by checking which detect files exist in the project."""
        profiles_dir = factory_dir / "profiles"
        if not profiles_dir.is_dir():
            return "rust"

        for profile_path in sorted(profiles_dir.iterdir()):
            toml_path = profile_path / "profile.toml"
            if not toml_path.exists():
                continue
            try:
                with open(toml_path, "rb") as f:
                    data = tomllib.load(f)
                for detect_file in data.get("detect", []):
                    if (project_dir / detect_file).exists():
                        return data.get("name", profile_path.name)
            except (OSError, tomllib.TOMLDecodeError):
                continue

        return "rust"

    def get_phase(self, phase: str) -> PhaseDef:
        return self.phases.get(phase, PhaseDef(name=phase))

    def get_model(self, phase: str, config) -> str:
        tier = self.get_phase(phase).model_tier
        return {
            "strong": config.strong_model,
            "default": config.default_model,
            "fast": config.fast_model,
        }.get(tier, config.default_model)

    def get_max_turns(self, phase: str, config) -> int:
        phase_def = self.get_phase(phase)
        if phase_def.max_turns is not None:
            return phase_def.max_turns
        if phase == "verify":
            return config.verify_turns
        return config.max_turns

    def get_prompt_path(self, phase: str) -> Path:
        """Resolve prompt file: project override > profile-specific > factory default."""
        if self.project_override_dir:
            override = self.project_override_dir / "prompts" / f"{phase}.md"
            if override.exists():
                return override

        profile_prompt = self.profile_dir / "prompts" / f"{phase}.md"
        if profile_prompt.exists():
            return profile_prompt

        return self.factory_prompts_dir / f"{phase}.md"

    def phase_list(self) -> list[str]:
        """Return ordered list of pipeline phases (excluding commit)."""
        canonical = ["understand", "plan", "plan_delta", "implement", "review", "write_tests", "verify"]
        return [p for p in canonical if p in self.phases]

    def run_tool(self, tool_name: str, project_dir: Path) -> ToolResult:
        tool = self.tools.get(tool_name)
        if not tool:
            return ToolResult(
                success=False,
                summary=f"unknown tool: {tool_name}",
                details="",
                output="",
            )
        if tool.builtin:
            return self._run_builtin(tool, project_dir)
        return self._run_command(tool, project_dir)

    def _run_builtin(self, tool: ToolDef, project_dir: Path) -> ToolResult:
        if tool.builtin == "cargo_check":
            result = cargo.check(project_dir)
            return ToolResult(
                success=result.success,
                summary=result.summary(),
                details=result.format_errors(),
                output="",
            )
        elif tool.builtin == "cargo_check_tests":
            result = cargo.check(project_dir, tests=True)
            return ToolResult(
                success=result.success,
                summary=result.summary(),
                details=result.format_errors(),
                output="",
            )
        elif tool.builtin == "cargo_test_verbose":
            success, output = cargo.test_verbose(project_dir)
            return ToolResult(
                success=success,
                summary="tests passed" if success else "tests failed",
                details="",
                output=output,
            )
        elif tool.builtin == "cargo_clippy":
            result = cargo.clippy(project_dir)
            return ToolResult(
                success=result.success,
                summary=result.summary(),
                details=result.format_errors(),
                output="",
            )
        else:
            return ToolResult(
                success=False,
                summary=f"unknown builtin: {tool.builtin}",
                details="",
                output="",
            )

    def _run_command(self, tool: ToolDef, project_dir: Path) -> ToolResult:
        try:
            proc = subprocess.run(
                tool.command,
                cwd=project_dir,
                capture_output=True,
                text=True,
                timeout=tool.timeout,
            )
        except subprocess.TimeoutExpired:
            return ToolResult(
                success=False,
                summary=f"{tool.name} timed out after {tool.timeout}s",
                details="",
                output="",
            )
        except FileNotFoundError:
            return ToolResult(
                success=False,
                summary=f"command not found: {tool.command[0]}",
                details="",
                output="",
            )

        output = proc.stdout + proc.stderr
        if len(output) > 4000:
            output = "...(truncated)\n" + output[-4000:]

        return ToolResult(
            success=proc.returncode == 0,
            summary="OK" if proc.returncode == 0 else f"failed (exit {proc.returncode})",
            details=output if proc.returncode != 0 else "",
            output=output,
        )

    def generate_context(self, config) -> str:
        if self.context_type == "rust":
            from .context import generate_context
            return generate_context(config)
        return ""

    def probe_environment(self, project_dir: Path) -> str:
        lines = []
        for probe in self.environment:
            label = probe.get("label", "?")
            cmd = probe.get("command", "")
            try:
                r = subprocess.run(
                    cmd, shell=True, capture_output=True, text=True, timeout=10,
                )
                value = r.stdout.strip() or r.stderr.strip()
            except (subprocess.TimeoutExpired, OSError):
                value = "unavailable"
            lines.append(f"- {label}: {value}")
        return "\n".join(lines)

    def init_project(self, project_dir: Path):
        if not self.init_check or not self.init_command:
            return
        if (project_dir / self.init_check).exists():
            return
        log.info(f"Initializing {self.name} project")
        subprocess.run(
            self.init_command,
            cwd=project_dir,
            capture_output=True,
        )

    def check_prerequisites(self, claude_cmd: str):
        for cmd in [claude_cmd, "jq"] + self.prerequisites:
            if not shutil.which(cmd):
                raise SystemExit(f"Required command not found: {cmd}")
