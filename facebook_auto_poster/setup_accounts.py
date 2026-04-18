"""
setup_accounts.py — Login inicial para cada cuenta.

Abre el navegador por cada cuenta, hace login, te deja resolver
el CAPTCHA manualmente si aparece, y guarda las cookies.

Uso:
    python setup_accounts.py           → configura todas las cuentas
    python setup_accounts.py zofia     → configura solo una cuenta
"""

import logging
import sys
import time

from config import CONFIG, load_accounts
from facebook_poster import FacebookPoster

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - [%(name)s] - %(levelname)s - %(message)s",
)
logger = logging.getLogger("setup")


def setup_account(account, config):
    """Hace login para una cuenta y guarda las cookies."""
    logger.info("=" * 50)
    logger.info("Configurando cuenta: %s", account.name)
    logger.info("=" * 50)

    poster = FacebookPoster(account, config)

    try:
        # Intentar login automático primero
        poster.driver.get("https://www.facebook.com/login")
        poster.human_wait(2, 3)

        # Cargar cookies si existen
        if poster._load_cookies():
            poster.driver.refresh()
            poster.human_wait(3, 5)
            if poster._is_logged_in():
                logger.info(">>> %s ya tiene sesión activa (cookies válidas)", account.name)
                poster.close()
                return True

        # Login automático: escribir credenciales
        from selenium.webdriver.common.by import By
        from selenium.webdriver.common.keys import Keys
        from selenium.webdriver.support.ui import WebDriverWait
        from selenium.webdriver.support import expected_conditions as EC

        poster.driver.get("https://www.facebook.com/login")
        wait = WebDriverWait(poster.driver, 20)

        email_input = wait.until(
            EC.presence_of_element_located((By.XPATH, "//input[@name='email']"))
        )
        email_input.clear()
        email_input.send_keys(account.email)
        poster.human_wait(0.5, 1)

        pass_input = wait.until(
            EC.presence_of_element_located((By.XPATH, "//input[@name='pass']"))
        )
        pass_input.clear()
        pass_input.send_keys(account.password)
        poster.human_wait(0.5, 1)
        pass_input.send_keys(Keys.RETURN)

        # Esperar — si hay CAPTCHA, el usuario lo resuelve manualmente
        print(f"\n{'='*50}")
        print(f"  CUENTA: {account.name}")
        print(f"{'='*50}")
        print(f"  Si aparece un CAPTCHA, resuélvelo manualmente.")
        print(f"  Cuando veas el feed de Facebook (página de inicio),")
        print(f"  presiona ENTER aquí para guardar las cookies.")
        print(f"{'='*50}\n")

        input("  >>> Presiona ENTER cuando estés en el feed de inicio: ")

        # Verificar que realmente está logueado
        if poster._is_logged_in():
            poster._save_cookies()
            logger.info(">>> Cookies guardadas para %s", account.name)
            poster.close()
            return True
        else:
            logger.error(">>> No se detectó sesión activa para %s", account.name)
            poster._screenshot("setup_error.png")
            poster.close()
            return False

    except Exception:
        logger.error("Error configurando %s", account.name, exc_info=True)
        poster.close()
        return False


def main():
    accounts = load_accounts()

    # Filtrar por nombre si se pasa como argumento
    if len(sys.argv) > 1:
        name_filter = sys.argv[1].lower()
        accounts = [a for a in accounts if a.name == name_filter]
        if not accounts:
            logger.error("Cuenta '%s' no encontrada en .env", name_filter)
            sys.exit(1)

    print(f"\nCuentas a configurar: {', '.join(a.name for a in accounts)}\n")

    results = {}
    for account in accounts:
        success = setup_account(account, CONFIG)
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
    main()
