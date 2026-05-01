"""
setup_accounts.py — Login inicial para cada cuenta.

Abre el navegador por cada cuenta, hace login, te deja resolver
el CAPTCHA manualmente si aparece, y guarda las cookies.

Uso:
    python setup_accounts.py           → configura todas las cuentas
    python setup_accounts.py zofia     → configura solo una cuenta
"""

import asyncio
import logging
import sys

from config import CONFIG, load_accounts
from facebook_poster_async import FacebookPosterAsync

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - [%(name)s] - %(levelname)s - %(message)s",
)
logger = logging.getLogger("setup")


async def setup_account(account, config) -> bool:
    """Hace login para una cuenta y guarda las cookies."""
    async with FacebookPosterAsync(account, config) as poster:
        return await poster.setup_interactive()


async def main():
    accounts = load_accounts()

    # Filtrar por nombre si se pasa como argumento
    if len(sys.argv) > 1:
        name_filter = sys.argv[1].lower()
        accounts = [a for a in accounts if a.name == name_filter]
        if not accounts:
            logger.error("Cuenta '%s' no encontrada", name_filter)
            sys.exit(1)

    print(f"\nCuentas a configurar: {', '.join(a.name for a in accounts)}\n")

    results = {}
    for account in accounts:
        success = await setup_account(account, CONFIG)
        results[account.name] = success
        print()

    # Resumen
    print("\n" + "=" * 40)
    print("  RESUMEN DE CONFIGURACIÓN")
    print("=" * 40)
    for name, ok in results.items():
        status = "OK - cookies guardadas" if ok else "FALLÓ"
        print(f"  {name:<15} {status}")
    print("=" * 40 + "\n")


if __name__ == "__main__":
    asyncio.run(main())
