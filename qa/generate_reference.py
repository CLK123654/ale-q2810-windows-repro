from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

root = Path(__file__).resolve().parents[1]
work = root / "work-reference"
evidence = root / "evidence"
if work.exists():
    shutil.rmtree(work)
work.mkdir()
with zipfile.ZipFile(root / "task" / "输入数据包.zip") as archive:
    archive.extractall(work)
completed = subprocess.run(
    [
        sys.executable,
        str(root / "implementation" / "build_delivery.py"),
        "--input", str(work / "input_data"),
        "--output", str(work / "output"),
        "--psql", os.environ["PSQL_PATH"],
        "--database-url", os.environ["REFERENCE_DATABASE_URL"],
    ],
    text=True,
    encoding="utf-8",
    errors="replace",
    capture_output=True,
    timeout=300,
)
if completed.returncode:
    raise SystemExit(completed.stdout + completed.stderr)
evidence.mkdir(exist_ok=True)
with zipfile.ZipFile(evidence / "reference-candidate.zip", "w", zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
    for source in sorted((work / "output").rglob("*")):
        if source.is_file():
            archive.write(source, source.relative_to(work).as_posix())
(evidence / "reference-generation.json").write_text(
    json.dumps(
        {
            "result": "PASS",
            "commit_sha": os.getenv("GITHUB_SHA"),
            "workflow_run_id": os.getenv("GITHUB_RUN_ID"),
        },
        indent=2,
    ) + "\n",
    encoding="utf-8",
)
