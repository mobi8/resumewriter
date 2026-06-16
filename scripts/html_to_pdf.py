#!/usr/bin/env python3
import signal
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

SCRIPT_TIMEOUT_SECONDS = 45
GOTO_TIMEOUT_MS = 15_000


class ScriptTimeoutError(TimeoutError):
    pass


def log(message: str) -> None:
    print(f"[html_to_pdf] {message}", flush=True)


def _handle_timeout(signum, frame) -> None:
    raise ScriptTimeoutError(f"html_to_pdf.py exceeded {SCRIPT_TIMEOUT_SECONDS}s timeout")


def main() -> int:
    if len(sys.argv) != 3:
        print("Usage: html_to_pdf.py input.html output.pdf", file=sys.stderr)
        return 2

    input_html = Path(sys.argv[1]).resolve()
    output_pdf = Path(sys.argv[2]).resolve()

    signal.signal(signal.SIGALRM, _handle_timeout)
    signal.alarm(SCRIPT_TIMEOUT_SECONDS)

    browser = None
    try:
        with sync_playwright() as playwright:
            try:
                log("launching chromium")
                browser = playwright.chromium.launch(
                    headless=True,
                    args=["--no-sandbox", "--disable-dev-shm-usage"],
                    timeout=GOTO_TIMEOUT_MS,
                )
                page = browser.new_page()

                def handle_route(route):
                    request_url = route.request.url
                    if request_url.startswith(("http://", "https://")):
                        log(f"aborting external request: {request_url}")
                        route.abort()
                        return
                    route.continue_()

                page.route("**/*", handle_route)
                page.set_default_timeout(GOTO_TIMEOUT_MS)
                page.set_default_navigation_timeout(GOTO_TIMEOUT_MS)

                log(f"loading html: {input_html}")
                page.goto(input_html.as_uri(), wait_until="domcontentloaded", timeout=GOTO_TIMEOUT_MS)

                log("emulating print media")
                page.emulate_media(media="print")

                log(f"creating pdf: {output_pdf}")
                page.pdf(
                    path=str(output_pdf),
                    format="A4",
                    margin={"top": "18mm", "bottom": "18mm", "left": "18mm", "right": "18mm"},
                    print_background=True,
                )
                log("pdf created successfully")
            finally:
                if browser:
                    log("closing browser")
                    browser.close()
    except ScriptTimeoutError as exc:
        print(f"[html_to_pdf] ERROR: {exc}", file=sys.stderr, flush=True)
        return 124
    except Exception as exc:
        print(f"[html_to_pdf] ERROR: {type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
        return 1
    finally:
        signal.alarm(0)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
