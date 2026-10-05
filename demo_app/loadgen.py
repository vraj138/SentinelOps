"""Load generator: sends a steady stream of checkouts so the stack always has metrics."""

import logging
import time

import httpx

from demo_app.common import DemoSettings

logger = logging.getLogger(__name__)


def run(client: httpx.Client, requests_per_second: float, iterations: int | None = None) -> int:
    """Send checkouts at a fixed rate. Returns the number of failed requests.

    Runs forever when `iterations` is None. Failures are logged, never hidden.
    """
    failures = 0
    sent = 0
    while iterations is None or sent < iterations:
        try:
            response = client.post("/checkout")
            if response.is_error:
                failures += 1
                logger.warning("checkout returned %s: %s", response.status_code, response.text)
        except httpx.HTTPError as exc:
            failures += 1
            logger.warning("checkout request failed: %r", exc)
        sent += 1
        time.sleep(1 / requests_per_second)
    return failures


def main() -> None:
    """Entry point for the loadgen container."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    settings = DemoSettings()
    logger.info("sending %.1f checkouts/sec to %s", settings.loadgen_rps, settings.gateway_url)
    with httpx.Client(
        base_url=settings.gateway_url, timeout=settings.http_timeout_seconds
    ) as client:
        run(client, settings.loadgen_rps)


if __name__ == "__main__":
    main()
