import json
import select
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from . import log


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_tokens: int = 0
    cache_read_tokens: int = 0
    num_turns: int = 0


def run_agent(
    prompt: str,
    log_file: Path,
    model: str,
    max_turns: int,
    workdir: Path,
    cmd: str = "claude",
    skip_permissions: bool = True,
    verbose: bool = False,
    activity_file: Path | None = None,
    stall_timeout: int = 0,
) -> tuple[bool, Usage, bool]:
    """Run a coding agent CLI in print mode. Returns (success, usage, stalled)."""

    cmd_list = _build_claude_cmd(cmd, model, max_turns, skip_permissions)

    log.info(f"  Agent: model={model} max_turns={max_turns}")
    log.info(f"  Log:    {log_file}")

    log_file.parent.mkdir(parents=True, exist_ok=True)

    usage = Usage()
    result_seen = False
    stalled = False

    with open(log_file, "w") as lf:
        proc = subprocess.Popen(
            cmd_list,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            cwd=workdir,
            text=True,
        )
        proc.stdin.write(prompt)
        proc.stdin.close()

        timeout_val = stall_timeout if stall_timeout > 0 else None

        while True:
            ready, _, _ = select.select([proc.stdout], [], [], timeout_val)
            if not ready:
                log.error(f"  Agent stalled — no output for {stall_timeout}s, killing")
                stalled = True
                proc.kill()
                proc.wait()
                break
            line = proc.stdout.readline()
            if not line:
                break

            lf.write(line)
            lf.flush()

            if verbose:
                sys.stderr.write(line)

            # Parse stream-json for usage stats and turn count
            stripped = line.strip()
            if stripped:
                old_in, old_out = usage.input_tokens, usage.output_tokens
                _accumulate_usage(stripped, usage)
                _accumulate_turns(stripped, usage)
                if activity_file:
                    _update_activity(stripped, activity_file)
                    if usage.input_tokens != old_in or usage.output_tokens != old_out:
                        _write_live_usage(activity_file.parent / "live_usage", usage)

                try:
                    obj = json.loads(stripped)
                    if isinstance(obj, dict) and obj.get("type") == "result":
                        result_seen = True
                        break
                except json.JSONDecodeError:
                    pass

    if not stalled:
        _wait_or_terminate(proc, result_seen)

    if activity_file:
        activity_file.unlink(missing_ok=True)
        (activity_file.parent / "live_usage").unlink(missing_ok=True)

    if not stalled and proc.returncode != 0:
        log.error(f"  Agent exited with code {proc.returncode}")

    success = not stalled and proc.returncode == 0
    return success, usage, stalled


_AGENT_EXIT_GRACE = 30


def _wait_or_terminate(proc, result_seen):
    """Wait for the agent to exit, terminating it if child processes hang."""
    if not result_seen:
        proc.wait()
        return
    try:
        proc.wait(timeout=_AGENT_EXIT_GRACE)
    except subprocess.TimeoutExpired:
        log.warn(
            f"  Agent still running {_AGENT_EXIT_GRACE}s after session ended,"
            " terminating (likely hung child processes)"
        )
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()


def _build_claude_cmd(cmd, model, max_turns, skip_permissions):
    args = [
        cmd,
        "-p",
        "--output-format", "stream-json",
        "--verbose",
        "--model", model,
        "--max-turns", str(max_turns),
        "--allowedTools", "Read,Write,Edit,Glob,Grep,Bash",
    ]
    if skip_permissions:
        args.append("--dangerously-skip-permissions")
    args.append("-")  # read prompt from stdin
    return args


def _update_activity(line: str, activity_file: Path):
    """Parse a stream-json line and write current activity to file."""
    try:
        obj = json.loads(line)
    except json.JSONDecodeError:
        return

    activity = _extract_activity(obj)
    if activity:
        tmp = activity_file.with_suffix(".tmp")
        tmp.write_text(activity)
        tmp.rename(activity_file)


def _extract_activity(obj) -> str | None:
    """Extract a human-readable activity string from a stream-json object."""
    if not isinstance(obj, dict):
        return None

    # Tool results
    if obj.get("type") == "tool_result":
        return None

    # Final result
    if obj.get("type") == "result":
        return "Done" if obj.get("subtype") == "success" else "Failed"

    # Assistant messages with content
    msg = obj.get("message") if obj.get("type") == "assistant" else None
    if not msg:
        return None

    content = msg.get("content", [])
    if not isinstance(content, list):
        return None

    for block in reversed(content):
        if not isinstance(block, dict):
            continue
        btype = block.get("type")

        if btype == "tool_use":
            name = block.get("name", "")
            inp = block.get("input", {})
            return _format_tool_activity(name, inp)

        if btype == "thinking":
            thought = block.get("thinking", "").strip()
            if thought:
                first_line = thought.split("\n")[0]
                if len(first_line) > 200:
                    return first_line[:197] + "..."
                return first_line
            return "Thinking..."

        if btype == "text":
            text = block.get("text", "").strip()
            if text:
                first_line = text.split("\n")[0]
                if len(first_line) > 200:
                    return first_line[:197] + "..."
                return first_line

    return None


def _format_tool_activity(name: str, inp: dict) -> str:
    """Format a tool use into a short activity string."""
    if name in ("Read", "Write", "Edit"):
        path = inp.get("file_path", inp.get("path", ""))
        if path:
            parts = path.rsplit("/", 2)
            short = "/".join(parts[-2:]) if len(parts) > 1 else path
            return f"{name} {short}"
        return name

    if name == "Bash":
        cmd = inp.get("command", "")
        if cmd:
            first_line = cmd.split("\n")[0]
            if len(first_line) > 140:
                first_line = first_line[:137] + "..."
            return f"$ {first_line}"
        return "Bash"

    if name == "Grep":
        pattern = inp.get("pattern", "")
        path = inp.get("path", "")
        suffix = f" in {path}" if path else ""
        return f"Grep {pattern[:60]}{suffix}" if pattern else "Grep"

    if name == "Glob":
        pattern = inp.get("pattern", "")
        return f"Glob {pattern[:60]}" if pattern else "Glob"

    return name


def _write_live_usage(path: Path, usage: Usage):
    """Write current usage stats to a file for the monitor to read."""
    data = json.dumps({
        "input_tokens": usage.input_tokens,
        "output_tokens": usage.output_tokens,
        "cache_creation_tokens": usage.cache_creation_tokens,
        "cache_read_tokens": usage.cache_read_tokens,
        "num_turns": usage.num_turns,
    })
    tmp = path.with_suffix(".tmp")
    tmp.write_text(data)
    tmp.rename(path)


def _accumulate_turns(line: str, usage: Usage):
    """Extract num_turns from a stream-json result line."""
    try:
        obj = json.loads(line)
    except json.JSONDecodeError:
        return
    if isinstance(obj, dict) and obj.get("type") == "result":
        usage.num_turns = max(usage.num_turns, obj.get("num_turns", 0))


def _accumulate_usage(line: str, usage: Usage):
    """Extract token counts from a stream-json line."""
    try:
        obj = json.loads(line)
    except json.JSONDecodeError:
        return

    u = _find_usage(obj)
    if not u:
        return

    # Stream-json reports cumulative usage — keep the max seen
    usage.input_tokens = max(usage.input_tokens, u.get("input_tokens", 0))
    usage.output_tokens = max(usage.output_tokens, u.get("output_tokens", 0))
    usage.cache_creation_tokens = max(
        usage.cache_creation_tokens, u.get("cache_creation_input_tokens", 0)
    )
    usage.cache_read_tokens = max(
        usage.cache_read_tokens, u.get("cache_read_input_tokens", 0)
    )


def _find_usage(obj) -> dict | None:
    """Recursively search a JSON object for a usage dict with input_tokens."""
    if isinstance(obj, dict):
        if "input_tokens" in obj and "output_tokens" in obj:
            return obj
        for v in obj.values():
            result = _find_usage(v)
            if result:
                return result
    if isinstance(obj, list):
        for item in obj:
            result = _find_usage(item)
            if result:
                return result
    return None
