#!/usr/bin/env python3
import os
import signal
import subprocess
import sys
from pathlib import Path

SCRIPT_TIMEOUT_SECONDS = 45
GOTO_TIMEOUT_MS = 15_000
WORKER_FLAG = "--worker"


def log(message: str) -> None:
    print(f"[html_to_pdf] {message}", flush=True)


def worker_log(message: str) -> None:
    print(f"[html_to_pdf] {message}", file=sys.stderr, flush=True)


def run_worker(input_html: Path, output_pdf: Path) -> int:
    worker_log("worker started")
    from playwright.sync_api import sync_playwright

    browser = None
    try:
        with sync_playwright() as playwright:
            worker_log("playwright started")
            try:
                log("launching chromium")
                browser = playwright.chromium.launch(
                    headless=True,
                    args=["--no-sandbox", "--disable-dev-shm-usage"],
                    timeout=GOTO_TIMEOUT_MS,
                )
                worker_log("browser launched")
                page = browser.new_page()
                worker_log("page created")

                def handle_route(route):
                    request_url = route.request.url
                    if request_url.startswith(("http://", "https://")):
                        worker_log(f"aborting external request: {request_url}")
                        route.abort()
                        return
                    route.continue_()

                page.route("**/*", handle_route)
                page.set_default_timeout(GOTO_TIMEOUT_MS)
                page.set_default_navigation_timeout(GOTO_TIMEOUT_MS)

                log(f"loading html: {input_html}")
                page.goto(input_html.as_uri(), wait_until="domcontentloaded", timeout=GOTO_TIMEOUT_MS)
                worker_log("html loaded")
                worker_log("network idle skipped")

                log("emulating print media")
                page.emulate_media(media="print")

                log(f"creating pdf: {output_pdf}")
                worker_log("pdf generation started")
                page.pdf(
                    path=str(output_pdf),
                    format="A4",
                    margin={"top": "18mm", "bottom": "18mm", "left": "18mm", "right": "18mm"},
                    print_background=True,
                )
                worker_log("pdf generation completed")
                log("pdf created successfully")
            finally:
                if browser:
                    log("closing browser")
                    browser.close()
    except Exception as exc:
        print(f"[html_to_pdf] ERROR: {type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
        return 1

    return 0


def run_parent(input_html: Path, output_pdf: Path) -> int:
    worker_cmd = [sys.executable, str(Path(__file__).resolve()), WORKER_FLAG, str(input_html), str(output_pdf)]
    log(f"starting worker with {SCRIPT_TIMEOUT_SECONDS}s timeout")
    process = subprocess.Popen(worker_cmd, start_new_session=True)
    try:
        return_code = process.wait(timeout=SCRIPT_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGTERM)
            process.wait(timeout=5)
        except Exception:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        output_pdf.unlink(missing_ok=True)
        print(
            f"[html_to_pdf] ERROR: worker exceeded {SCRIPT_TIMEOUT_SECONDS}s timeout and was killed",
            file=sys.stderr,
            flush=True,
        )
        return 124

    if return_code != 0:
        output_pdf.unlink(missing_ok=True)
        print(
            f"[html_to_pdf] ERROR: worker failed with exit code {return_code}",
            file=sys.stderr,
            flush=True,
        )
        return return_code

    if not output_pdf.exists():
        print("[html_to_pdf] ERROR: worker completed but output PDF was not created", file=sys.stderr, flush=True)
        return 1

    return 0


def main() -> int:
    if len(sys.argv) == 4 and sys.argv[1] == WORKER_FLAG:
        return run_worker(Path(sys.argv[2]).resolve(), Path(sys.argv[3]).resolve())

    if len(sys.argv) != 3:
        print("Usage: html_to_pdf.py input.html output.pdf", file=sys.stderr)
        return 2

    return run_parent(Path(sys.argv[1]).resolve(), Path(sys.argv[2]).resolve())


if __name__ == "__main__":
    raise SystemExit(main())
