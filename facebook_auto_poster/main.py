"""
main.py — Entry point and scheduler for the Facebook Auto-Poster.

Runs sessions on a schedule (Tuesday 10:00, Thursday 14:30) or
can be invoked once via run_session().
"""

import logging
import os
import sys
import time
from pathlib import Path

import schedule

from config import CONFIG, load_accounts
from account_manager import AccountManager

# ---------------------------------------------------------------------------
# Global logger → logs/main.log + console
# ---------------------------------------------------------------------------
LOG_DIR = Path(__file__).resolve().parent / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)

main_logger = logging.getLogger("main")
main_logger.setLevel(logging.DEBUG)

_fh = logging.FileHandler(LOG_DIR / "main.log", encoding="utf-8")
_fh.setLevel(logging.DEBUG)
_fh.setFormatter(
    logging.Formatter("%(asctime)s - [%(name)s] - %(levelname)s - %(message)s")
)
main_logger.addHandler(_fh)

_ch = logging.StreamHandler()
_ch.setLevel(logging.INFO)
_ch.setFormatter(
    logging.Formatter("%(asctime)s - [%(name)s] - %(levelname)s - %(message)s")
)
main_logger.addHandler(_ch)


# ---------------------------------------------------------------------------
# Session runner
# ---------------------------------------------------------------------------
def run_session() -> None:
    """Execute a single posting session for all configured accounts."""
    main_logger.info("=== Starting new posting session ===")

    try:
        accounts = load_accounts()
    except ValueError as exc:
        main_logger.error("Configuration error: %s", exc)
        return

    main_logger.info(
        "Loaded %d account(s): %s",
        len(accounts),
        ", ".join(a.name for a in accounts),
    )
    main_logger.info("Execution mode: %s", CONFIG["execution_mode"])

    # Resolve post text
    text = os.getenv("POST_TEXT", "").strip()
    if not text:
        try:
            text = input("Enter post text: ").strip()
        except EOFError:
            main_logger.error("No post text provided and stdin is not available.")
            return

    if not text:
        main_logger.error("Post text is empty — aborting session.")
        return

    manager = AccountManager(accounts, CONFIG, text)
    results = manager.run()
    manager.print_summary(results)

    main_logger.info("=== Session finished ===")


# ---------------------------------------------------------------------------
# Scheduler
# ---------------------------------------------------------------------------
def main() -> None:
    main_logger.info("Facebook Auto-Poster starting up …")
    main_logger.info(
        "Scheduled runs: Tuesday 10:00, Thursday 14:30  "
        "(mode=%s)",
        CONFIG["execution_mode"],
    )

    schedule.every().tuesday.at("10:00").do(run_session)
    schedule.every().thursday.at("14:30").do(run_session)

    print(
        "Scheduler is running. Waiting for next scheduled time …\n"
        "Press Ctrl+C to exit.\n"
    )

    try:
        while True:
            schedule.run_pending()
            time.sleep(60)
    except KeyboardInterrupt:
        main_logger.info("Scheduler stopped by user.")
        print("\nGoodbye.")
        sys.exit(0)


if __name__ == "__main__":
    main()
