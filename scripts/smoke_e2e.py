#!/usr/bin/env python
"""Pre-launch E2E smoke harness — drives every pipeline through the LIVE HTTP API.

Unlike the eval harness (which runs handlers in-process), this exercises the real
upload -> ARQ worker -> Postgres -> search path over HTTP, the way a user does. It
provisions a throwaway tenant, walks every format to `indexed`, verifies search in
all three modes, and checks graceful failure paths.

Usage:
    # API + CPU worker + GPU worker must be running; infra (pg/redis/minio) up.
    uv run python scripts/smoke_e2e.py [--base-url http://127.0.0.1:8000]

Exits non-zero if any check fails — suitable as a release gate / CI step.
Note: audio/video run whisper (CPU fallback is slow), so a full run can take a
few minutes.
"""
from __future__ import annotations

import argparse
import sys
import time
import uuid
from pathlib import Path

import httpx

from omnivore.config import get_settings

ROOT = Path(__file__).resolve().parent.parent
FIX = ROOT / "eval" / "fixtures"

# (fixture file, expected handler name). Covers all 11 handlers via the live path.
FORMATS = [
    (FIX / "csv-001/sample.csv", "csv"),
    (FIX / "html-001/sample.html", "html"),
    (FIX / "json-001/sample.json", "json"),
    (FIX / "md-001/sample.md", "markdown"),
    (FIX / "txt-001/sample.txt", "text"),
    (FIX / "pdf-001/sample.pdf", "pdf"),
    (FIX / "docx-001/sample.docx", "docx"),
    (FIX / "xlsx-001/sample.xlsx", "xlsx"),
    (FIX / "image-001/sample.png", "image-ocr"),
    (FIX / "audio-001/sample.wav", "audio"),
    (FIX / "video-001/sample.mp4", "video"),
]

results: list[tuple[str, str, str, str]] = []  # (section, name, PASS/FAIL/SKIP, detail)


def record(section: str, name: str, ok: bool | None, detail: str = "") -> None:
    status = "SKIP" if ok is None else ("PASS" if ok else "FAIL")
    results.append((section, name, status, detail))
    print(f"  [{status}] {section}: {name} {('— ' + detail) if detail else ''}")


def poll(client: httpx.Client, doc_id: str, key: str, timeout: float) -> dict:
    """Poll a document until it reaches a terminal status or timeout."""
    deadline = time.monotonic() + timeout
    last: dict = {}
    while time.monotonic() < deadline:
        r = client.get(f"/v1/documents/{doc_id}", headers={"X-API-Key": key})
        last = r.json().get("data", {}) if r.status_code == 200 else {"status": f"http-{r.status_code}"}
        if last.get("status") in ("indexed", "failed", "duplicate"):
            return last
        time.sleep(2)
    return last


def upload(client: httpx.Client, key: str, path: Path, content: bytes | None = None,
           filename: str | None = None) -> httpx.Response:
    data = content if content is not None else path.read_bytes()
    files = {"file": (filename or path.name, data, "application/octet-stream")}
    return client.post("/v1/documents", headers={"X-API-Key": key}, files=files)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base-url", default="http://127.0.0.1:8000")
    args = ap.parse_args()

    settings = get_settings()
    admin_token = settings.ADMIN_BOOTSTRAP_TOKEN.get_secret_value()
    client = httpx.Client(base_url=args.base_url, timeout=30.0)

    # readiness
    try:
        ready = client.get("/v1/ready")
        if ready.status_code != 200:
            print(f"FATAL: /v1/ready returned {ready.status_code}: {ready.text[:200]}")
            return 2
    except httpx.HTTPError as e:
        print(f"FATAL: cannot reach API at {args.base_url}: {e}")
        return 2

    # provision a throwaway tenant
    slug = f"smoke-{uuid.uuid4().hex[:8]}"
    r = client.post("/v1/admin/tenants", headers={"X-Admin-Token": admin_token},
                    json={"slug": slug, "display_name": "Smoke"})
    if r.status_code not in (200, 201):
        print(f"FATAL: tenant provisioning failed ({r.status_code}): {r.text[:300]}")
        return 2
    key = r.json()["data"]["api_key"]
    print(f"provisioned tenant {slug}\n")

    # ---- Section 1: format coverage (live HTTP) ----
    print("== format coverage ==")
    for path, expected_handler in FORMATS:
        if not path.exists():
            record("format", expected_handler, None, f"missing fixture {path}")
            continue
        up = upload(client, key, path)
        if up.status_code != 202:
            record("format", expected_handler, False, f"upload {up.status_code}: {up.text[:120]}")
            continue
        doc_id = up.json()["data"]["document_id"]
        # whisper on CPU is slow; give gpu-class formats a long deadline
        timeout = 360.0 if expected_handler in ("audio", "video") else 90.0
        final = poll(client, doc_id, key, timeout)
        st, handler = final.get("status"), final.get("handler")
        ok = st == "indexed" and handler == expected_handler
        record("format", expected_handler, ok, f"status={st} handler={handler}")

    # ---- Section 2: search pipeline (sentinel doc, all 3 modes) ----
    print("\n== search modes ==")
    sentinel = f"zylqx{uuid.uuid4().hex[:8]}"  # unique, lexically distinctive token
    body = f"# Smoke search doc\n\nThe magic sentinel token is {sentinel} and it is unique.\n".encode()
    up = upload(client, key, FIX / "md-001/sample.md", content=body, filename="sentinel.md")
    if up.status_code == 202:
        doc_id = up.json()["data"]["document_id"]
        final = poll(client, doc_id, key, 90.0)
        if final.get("status") == "indexed":
            for mode in ("bm25", "vector", "hybrid"):
                sr = client.post("/v1/search", headers={"X-API-Key": key},
                                 json={"query": sentinel, "mode": mode, "top_k": 5})
                hits = sr.json().get("data", []) if sr.status_code == 200 else []
                found = any(doc_id == h.get("document_id") for h in hits)
                record("search", mode, found, f"{len(hits)} hits, sentinel doc {'found' if found else 'MISSING'}")
        else:
            record("search", "all", False, f"sentinel doc not indexed: {final.get('status')}")
    else:
        record("search", "all", False, f"sentinel upload {up.status_code}")

    # ---- Section 3: edge / failure cases ----
    print("\n== edge / failure ==")
    # duplicate detection (re-upload an already-ingested fixture)
    dup = upload(client, key, FIX / "csv-001/sample.csv")
    dup_status = dup.json().get("data", {}).get("status") if dup.status_code == 202 else None
    record("edge", "duplicate(sha256)", dup_status == "duplicate", f"status={dup_status}")

    # unsupported type — accepted then worker marks failed (graceful, no crash)
    up = upload(client, key, Path("x"), content=b"\x00\x01not-a-known-format\xff" * 8, filename="mystery.xyz")
    if up.status_code == 202:
        final = poll(client, up.json()["data"]["document_id"], key, 60.0)
        record("edge", "unsupported", final.get("status") == "failed", f"status={final.get('status')}")
    else:
        record("edge", "unsupported", up.status_code in (400, 415, 422), f"rejected {up.status_code}")

    # empty file
    up = upload(client, key, Path("e"), content=b"", filename="empty.txt")
    if up.status_code == 202:
        final = poll(client, up.json()["data"]["document_id"], key, 60.0)
        record("edge", "empty-file", final.get("status") in ("failed", "indexed"),
               f"status={final.get('status')} (graceful)")
    else:
        record("edge", "empty-file", up.status_code in (400, 413, 422), f"rejected {up.status_code}")

    # corrupt PDF (garbage with a .pdf name)
    up = upload(client, key, Path("c"), content=b"%PDF-1.4\n garbage not really a pdf " * 4, filename="broken.pdf")
    if up.status_code == 202:
        final = poll(client, up.json()["data"]["document_id"], key, 90.0)
        record("edge", "corrupt-pdf", final.get("status") in ("failed", "indexed"),
               f"status={final.get('status')} (no crash)")
    else:
        record("edge", "corrupt-pdf", True, f"rejected {up.status_code}")

    # oversize 413 — only feasible to test if the limit is small enough to generate
    limit = settings.MAX_UPLOAD_SIZE_BYTES
    if limit <= 25 * 1024 * 1024:
        up = upload(client, key, Path("o"), content=b"x" * (limit + 1), filename="big.txt")
        record("edge", "oversize-413", up.status_code == 413, f"status={up.status_code}")
    else:
        record("edge", "oversize-413", None,
               f"MAX_UPLOAD_SIZE_BYTES={limit} too large to trigger here; the 413/429 "
               "limit paths are covered deterministically in tests/unit + tests/chaos")

    # ---- summary ----
    print("\n" + "=" * 60)
    passed = [r for r in results if r[2] == "PASS"]
    failed = [r for r in results if r[2] == "FAIL"]
    skipped = [r for r in results if r[2] == "SKIP"]
    print(f"SMOKE RESULT: {len(passed)} pass, {len(failed)} fail, {len(skipped)} skip")
    for sec, name, st, detail in failed:
        print(f"  FAIL {sec}/{name}: {detail}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
