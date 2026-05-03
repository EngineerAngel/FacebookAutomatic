"""
account_manager_async.py — Orchestrates FacebookPosterAsync instances.

Replaces AccountManager's multiprocessing backend with asyncio.Semaphore.
Feature flag: CONFIG["use_async_poster"] (default False).

Usage:
    mgr = AsyncAccountManager(accounts, config, text, image_paths=image_paths)
    results = await mgr.run()
"""

from __future__ import annotations

import asyncio
import logging
import random
from typing import Optional

from config import AccountConfig, is_account_hour_allowed
from facebook_poster_async import FacebookPosterAsync

logger = logging.getLogger("account_manager_async")


class AsyncAccountManager:
    """Manage posting sessions for multiple accounts concurrently via asyncio."""

    def __init__(
        self,
        accounts: list[AccountConfig],
        config: dict,
        text: str,
        image_paths: Optional[list[str]] = None,
        callback_url: Optional[str] = None,
    ) -> None:
        self.accounts = accounts
        self.config = config
        self.text = text
        self.image_paths = image_paths
        self.callback_url = callback_url

    # ------------------------------------------------------------------ #
    # Sequential (async, single at a time)
    # ------------------------------------------------------------------ #
    async def run_sequential(self) -> dict[str, dict[str, bool]]:
        summary: dict[str, dict[str, bool]] = {}

        for idx, account in enumerate(self.accounts):
            logger.info("Starting async session for %s", account.name)
            async with FacebookPosterAsync(account, self.config, callback_url=self.callback_url) as poster:
                try:
                    if not await poster.login():
                        logger.error("Login failed for %s — skipping", account.name)
                        summary[account.name] = {}
                        continue

                    results = await poster.publish_to_all_groups(self.text, image_paths=self.image_paths)
                    summary[account.name] = results

                except Exception:
                    logger.error("Unhandled error for %s", account.name, exc_info=True)
                    summary[account.name] = {}

            if idx < len(self.accounts) - 1:
                delay = random.uniform(
                    self.config["wait_between_accounts_min"],
                    self.config["wait_between_accounts_max"],
                )
                logger.info("Waiting %.0f s before next account …", delay)
                await asyncio.sleep(delay)

        return summary

    # ------------------------------------------------------------------ #
    # Parallel (bounded concurrency via Semaphore)
    # ------------------------------------------------------------------ #
    async def run_parallel(self) -> dict[str, dict[str, bool]]:
        max_concurrent = self.config.get("max_concurrent_accounts", 3)
        sem = asyncio.Semaphore(max_concurrent)
        results: dict[str, dict[str, bool]] = {}

        async def _run_one(account: AccountConfig) -> None:
            async with sem:
                logger.info("Starting async session for %s (parallel)", account.name)
                async with FacebookPosterAsync(
                    account, self.config, callback_url=self.callback_url
                ) as poster:
                    try:
                        if not await poster.login():
                            logger.error("Login failed for %s — skipping", account.name)
                            results[account.name] = {}
                            return
                        account_results = await poster.publish_to_all_groups(
                            self.text, image_paths=self.image_paths
                        )
                        results[account.name] = account_results
                    except Exception:
                        logger.error("Unhandled error for %s", account.name, exc_info=True)
                        results[account.name] = {}

        tasks = [asyncio.create_task(_run_one(account)) for account in self.accounts]
        await asyncio.gather(*tasks, return_exceptions=True)
        return results

    # ------------------------------------------------------------------ #
    # Unified entry point
    # ------------------------------------------------------------------ #
    async def run(self) -> dict[str, dict[str, bool]]:
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
        logger.info("Async execution mode: %s", mode)

        if mode == "parallel":
            return await self.run_parallel()
        return await self.run_sequential()

    # ------------------------------------------------------------------ #
    # Pretty summary (same as sync AccountManager)
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
