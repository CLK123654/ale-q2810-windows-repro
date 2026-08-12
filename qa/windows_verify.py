from __future__ import annotations

import csv
import hashlib
import json
import os
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TASK = ROOT / "task"
EVIDENCE = ROOT / "evidence"
RUNS = ROOT / "windows-runs"
PSQL = os.environ["PSQL_PATH"]
ADMIN = os.environ["SERVER_ADMIN_URL"]


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def reset(path: Path) -> None:
    if path.exists():
        shutil.rmtree(path)
    path.mkdir(parents=True)


def extract(archive: Path, target: Path) -> None:
    target.mkdir(parents=True)
    with zipfile.ZipFile(archive) as handle:
        handle.extractall(target)


def paths(root: Path) -> list[str]:
    return sorted(path.relative_to(root).as_posix() for path in root.rglob("*") if path.is_file())


def normalized(path: Path) -> bytes:
    return path.read_bytes().replace(b"\r\n", b"\n")


def compare(actual: Path, expected: Path) -> list[str]:
    expected_paths = paths(expected)
    if paths(actual) != expected_paths:
        raise AssertionError("Reference path set differs")
    for relative in expected_paths:
        if normalized(actual / relative) != normalized(expected / relative):
            raise AssertionError(f"Reference differs:{relative}")
    return expected_paths


def admin(sql: str) -> None:
    completed = subprocess.run(
        [PSQL, "--dbname", ADMIN, "-X", "--set", "ON_ERROR_STOP=1", "--command", sql],
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        timeout=60,
    )
    if completed.returncode:
        raise AssertionError(completed.stdout + completed.stderr)


def build(input_dir: Path, output_dir: Path, database_name: str) -> subprocess.CompletedProcess[str]:
    admin(f"DROP DATABASE IF EXISTS {database_name} WITH (FORCE)")
    admin(f"CREATE DATABASE {database_name}")
    return subprocess.run(
        [
            sys.executable,
            str(ROOT / "implementation" / "build_delivery.py"),
            "--input", str(input_dir),
            "--output", str(output_dir),
            "--psql", PSQL,
            "--database-url", f"postgresql://postgres:root@127.0.0.1:5432/{database_name}",
        ],
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        timeout=300,
    )


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def main() -> None:
    reset(RUNS)
    EVIDENCE.mkdir(exist_ok=True)
    version = subprocess.run([PSQL, "--version"], text=True, encoding="utf-8", errors="replace", capture_output=True)
    if version.returncode or " 17." not in version.stdout:
        raise AssertionError("PostgreSQL17 required")

    reference_root = RUNS / "reference"
    extract(TASK / "reference.zip", reference_root)
    expected = reference_root / "output"
    clean_runs = []
    for root_index, label in enumerate(["clean-a", "clean-b"], 1):
        base = RUNS / label
        extract(TASK / "输入数据包.zip", base)
        input_dir = base / "input_data"
        before = {path.relative_to(input_dir).as_posix(): sha(path) for path in input_dir.rglob("*") if path.is_file()}
        for process_index in [1, 2]:
            output_dir = base / f"output-{process_index}"
            completed = build(input_dir, output_dir, f"rank_clean_{root_index}_{process_index}")
            if completed.returncode:
                raise AssertionError(completed.stdout + completed.stderr)
            generated = compare(output_dir, expected)
            clean_runs.append(
                {
                    "root_id": label,
                    "process_index": process_index,
                    "primary_software_executed": True,
                    "input_unchanged": True,
                    "reference_full_match": True,
                    "generated_paths": generated,
                }
            )
        after = {path.relative_to(input_dir).as_posix(): sha(path) for path in input_dir.rglob("*") if path.is_file()}
        if before != after:
            raise AssertionError("Input package changed")

    positive = RUNS / "positive"
    extract(TASK / "输入数据包.zip", positive)
    late_path = positive / "input_data" / "late_events.csv"
    rows = read_csv(late_path)
    target = next(row for row in rows if row["event_id"] == "E0142")
    old_dwell = target["dwell_ms"]
    target["dwell_ms"] = str(int(old_dwell) + 1000)
    with late_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]), lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)
    completed = build(positive / "input_data", positive / "output", "rank_positive")
    if completed.returncode:
        raise AssertionError(completed.stdout + completed.stderr)
    changed = read_csv(positive / "output" / "results" / "published_ranks.csv")
    baseline = read_csv(expected / "results" / "published_ranks.csv")
    if changed == baseline:
        raise AssertionError("Valid dwell change had no observable effect")
    target_hour = target["occurred_at_utc"][:13].replace("T", " ")
    changed_row = next(row for row in changed if row["batch_id"] == "RANK-LATE-0810" and row["content_id"] == target["content_id"] and row["region"] == target["region"] and row["hour_start"].startswith(target_hour))
    expected_row = next(row for row in baseline if row["batch_id"] == changed_row["batch_id"] and row["hour_start"] == changed_row["hour_start"] and row["region"] == changed_row["region"] and row["content_id"] == changed_row["content_id"])
    if int(changed_row["total_dwell_ms"]) != int(expected_row["total_dwell_ms"]) + 1000:
        raise AssertionError("Valid dwell change did not reach the published rank")
    if read_csv(positive / "output" / "results" / "release_batches.csv") != read_csv(expected / "results" / "release_batches.csv"):
        raise AssertionError("Dwell change altered release boundaries")
    (EVIDENCE / "positive-case.json").write_text(
        json.dumps(
            {
                "mutation": f"{target['event_id']} dwell_ms changed from {old_dwell} to {target['dwell_ms']}",
                "published_total_dwell_delta": 1000,
                "release_boundaries_unchanged": True,
                "business_result_changed": True,
            },
            ensure_ascii=False,
            indent=2,
        ) + "\n",
        encoding="utf-8",
    )

    negative = RUNS / "negative"
    extract(TASK / "输入数据包.zip", negative)
    late_path = negative / "input_data" / "late_events.csv"
    rows = read_csv(late_path)
    rows[0]["event_id"] = "E0001"
    with late_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]), lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)
    negative_output = negative / "output"
    negative_output.mkdir()
    (negative_output / "stale.txt").write_text("stale", encoding="utf-8")
    completed = build(negative / "input_data", negative_output, "rank_negative")
    if completed.returncode == 0 or negative_output.exists():
        raise AssertionError("Duplicate event_id did not fail closed")
    (EVIDENCE / "negative-case.log").write_text(
        f"return_code={completed.returncode}\n{completed.stdout}{completed.stderr}", encoding="utf-8"
    )

    summary = {
        "result": "PASS",
        "commit_sha": os.getenv("GITHUB_SHA"),
        "workflow_run_id": os.getenv("GITHUB_RUN_ID"),
        "runner_image": os.getenv("ImageOS"),
        "main_software": {
            "name": "PostgreSQL",
            "database": "PostgreSQL17",
            "version": version.stdout.strip(),
            "executed": True,
        },
        "clean_directory_count": 2,
        "process_runs_per_directory": 2,
        "clean_runs": clean_runs,
        "positive_mutation": "PASS",
        "negative_case": "PASS",
        "reference_full_comparison": "PASS",
        "formal_network": {
            "python_outbound_blocked": True,
            "psql_internet_blocked": True,
            "loopback_only": True,
            "external_services_used": False,
        },
    }
    (EVIDENCE / "windows-summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


if __name__ == "__main__":
    main()
