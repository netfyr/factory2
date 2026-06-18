import fcntl
import hashlib
import json
from datetime import datetime
from pathlib import Path


class State:
    def __init__(self, state_dir: Path):
        self.path = state_dir / "state.json"
        self.lock_path = state_dir / "state.json.lock"
        if not self.path.exists():
            self._write_raw({"stories": {}})

    def _read(self) -> dict:
        with open(self.lock_path, "a") as lock:
            fcntl.flock(lock, fcntl.LOCK_SH)
            try:
                return json.loads(self.path.read_text())
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)

    def _write_raw(self, data: dict):
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2))
        tmp.rename(self.path)

    def _update(self, fn):
        with open(self.lock_path, "a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                data = json.loads(self.path.read_text())
                fn(data)
                tmp = self.path.with_suffix(".tmp")
                tmp.write_text(json.dumps(data, indent=2))
                tmp.rename(self.path)
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)

    def _ensure_story(self, data: dict, story_id: str) -> dict:
        return data.setdefault("stories", {}).setdefault(story_id, {})

    # ── Top-level metadata ────────────────────────────────────────

    def set_metadata(self, key: str, value):
        def update(data):
            data[key] = value
        self._update(update)

    # ── Story status ─────────────────────────────────────────────

    def get_story_status(self, story_id: str) -> str:
        data = self._read()
        return data.get("stories", {}).get(story_id, {}).get("status", "pending")

    def set_story_status(self, story_id: str, status: str):
        def update(data):
            self._ensure_story(data, story_id)["status"] = status
        self._update(update)

    # ── Phase status ─────────────────────────────────────────────

    def get_phase_status(self, story_id: str, phase: str) -> str:
        data = self._read()
        return (
            data.get("stories", {})
            .get(story_id, {})
            .get("phases", {})
            .get(phase, {})
            .get("status", "pending")
        )

    def set_phase_status(self, story_id: str, phase: str, status: str):
        ts = datetime.now().isoformat()

        def update(data):
            story = self._ensure_story(data, story_id)
            phases = story.setdefault("phases", {})
            prev = phases.get(phase, {})
            entry = {"status": status, "timestamp": ts}
            if status == "running":
                entry["started_at"] = ts
            elif prev.get("started_at"):
                entry["started_at"] = prev["started_at"]
            phases[phase] = entry
        self._update(update)

    def clear_phases(self, story_id: str):
        """Reset all phase statuses so the story is fully reprocessed."""
        def update(data):
            story = self._ensure_story(data, story_id)
            story.pop("phases", None)
        self._update(update)

    # ── Spec hash ────────────────────────────────────────────────

    def get_spec_hash(self, story_id: str) -> str:
        data = self._read()
        return data.get("stories", {}).get(story_id, {}).get("spec_hash", "")

    def set_spec_hash(self, story_id: str, hash_val: str):
        def update(data):
            self._ensure_story(data, story_id)["spec_hash"] = hash_val
        self._update(update)

    # ── Quarantine / skip ────────────────────────────────────────

    def quarantine(self, story_id: str, reason: str):
        ts = datetime.now().isoformat()

        def update(data):
            story = self._ensure_story(data, story_id)
            story["status"] = "quarantined"
            story["quarantine_reason"] = reason
            story["quarantined_at"] = ts
        self._update(update)

    def skip(self, story_id: str, reason: str):
        def update(data):
            story = self._ensure_story(data, story_id)
            story["status"] = "skipped"
            story["skip_reason"] = reason
        self._update(update)

    # ── Run history ──────────────────────────────────────────────

    def begin_run(self, story_id: str, trigger: str, spec_hash_val: str,
                  pipeline_mode: str = "full"):
        """Open a new run entry. Closes any orphaned previous run first."""
        ts = datetime.now().isoformat()

        def update(data):
            story = self._ensure_story(data, story_id)
            runs = story.setdefault("runs", [])
            if runs and "finished_at" not in runs[-1]:
                runs[-1]["outcome"] = "interrupted"
                runs[-1]["finished_at"] = None
            runs.append({
                "run": len(runs) + 1,
                "started_at": ts,
                "trigger": trigger,
                "spec_hash": spec_hash_val,
                "pipeline_mode": pipeline_mode,
            })
        self._update(update)

    def end_run(self, story_id: str, outcome: str, quarantine_reason: str | None = None):
        """Close the current run with outcome, duration, and cost snapshot."""
        ts = datetime.now().isoformat()

        def update(data):
            story = self._ensure_story(data, story_id)
            runs = story.get("runs", [])
            if not runs:
                return
            run = runs[-1]
            run["finished_at"] = ts
            run["outcome"] = outcome
            if quarantine_reason:
                run["quarantine_reason"] = quarantine_reason
            try:
                t0 = datetime.fromisoformat(run["started_at"])
                t1 = datetime.fromisoformat(ts)
                run["duration_s"] = round((t1 - t0).total_seconds())
            except (ValueError, TypeError, KeyError):
                pass
            run["costs"] = dict(story.get("costs", {}))
            story["costs"] = {}
        self._update(update)

    def close_interrupted_runs(self):
        """Close all open runs across all stories as interrupted."""
        ts = datetime.now().isoformat()

        def update(data):
            for story in data.get("stories", {}).values():
                runs = story.get("runs", [])
                if runs and "finished_at" not in runs[-1]:
                    runs[-1]["outcome"] = "interrupted"
                    runs[-1]["finished_at"] = ts
        self._update(update)

    def get_runs(self, story_id: str) -> list[dict]:
        data = self._read()
        return data.get("stories", {}).get(story_id, {}).get("runs", [])

    # ── Cost tracking ────────────────────────────────────────────

    def add_cost(
        self, story_id: str, phase: str,
        input_tokens: int, output_tokens: int,
        cache_creation_tokens: int = 0, cache_read_tokens: int = 0,
        num_turns: int = 0, model: str = "",
        cost_usd: float = 0.0,
    ):
        def update(data):
            story = self._ensure_story(data, story_id)
            costs = story.setdefault("costs", {})
            pc = costs.setdefault(phase, {
                "input_tokens": 0, "output_tokens": 0,
                "cache_creation_tokens": 0, "cache_read_tokens": 0,
                "num_turns": 0, "cost_usd": 0.0,
            })
            pc["input_tokens"] += input_tokens
            pc["output_tokens"] += output_tokens
            pc["cache_creation_tokens"] = pc.get("cache_creation_tokens", 0) + cache_creation_tokens
            pc["cache_read_tokens"] = pc.get("cache_read_tokens", 0) + cache_read_tokens
            pc["num_turns"] = pc.get("num_turns", 0) + num_turns
            pc["cost_usd"] = pc.get("cost_usd", 0.0) + cost_usd
            if model:
                pc["model"] = model
        self._update(update)

    def get_total_costs(self) -> dict:
        """Return token counts summed across all stories."""
        data = self._read()
        totals = {"input_tokens": 0, "output_tokens": 0, "cache_creation_tokens": 0, "cache_read_tokens": 0}
        for story in data.get("stories", {}).values():
            for phase_cost in story.get("costs", {}).values():
                for key in totals:
                    totals[key] += phase_cost.get(key, 0)
        return totals


def spec_hash(spec_file: Path) -> str:
    return hashlib.sha256(spec_file.read_bytes()).hexdigest()


def specs_combined_hash(specs_dir: Path) -> str:
    h = hashlib.sha256()
    for f in sorted(specs_dir.glob("*.md")):
        h.update(f.read_bytes())
    return h.hexdigest()
