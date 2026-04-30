from __future__ import annotations

import argparse
import asyncio
import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from eval.metrics import EvalResult
from eval.runner import run_fixture
from omnivore.pipeline.registry import HandlerRegistry


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

    registry = HandlerRegistry()
    registry.discover()

    results: list[EvalResult] = []

    for fixture in catalog:
        fixture_id = fixture["fixture_id"]
        fixture_dir = Path(fixture["_fixture_dir"])
        source_files = [p for p in fixture_dir.iterdir() if p.name != "meta.json"]

        if not source_files:
            print(f"SKIP {fixture_id}: no source file — add it to {fixture_dir}/")
            continue

        source_file = source_files[0]
        print(f"RUN  {fixture_id}: {source_file.name}")

        try:
            run_output = await run_fixture(source_file, fixture, registry)
        except Exception as exc:
            print(f"ERR  {fixture_id}: {exc}")
            results.append(
                EvalResult(
                    fixture_id=fixture_id,
                    handler=fixture.get("expected_handler", "unknown"),
                    handler_version="error",
                    run_at=datetime.now(UTC),
                    metrics={},
                    errors=[str(exc)],
                )
            )
            continue

        accuracy = run_output.get("structural_accuracy", 0.0)
        status = "PASS" if accuracy == 1.0 else f"FAIL({accuracy:.0%})"
        print(f"     {status}  handler={run_output.get('handler_used')}  "
              f"chunks={run_output.get('chunk_count')}  "
              f"frags={run_output.get('fragment_count')}")

        result = EvalResult(
            fixture_id=fixture_id,
            handler=run_output.get("handler_used", "unknown"),
            handler_version="1.0.0",
            run_at=datetime.now(UTC),
            metrics={
                "structural_accuracy": accuracy,
                "fragment_count": run_output.get("fragment_count"),
                "chunk_count": run_output.get("chunk_count"),
                "table_count": run_output.get("table_count"),
                "handler_matched": run_output.get("handler_matched"),
                "extraction_accuracy": None,   # Phase 3
                "chunk_faithfulness": None,    # Phase 3
                "retrieval_recall_at_10": None,  # Phase 3
                "ndcg_at_10": None,            # Phase 3
            },
            errors=run_output.get("warnings", []),
        )
        results.append(result)

    run_id = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
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

    passed = sum(1 for r in results if r.metrics.get("structural_accuracy") == 1.0)
    print(f"\n{passed}/{len(results)} fixtures passed — results written to {out_path}")
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
