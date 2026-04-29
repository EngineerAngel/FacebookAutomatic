"""
account_manager.py — Orchestrates FacebookPoster instances across accounts.

Supports sequential and parallel (multiprocessing) execution modes.
"""

import logging
import multiprocessing
import random
import time
from multiprocessing.managers import DictProxy

from config import AccountConfig, is_account_hour_allowed
from facebook_poster import FacebookPoster

# ---------------------------------------------------------------------------
# Module-level logger (main.log)
# ---------------------------------------------------------------------------
logger = logging.getLogger("account_manager")


# ---------------------------------------------------------------------------
# Worker function for parallel mode (must be top-level for pickling)
# ---------------------------------------------------------------------------
def _worker(
    account: AccountConfig,
    config: dict,
    text: str,
    shared_results: dict,
    image_path: str | None = None,
    callback_url: str | None = None,
) -> None:
    """Run a full session for one account inside a child process."""
    poster = FacebookPoster(account, config, callback_url=callback_url)
    try:
        if not poster.login():
            logger.error("Login failed for %s — skipping", account.name)
            shared_results[account.name] = {}
            return

        results = poster.publish_to_all_groups(text, image_path=image_path)
        shared_results[account.name] = dict(results)
    except Exception:
        logger.error(
            "Unhandled error in worker for %s", account.name, exc_info=True
        )
        shared_results[account.name] = {}
    finally:
        poster.close()


# ---------------------------------------------------------------------------
# AccountManager
# ---------------------------------------------------------------------------
class AccountManager:
    """Manage posting sessions for multiple Facebook accounts."""

    def __init__(
        self,
        accounts: list[AccountConfig],
        config: dict,
        text: str,
        image_path: str | None = None,
        callback_url: str | None = None,
        skip_hour_check: bool = False,
    ) -> None:
        self.accounts = accounts
        self.config = config
        self.text = text
        self.image_path = image_path
        self.callback_url = callback_url
        self.skip_hour_check = skip_hour_check

    # ------------------------------------------------------------------ #
    # Sequential execution
    # ------------------------------------------------------------------ #
    def run_sequential(self) -> dict[str, dict[str, bool]]:
        summary: dict[str, dict[str, bool]] = {}

        for idx, account in enumerate(self.accounts):
            logger.info("Starting session for %s", account.name)
            poster = FacebookPoster(account, self.config, callback_url=self.callback_url)

            try:
                if not poster.login():
                    logger.error("Login failed for %s — skipping", account.name)
                    summary[account.name] = {}
                    continue

                results = poster.publish_to_all_groups(self.text, image_path=self.image_path)
                summary[account.name] = results

            except Exception:
                logger.error(
                    "Unhandled error for %s", account.name, exc_info=True
                )
                summary[account.name] = {}
            finally:
                poster.close()

            # Wait between accounts (skip after the last one)
            if idx < len(self.accounts) - 1:
                delay = random.uniform(
                    self.config["wait_between_accounts_min"],
                    self.config["wait_between_accounts_max"],
                )
                logger.info("Waiting %.0f s before next account …", delay)
                time.sleep(delay)

        return summary

    # ------------------------------------------------------------------ #
    # Parallel execution
    # ------------------------------------------------------------------ #
    def run_parallel(self) -> dict[str, dict[str, bool]]:
        manager = multiprocessing.Manager()
        shared_results: DictProxy = manager.dict()

        processes: list[multiprocessing.Process] = []
        for account in self.accounts:
            p = multiprocessing.Process(
                target=_worker,
                args=(account, self.config, self.text, shared_results,
                      self.image_path, self.callback_url),
                name=f"poster-{account.name}",
            )
            processes.append(p)

        # Start all, then join all
        for p in processes:
            p.start()
            logger.info("Process started: %s (pid %s)", p.name, p.pid)

        for p in processes:
            p.join()
            logger.info("Process finished: %s", p.name)

        # Convert managed dict to plain dict
        return {k: dict(v) for k, v in shared_results.items()}

    # ------------------------------------------------------------------ #
    # Unified entry point
    # ------------------------------------------------------------------ #
    def run(self) -> dict[str, dict[str, bool]]:
        if self.skip_hour_check:
            logger.info("Restricción horaria omitida (publicación manual desde admin)")
        else:
            in_window = [a for a in self.accounts if is_account_hour_allowed(a)]
            skipped = [a for a in self.accounts if not is_account_hour_allowed(a)]
            for a in skipped:
                logger.warning(
                    "Cuenta '%s' fuera de su horario local (tz=%s ventana=%s-%s) — saltando",
                    a.name, a.timezone, a.active_hours[0], a.active_hours[1],
                )
            if not in_window:
                msg = "Todas las cuentas están fuera de su horario permitido"
                logger.warning(msg)
                raise ValueError(msg)
            self.accounts = in_window

        mode = self.config.get("execution_mode", "sequential")
        logger.info("Execution mode: %s", mode)

        if mode == "parallel":
            return self.run_parallel()
        return self.run_sequential()

    # ------------------------------------------------------------------ #
    # Pretty summary
    # ------------------------------------------------------------------ #
    @staticmethod
    def print_summary(results: dict[str, dict[str, bool]]) -> None:
        if not results:
            print("\nNo results to display.\n")
            return

        header = f"{'Account':<15} | {'Groups OK':>9} | {'Groups failed':>13} | {'Total':>5}"
        sep = "-" * len(header)

        print(f"\n{header}")
        print(sep)

        for account, groups in results.items():
            ok = sum(1 for v in groups.values() if v)
            fail = sum(1 for v in groups.values() if not v)
            total = len(groups)
            print(f"{account:<15} | {ok:>9} | {fail:>13} | {total:>5}")

        print(sep)
        print()
