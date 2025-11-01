import json
import logging
import os
import random
import time
from concurrent.futures import ThreadPoolExecutor, as_completed, Future
from datetime import datetime, timezone
from typing import Any

from x402_minter import X402Minter, X402MintError


# --- logging setup ---
logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s [%(threadName)s] %(message)s",
)
logger = logging.getLogger(__name__)


def run_minter_for_account(
    private_key: str,
    url: str,
    amount: int,
    *,
    network: str = "base",
    scheme: str = "exact",
    x402_version: int = 1,
) -> dict[str, Any]:
    """
    Run a mint session for a single account.
    Returns a structured dict including success flag and results/error.
    """
    suffix = private_key[-6:]

    try:
        # Use verbose=False to suppress minter's print noise in parallel runs
        minter = X402Minter(
            private_key,
            network=network,
            scheme=scheme,
            x402_version=x402_version,
        )
        t0 = time.time()
        results: list[dict[str, Any]] = minter.mint(url, amount)
        elapsed = time.time() - t0

        logger.info("Account %s finished OK in %.2fs (items=%d)", suffix, elapsed, len(results))
        return {
            "private_key_suffix": suffix,
            "ok": True,
            "elapsed_sec": round(elapsed, 3),
            "results": results,
        }

    except (X402MintError, ValueError) as e:
        # Known/expected failures: validation or permanent mint failure
        logger.warning("Account %s finished with expected error: %s", suffix, e, exc_info=False)
        return {
            "private_key_suffix": suffix,
            "ok": False,
            "error": str(e),
            "error_type": type(e).__name__,
        }
    except Exception as e:
        # Unexpected failures: keep traceback for diagnostics
        logger.exception("Account %s crashed with unexpected error", suffix)
        return {
            "private_key_suffix": suffix,
            "ok": False,
            "error": str(e),
            "error_type": type(e).__name__,
        }


def run_parallel_mints(
    private_keys: list[str],
    url: str,
    amount_per_account: int,
    *,
    max_workers: int = 10,
    network: str = "base",
    scheme: str = "exact",
    x402_version: int = 1,
    verbose_minter: bool = False,
) -> list[dict[str, Any]]:
    """
    Launch multiple accounts in parallel with bounded concurrency.
    Returns a list of per-account result dicts.
    """
    results: list[dict[str, Any]] = []
    logger.info(
        "Starting parallel mints: accounts=%d, amount_per_account=%d, workers=%d",
        len(private_keys),
        amount_per_account,
        max_workers,
    )

    with ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="x402") as ex:
        # Submit all tasks at once; executor enforces the concurrency bound
        future_map: dict[Future, str] = {}
        for pk in private_keys:
            fut = ex.submit(
                run_minter_for_account,
                pk,
                url,
                amount_per_account,
                network=network,
                scheme=scheme,
                x402_version=x402_version,
            )
            future_map[fut] = pk[-6:]

        # Collect as they complete
        for fut in as_completed(future_map):
            suffix = future_map[fut]
            try:
                res = fut.result()
            except Exception as e:
                logger.exception("Account %s future raised unexpected error", suffix)
                res = {
                    "private_key_suffix": suffix,
                    "ok": False,
                    "error": str(e),
                    "error_type": type(e).__name__,
                }
            results.append(res)

            if res.get("ok"):
                logger.info("Account %s OK", suffix)
            else:
                logger.warning("Account %s error: %s", suffix, res.get("error"))

    # Summary
    ok_count = sum(1 for r in results if r.get("ok"))
    logger.info("Summary: %d success, %d failed", ok_count, len(results) - ok_count)
    return results


def dump_results_json(
    results: list[dict[str, Any]],
    *,
    path: str | None = None,
) -> str:
    """
    Save results to JSON. If path is not provided, create a timestamped file.
    Returns the file path written.
    """
    if path is None:
        ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        path = f"x402_results_{ts}.json"

    with open(path, "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)

    logger.info("Saved results JSON to %s", path)
    return path


if __name__ == "__main__":
    # Fill with your private keys
    PRIVATE_KEYS: list[str] = [
        # "0xabc...",
        # "0xdef...",
    ]

    URL_MINT: str = "https://api.ping.observer/mint" # Example mint URL
    AMOUNT_PER_ACCOUNT: int = 1
    MAX_WORKERS: int = 10

    if not PRIVATE_KEYS:
        logger.error("No PRIVATE_KEYS provided. Exiting.")
        raise SystemExit(1)

    all_results = run_parallel_mints(
        PRIVATE_KEYS,
        URL_MINT,
        AMOUNT_PER_ACCOUNT,
        max_workers=MAX_WORKERS,
        network="base",
        scheme="exact",
        x402_version=1,
    )

    # Separate successes and failures for a clean final log
    success = [r for r in all_results if r.get("ok")]
    failed = [r for r in all_results if not r.get("ok")]

    logger.info("Final: %d success, %d failed", len(success), len(failed))
    for f in failed:
        logger.info("Failed %s: %s (%s)", f["private_key_suffix"], f.get("error"), f.get("error_type"))

    # Optional: persist results
    dump_results_json(all_results)
