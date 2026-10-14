"""Runs v1 (IncidentPilot) as the baseline, without modifying `legacy/`.

Which v1: the published IncidentPilot commit only contains the retrieval app (no LLM). The
LLM orchestrator the v1 story is about (gpt-4o-mini, and the `FastMCPClient` import that
fails silently) exists only as uncommitted files in the local working copy. The baseline
therefore runs a snapshot of the working copy, copied into storage/v1-baseline/<variant>/,
and records a content hash of it (plus the commit it sits on) with the results.

It runs in its own isolated environment built from v1's requirements.txt, so its ChromaDB
index and dependencies never touch this project.

Variants:
- shipped: v1's own corpus.
- our-runbooks: the same code, with SentinelOps' docs/runbooks added to its corpus at ingest
  time. This separates "v1 lacked the right docs" from "v1 had no live evidence".

v1's metrics client import fails (the original bug), so it gets no metrics in either variant.
That is v1's real behavior, not something the baseline adds.
"""

import hashlib
import json
import os
import shutil
import subprocess
from pathlib import Path

from sentinelops.config import ROOT

LEGACY = ROOT / "legacy" / "v1-incidentpilot" / "IncidentPilot"
VARIANTS = ("shipped", "our-runbooks")

ENTRY = """
import json, sys
sys.path.insert(0, "src")
from ingest import load_corpus
from rag import get_collection, add_documents
from orchestrator import orchestrate_any_incident

query, extra = sys.argv[1], sys.argv[2:]
coll = get_collection()
if coll.count() == 0:
    docs, metas, ids = load_corpus("data")
    for root in extra:
        d, m, i = load_corpus(root)
        docs, metas, ids = docs + d, metas + m, ids + i
    add_documents(coll, docs, metas, ids)
plan = orchestrate_any_incident(query, metric_names=["http_requests_total"])
from importlib.metadata import version
versions = {p: version(p) for p in ("openai", "chromadb")}
print("V1_RESULT=" + json.dumps({"plan": plan, "versions": versions}))
"""


SKIP = shutil.ignore_patterns(".git", "storage", "__pycache__", "*.pyc", ".venv", ".env")


def legacy_sha() -> str:
    """The IncidentPilot commit the working copy sits on."""
    result = subprocess.run(
        ["git", "-C", str(LEGACY), "rev-parse", "HEAD"], capture_output=True, text=True, check=True
    )
    return result.stdout.strip()


RUNTIME_DIRS = {"storage", "__pycache__"}  # written by v1 while it runs, not part of its code


def snapshot_hash(directory: Path) -> str:
    """SHA-256 over every source file's path and content: identifies exactly which v1 ran.

    Files v1 writes while running (its vector store, bytecode) are left out, so the hash stays
    the same across runs of the same snapshot.
    """
    digest = hashlib.sha256()
    files = (
        p
        for p in directory.rglob("*")
        if p.is_file()
        and p.name != "v1_entry.py"
        and not RUNTIME_DIRS.intersection(p.relative_to(directory).parts)
    )
    for path in sorted(files):
        digest.update(str(path.relative_to(directory)).encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()


def export(variant: str, base_dir: Path) -> Path:
    """Snapshot the v1 working copy for `variant` (once). Returns its directory."""
    if variant not in VARIANTS:
        raise ValueError(f"unknown v1 variant {variant!r}")
    target = base_dir / variant
    if not (target / "src" / "orchestrator.py").exists():
        shutil.copytree(LEGACY, target, ignore=SKIP, dirs_exist_ok=True)
        (target / "v1_entry.py").write_text(ENTRY)
    return target


def command(variant: str, workdir: Path, query: str) -> list[str]:
    """The isolated-environment command that runs v1 on one alert description."""
    extra = [str(ROOT / "docs" / "runbooks")] if variant == "our-runbooks" else []
    return [
        "uv",
        "run",
        "--no-project",
        "--python",
        "3.12",
        "--with-requirements",
        str(workdir / "requirements.txt"),
        "python",
        "v1_entry.py",
        query,
        *extra,
    ]


def run(
    variant: str, base_dir: Path, query: str, openai_api_key: str, timeout: float = 600
) -> dict:
    """Run v1 once. Returns its JSON plan; raises with v1's stderr if it fails."""
    workdir = export(variant, base_dir)
    result = subprocess.run(
        command(variant, workdir, query),
        cwd=workdir,
        capture_output=True,
        text=True,
        timeout=timeout,
        env={
            "PATH": os.environ["PATH"],
            "HOME": os.environ["HOME"],
            "OPENAI_API_KEY": openai_api_key,
        },
    )
    for line in result.stdout.splitlines():
        if line.startswith("V1_RESULT="):
            payload = json.loads(line.removeprefix("V1_RESULT="))
            return payload["plan"], payload["versions"]
    raise RuntimeError(f"v1 {variant} produced no result: {result.stderr.strip()[-1000:]}")
