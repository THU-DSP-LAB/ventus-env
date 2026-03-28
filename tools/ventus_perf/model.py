from __future__ import annotations

import json
from pathlib import Path


def _load_json(path: Path) -> dict:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def _default_state_for_manifest(path: Path) -> str:
    return "completed" if path.name == "pass.json" else "incomplete"


def _load_pass_dir(pass_dir: Path) -> dict:
    pass_json = pass_dir / "pass.json"
    pass_begin = pass_dir / "pass.begin.json"
    manifest_path = pass_json if pass_json.exists() else pass_begin
    if not manifest_path.exists():
        raise FileNotFoundError(f"missing pass manifest in {pass_dir}")
    manifest = _load_json(manifest_path)
    manifest["path"] = pass_dir
    manifest["manifest_path"] = manifest_path
    manifest.setdefault("pass_id", pass_dir.name)
    manifest.setdefault("state", _default_state_for_manifest(manifest_path))
    manifest.setdefault("event_files", [])
    if not manifest["event_files"]:
        manifest["event_files"] = sorted(
            entry.name for entry in pass_dir.glob("events.*.jsonl") if entry.is_file()
        )
    return manifest


def _load_experiment_dir(experiment_dir: Path) -> tuple[dict, list[dict]]:
    experiment_json = experiment_dir / "experiment.json"
    experiment_begin = experiment_dir / "experiment.begin.json"
    manifest_path = experiment_json if experiment_json.exists() else experiment_begin
    if not manifest_path.exists():
        raise FileNotFoundError(f"missing experiment manifest in {experiment_dir}")
    experiment = _load_json(manifest_path)
    experiment["path"] = experiment_dir
    experiment["best_effort"] = manifest_path.name != "experiment.json"
    if experiment["best_effort"]:
        experiment["state"] = "incomplete"
    ordered_passes = experiment.get("actual_passes", [])
    if not ordered_passes:
        ordered_passes = sorted(
            str(path.relative_to(experiment_dir))
            for path in (experiment_dir / "passes").glob("*")
            if path.is_dir()
        )
    passes = [_load_pass_dir(experiment_dir / relative_path) for relative_path in ordered_passes]
    return experiment, passes


def load_input_manifests(input_dir: Path) -> tuple[dict, list[dict]]:
    input_path = Path(input_dir)
    if (input_path / "pass.json").exists() or (input_path / "pass.begin.json").exists():
        single_pass = _load_pass_dir(input_path)
        experiment = {
            "experiment_id": single_pass["pass_id"],
            "path": input_path,
            "state": single_pass.get("state", "incomplete"),
            "best_effort": True,
            "actual_passes": ["."],
            "input_kind": "pass",
        }
        return experiment, [single_pass]
    experiment, passes = _load_experiment_dir(input_path)
    experiment["input_kind"] = "experiment"
    experiment.setdefault("actual_passes", [str(p["path"].relative_to(input_path)) for p in passes])
    return experiment, passes


def load_events_for_pass(pass_manifest: dict) -> list[dict]:
    events: list[dict] = []
    pass_dir = Path(pass_manifest["path"])
    for relative_name in pass_manifest.get("event_files", []):
        path = pass_dir / relative_name
        if not path.exists():
            continue
        with path.open("r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                event = json.loads(line)
                event["pass_id"] = pass_manifest["pass_id"]
                event["pass_type"] = pass_manifest.get("pass_type")
                event["pass_state"] = pass_manifest.get("state")
                events.append(event)
    return events
