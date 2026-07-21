"""Batch WCAG review runner — tests all university homepages sequentially.

Reads URLs from Public_University_All_Contacts.xlsx, submits each to the
running WCAG tester server, and waits for completion before starting the
next. Uses local AI models to avoid API rate limits on 1,400+ sites.

Usage:
    1. Start the server:     python run.py
    2. In another terminal:  python batch_review.py [--start N] [--limit N]

Options:
    --start N    Skip first N institutions (for resuming after interruption)
    --limit N    Only process N institutions (for testing)
    --backend X  AI backend to use: "vllm" (default, local), "gemini", etc.
    --dry-run    Print URLs without submitting
"""
import argparse
import json
import os
import sys
import time

import httpx
import openpyxl

XLSX = "Public_University_All_Contacts.xlsx"
SERVER = "http://127.0.0.1:5050"
POLL_INTERVAL = 30  # seconds between status checks


def load_urls() -> list[dict]:
    """Load institution names + homepage URLs from the xlsx."""
    wb = openpyxl.load_workbook(XLSX, read_only=True)
    ws = wb["By Institution"]
    rows = []
    for row in ws.iter_rows(min_row=2, values_only=True):
        state, inst = row[0], row[1]
        url = row[16] if len(row) > 16 else None  # Column Q = index 16
        if inst and url:
            rows.append({
                "state": state,
                "institution": inst,
                "url": url.strip().rstrip("/"),
            })
    wb.close()
    return rows


def switch_backend(backend: str):
    """Update settings.json to use the specified AI backend."""
    settings_path = "settings.json"
    try:
        with open(settings_path) as f:
            settings = json.load(f)
    except Exception as exc:
        print(f"WARN: could not read {settings_path} ({exc}); starting from empty settings")
        settings = {}

    if backend == "vllm":
        settings["ai_backend"] = "vllm"
        settings["api_base_url"] = "http://localhost:8000/v1"
        settings["ai_model"] = "Qwen/Qwen3-32B"
        settings["ai_vision_model"] = "Qwen/Qwen2.5-VL-32B-Instruct"
        settings["ai_vision_api_url"] = "http://localhost:8000/v1"
        settings["ai_judge_model"] = "google/gemma-3-27b-it"
        settings["ai_judge_api_url"] = "http://localhost:8000/v1"
        settings["ai_judge_api_key"] = ""
        # No rate limit needed for local
        settings.pop("ai_timeout", None)
        print(f"Switched to LOCAL models (vllm)")
    else:
        # Keep existing cloud settings
        print(f"Using existing backend: {settings.get('ai_backend', 'unknown')}")
        return

    with open(settings_path, "w") as f:
        json.dump(settings, f, indent=2)


def submit_review(url: str, institution: str,
                   wcag_version: str = "2.1", level: str = "AA") -> str | None:
    """Submit a single-page review and return the review_id."""
    try:
        resp = httpx.post(
            f"{SERVER}/review/start",
            data={
                "url": url,
                "report_format": "508",
                "coverage_level": level,
                "wcag_version": wcag_version,
                "product_name": institution,
                "company_name": os.environ.get("WCAG_COMPANY_NAME", ""),
            },
            timeout=30,
        )
        if resp.status_code == 200:
            data = resp.json()
            review_id = data.get("review_id")
            if not review_id:
                print(f"    ERROR: server returned 200 but no review_id ({data})")
                return None
            return review_id
        else:
            print(f"    ERROR: HTTP {resp.status_code}: {resp.text[:200]}")
            return None
    except Exception as e:
        print(f"    ERROR: {e}")
        return None


def wait_for_completion(review_id: str, timeout: int = 3600) -> str:
    """Poll until the review completes or times out. Returns final status."""
    start = time.time()
    last_status = ""

    while time.time() - start < timeout:
        try:
            resp = httpx.get(f"{SERVER}/api/review/{review_id}/status", timeout=10)
            if resp.status_code == 200:
                data = resp.json()
                status = data.get("status", "unknown")

                if status != last_status:
                    elapsed = int(time.time() - start)
                    error = data.get("error") or ""
                    suffix = f" ({error})" if error else ""
                    print(f"    [{elapsed}s] status: {status}{suffix}")
                    last_status = status

                if status in ("complete", "error", "cancelled", "interrupted"):
                    return status
        except Exception as exc:
            print(f"    WARN: status poll for {review_id} failed ({exc}); retrying")

        time.sleep(POLL_INTERVAL)

    return "timeout"


def get_completed_reviews() -> set[str]:
    """Get URLs of already-completed reviews to skip on resume."""
    completed = set()
    try:
        resp = httpx.get(f"{SERVER}/api/reviews", timeout=10)
        if resp.status_code == 200:
            for review in resp.json():
                if review.get("status") == "complete" and review.get("source_url"):
                    completed.add(review["source_url"].strip().rstrip("/"))
    except Exception as exc:
        print(f"WARN: could not fetch completed-review list ({exc}); skip-completed disabled")
    return completed


def main():
    parser = argparse.ArgumentParser(description="Batch WCAG review runner")
    parser.add_argument("--start", type=int, default=0, help="Skip first N institutions")
    parser.add_argument("--limit", type=int, default=0, help="Process only N institutions")
    parser.add_argument("--backend", default="vllm", help="AI backend: vllm, gemini, etc.")
    parser.add_argument("--wcag", default="2.1", help="WCAG version: 2.0, 2.1, 2.2 (default: 2.1)")
    parser.add_argument("--level", default="AA", help="Coverage level: A, AA, AAA (default: AA)")
    parser.add_argument("--dry-run", action="store_true", help="Print URLs without submitting")
    parser.add_argument("--skip-completed", action="store_true", help="Skip already-reviewed URLs")
    args = parser.parse_args()

    # Load URLs
    urls = load_urls()
    print(f"Loaded {len(urls)} institutions from {XLSX}")

    # Apply start/limit
    if args.start:
        urls = urls[args.start:]
        print(f"Starting from index {args.start}")
    if args.limit:
        urls = urls[:args.limit]
        print(f"Limited to {args.limit} institutions")

    # Check for already-completed reviews
    if args.skip_completed:
        completed = get_completed_reviews()
        before = len(urls)
        urls = [u for u in urls if u["url"] not in completed]
        skipped = before - len(urls)
        if skipped:
            print(f"Skipping {skipped} already-completed reviews")

    if args.dry_run:
        print(f"\nDRY RUN — would process {len(urls)} URLs:")
        for i, u in enumerate(urls[:20]):
            print(f"  [{i+1}] {u['state']}: {u['institution']} -> {u['url']}")
        if len(urls) > 20:
            print(f"  ... +{len(urls) - 20} more")
        return

    # Switch to local backend
    switch_backend(args.backend)

    # Verify server is running
    try:
        resp = httpx.get(f"{SERVER}/api/queue/status", timeout=5)
        if resp.status_code != 200:
            print(f"ERROR: Server at {SERVER} returned {resp.status_code}")
            sys.exit(1)
    except Exception:
        print(f"ERROR: Cannot reach server at {SERVER}")
        print(f"Start it first:  python run.py")
        sys.exit(1)

    print(f"\nStarting batch review of {len(urls)} institutions...")
    print(f"Backend: {args.backend}")
    print(f"WCAG: {args.wcag} Level {args.level}")
    print(f"{'=' * 60}")

    succeeded = 0
    failed = 0
    start_time = time.time()

    for i, entry in enumerate(urls):
        state = entry["state"]
        inst = entry["institution"]
        url = entry["url"]

        print(f"\n[{i+1}/{len(urls)}] {state}: {inst}")
        print(f"    URL: {url}")

        review_id = submit_review(url, inst, args.wcag, args.level)
        if not review_id:
            failed += 1
            continue

        print(f"    Review ID: {review_id}")
        status = wait_for_completion(review_id)

        if status == "complete":
            succeeded += 1
            print(f"    COMPLETE")
        else:
            failed += 1
            print(f"    FAILED: {status}")

        # Progress summary
        elapsed = time.time() - start_time
        avg = elapsed / (i + 1)
        remaining = avg * (len(urls) - i - 1)
        print(f"    Progress: {succeeded} ok, {failed} failed, "
              f"~{remaining/3600:.1f}h remaining")

    # Final summary
    total_time = time.time() - start_time
    print(f"\n{'=' * 60}")
    print(f"BATCH COMPLETE")
    print(f"  Total: {len(urls)}")
    print(f"  Succeeded: {succeeded}")
    print(f"  Failed: {failed}")
    print(f"  Time: {total_time/3600:.1f} hours")
    print(f"{'=' * 60}")


if __name__ == "__main__":
    main()
