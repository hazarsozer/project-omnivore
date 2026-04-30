from __future__ import annotations

import argparse
import asyncio
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from eval.metrics import EvalResult, StubMetricsEvaluator


@dataclass
class HarnessConfig:
    fixtures_dir: Path = field(default_factory=lambda: Path("eval/fixtures"))
    results_dir: Path = field(default_factory=lambda: Path("eval/results"))
    fixture_ids: list[str] | None = None
    handlers: list[str] | None = None


async def load_fixture_catalog(fixtures_dir: Path) -> list[dict]:
    catalog = []
    for meta_path in sorted(fixtures_dir.rglob("meta.json")):
        with meta_path.open() as f:
            meta = json.load(f)
        meta["_meta_path"] = str(meta_path)
        meta["_fixture_dir"] = str(meta_path.parent)
        catalog.append(meta)
    return catalog


async def run_harness(config: HarnessConfig) -> list[EvalResult]:
    config.results_dir.mkdir(parents=True, exist_ok=True)
    catalog = await load_fixture_catalog(config.fixtures_dir)

    if config.fixture_ids:
        catalog = [f for f in catalog if f["fixture_id"] in config.fixture_ids]

    evaluator = StubMetricsEvaluator()
    results: list[EvalResult] = []

    for fixture in catalog:
        fixture_id = fixture["fixture_id"]
        fixture_dir = Path(fixture["_fixture_dir"])
        source_files = [p for p in fixture_dir.iterdir() if p.name not in ("meta.json",)]

        if not source_files:
            print(f"SKIP {fixture_id}: source file missing — run eval/fixtures/download.sh first")
            continue

        result = EvalResult(
            fixture_id=fixture_id,
            handler=fixture.get("expected_handler", "unknown"),
            handler_version="stub",
            run_at=datetime.now(timezone.utc),
            metrics={
                "extraction_accuracy": None,  # Phase 3
                "chunk_faithfulness": None,   # Phase 3
                "retrieval_recall_at_10": None,  # Phase 3
                "ndcg_at_10": None,           # Phase 3
            },
        )
        results.append(result)

    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out_path = config.results_dir / f"{run_id}.json"
    payload = [
        {
            "fixture_id": r.fixture_id,
            "handler": r.handler,
            "handler_version": r.handler_version,
            "run_at": r.run_at.isoformat(),
            "metrics": r.metrics,
            "errors": r.errors,
        }
        for r in results
    ]
    with out_path.open("w") as f:
        json.dump(payload, f, indent=2)
    print(f"Results written to {out_path} ({len(results)} fixtures)")
    return results


def main() -> None:
    parser = argparse.ArgumentParser(description="Omnivore eval harness")
    parser.add_argument("--fixture-ids", nargs="*", help="Run only these fixture IDs")
    parser.add_argument("--handlers", nargs="*", help="Filter by handler name")
    args = parser.parse_args()
    config = HarnessConfig(fixture_ids=args.fixture_ids, handlers=args.handlers)
    asyncio.run(run_harness(config))


if __name__ == "__main__":
    main()
