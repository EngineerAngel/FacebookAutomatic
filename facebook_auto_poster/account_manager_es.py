"""
account_manager_es.py — Orquesta instancias de FacebookPoster entre cuentas.

Soporta modos de ejecución secuencial y paralelo (multiprocessing).
"""

import logging
import multiprocessing
import random
import time
from datetime import datetime

from config import AccountConfig
from facebook_poster import FacebookPoster

# ---------------------------------------------------------------------------
# Logger a nivel módulo (main.log)
# ---------------------------------------------------------------------------
logger = logging.getLogger("account_manager")


# ---------------------------------------------------------------------------
# Función de trabajador para modo paralelo (debe ser de nivel superior para pickl)
# ---------------------------------------------------------------------------
def _worker(
    account: AccountConfig,
    config: dict,
    text: str,
    shared_results: dict,
) -> None:
    """Ejecuta una sesión completa para una cuenta dentro de un proceso hijo."""
    poster = FacebookPoster(account, config)
    try:
        if not poster.login():
            logger.error(
                "La sesión falló para %s — omitiendo", account.name
            )
            shared_results[account.name] = {}
            return

        results = poster.publish_to_all_groups(text)
        shared_results[account.name] = dict(results)
    except Exception:
        logger.error(
            "Error no manejado en el trabajador para %s",
            account.name,
            exc_info=True,
        )
        shared_results[account.name] = {}
    finally:
        poster.close()


# ---------------------------------------------------------------------------
# AccountManager
# ---------------------------------------------------------------------------
class AccountManager:
    """Gestiona sesiones de publicación para múltiples cuentas de Facebook."""

    def __init__(
        self,
        accounts: list[AccountConfig],
        config: dict,
        text: str,
    ) -> None:
        self.accounts = accounts
        self.config = config
        self.text = text

    # ------------------------------------------------------------------ #
    # Ejecución secuencial
    # ------------------------------------------------------------------ #
    def run_sequential(self) -> dict[str, dict[str, bool]]:
        summary: dict[str, dict[str, bool]] = {}

        for idx, account in enumerate(self.accounts):
            logger.info("Iniciando sesión para %s", account.name)
            poster = FacebookPoster(account, self.config)

            try:
                if not poster.login():
                    logger.error(
                        "La sesión falló para %s — omitiendo", account.name
                    )
                    summary[account.name] = {}
                    continue

                results = poster.publish_to_all_groups(self.text)
                summary[account.name] = results

            except Exception:
                logger.error(
                    "Error no manejado para %s", account.name, exc_info=True
                )
                summary[account.name] = {}
            finally:
                poster.close()

            # Esperar entre cuentas (omitir después de la última)
            if idx < len(self.accounts) - 1:
                delay = random.uniform(
                    self.config["wait_between_accounts_min"],
                    self.config["wait_between_accounts_max"],
                )
                logger.info(
                    "Esperando %.0f s antes de la siguiente cuenta …", delay
                )
                time.sleep(delay)

        return summary

    # ------------------------------------------------------------------ #
    # Ejecución paralela
    # ------------------------------------------------------------------ #
    def run_parallel(self) -> dict[str, dict[str, bool]]:
        manager = multiprocessing.Manager()
        shared_results: dict = manager.dict()

        processes: list[multiprocessing.Process] = []
        for account in self.accounts:
            p = multiprocessing.Process(
                target=_worker,
                args=(account, self.config, self.text, shared_results),
                name=f"poster-{account.name}",
            )
            processes.append(p)

        # Iniciar todos, luego esperar todos
        for p in processes:
            p.start()
            logger.info(
                "Proceso iniciado: %s (pid %s)", p.name, p.pid
            )

        for p in processes:
            p.join()
            logger.info("Proceso finalizado: %s", p.name)

        # Convertir dict administrado a dict simple
        return {k: dict(v) for k, v in shared_results.items()}

    # ------------------------------------------------------------------ #
    # Punto de entrada unificado
    # ------------------------------------------------------------------ #
    def run(self) -> dict[str, dict[str, bool]]:
        # Guardia: verificar horas permitidas de publicación
        current_hour = datetime.now().hour
        if current_hour not in self.config["post_hours_allowed"]:
            logger.warning(
                "La hora actual (%d) está fuera de la ventana de publicación "
                "permitida %s. Omitiendo esta ejecución.",
                current_hour,
                f"{self.config['post_hours_allowed'].start}–"
                f"{self.config['post_hours_allowed'].stop - 1}",
            )
            return {}

        mode = self.config.get("execution_mode", "sequential")
        logger.info("Modo de ejecución: %s", mode)

        if mode == "parallel":
            return self.run_parallel()
        return self.run_sequential()

    # ------------------------------------------------------------------ #
    # Resumen bonito
    # ------------------------------------------------------------------ #
    @staticmethod
    def print_summary(results: dict[str, dict[str, bool]]) -> None:
        if not results:
            print("\nNo hay resultados para mostrar.\n")
            return

        header = (
            f"{'Cuenta':<15} | {'Grupos OK':>9} | {'Grupos fallidos':>15} | {'Total':>5}"
        )
        sep = "-" * len(header)

        print(f"\n{header}")
        print(sep)

        for account, groups in results.items():
            ok = sum(1 for v in groups.values() if v)
            fail = sum(1 for v in groups.values() if not v)
            total = len(groups)
            print(
                f"{account:<15} | {ok:>9} | {fail:>15} | {total:>5}"
            )

        print(sep)
        print()
