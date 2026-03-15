"""
facebook_poster_es.py — Publicador de grupos de Facebook basado en Selenium.

Cada instancia de FacebookPoster está vinculada a un único AccountConfig y
gestiona su propio WebDriver, logger y directorio de capturas.
"""

import logging
import os
import random
import time
from pathlib import Path

from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

try:
    from webdriver_manager.chrome import ChromeDriverManager
except ImportError:
    ChromeDriverManager = None  # type: ignore[assignment,misc]

from config import AccountConfig

# ---------------------------------------------------------------------------
# Ayudantes de variación de texto
# ---------------------------------------------------------------------------
_PREFIXES = [
    "",
    "\u200b",          # espacio de ancho cero (variación invisible)
    "\u00a0",          # espacio de no ruptura
]

_SUFFIXES = [
    "",
    " \u200b",
    ".",
    " \u2063",         # separador invisible
    "  ",
]


def _vary_text(text: str, account_name: str) -> str:
    """Devuelve una copia sutilmente variada de *text* que es estable por cuenta."""
    rng = random.Random(account_name)
    prefix = rng.choice(_PREFIXES)
    suffix = rng.choice(_SUFFIXES)
    return f"{prefix}{text}{suffix}"


# ---------------------------------------------------------------------------
# FacebookPoster
# ---------------------------------------------------------------------------
class FacebookPoster:
    """Conduce una única sesión de Facebook para una cuenta."""

    def __init__(self, account: AccountConfig, config: dict) -> None:
        self.account = account
        self.config = config

        # -- logger por cuenta -----------------------------------------
        self.logger = logging.getLogger(f"poster.{account.name}")
        self.logger.setLevel(logging.DEBUG)

        # Manejador de archivo (por cuenta)
        os.makedirs(os.path.dirname(account.log_file), exist_ok=True)
        fh = logging.FileHandler(account.log_file, encoding="utf-8")
        fh.setLevel(logging.DEBUG)
        fh.setFormatter(
            logging.Formatter(
                "%(asctime)s - [%(name)s] - %(levelname)s - %(message)s"
            )
        )
        self.logger.addHandler(fh)

        # Manejador de consola
        ch = logging.StreamHandler()
        ch.setLevel(logging.INFO)
        ch.setFormatter(
            logging.Formatter(
                "%(asctime)s - [%(name)s] - %(levelname)s - %(message)s"
            )
        )
        self.logger.addHandler(ch)

        # -- directorio de capturas -----------------------------------
        Path(account.screenshots_dir).mkdir(parents=True, exist_ok=True)

        # -- WebDriver ------------------------------------------------
        self.driver = self._build_driver()

    # ------------------------------------------------------------------ #
    # Configuración de WebDriver
    # ------------------------------------------------------------------ #
    def _build_driver(self) -> webdriver.Chrome:
        opts = Options()

        # Anti-detección básica
        opts.add_argument(
            "user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        )
        opts.add_argument("--disable-blink-features=AutomationControlled")
        opts.add_experimental_option("excludeSwitches", ["enable-automation"])
        opts.add_experimental_option("useAutomationExtension", False)

        w, h = self.config["browser_window_size"]
        opts.add_argument(f"--window-size={w},{h}")

        if self.config["browser_headless"]:
            opts.add_argument("--headless=new")

        opts.add_argument("--disable-notifications")
        opts.add_argument("--lang=es")  # coincidir con la interfaz española

        # Servicio — preferir ruta explícita, luego webdriver-manager, luego PATH
        chromedriver_path = os.getenv("CHROMEDRIVER_PATH", "").strip()
        if chromedriver_path:
            service = Service(executable_path=chromedriver_path)
        elif ChromeDriverManager is not None:
            service = Service(ChromeDriverManager().install())
        else:
            service = Service()  # confiar en PATH

        driver = webdriver.Chrome(service=service, options=opts)

        # Eliminar bandera navigator.webdriver
        driver.execute_cdp_cmd(
            "Page.addScriptToEvaluateOnNewDocument",
            {
                "source": (
                    "Object.defineProperty(navigator, 'webdriver', "
                    "{get: () => undefined})"
                )
            },
        )

        driver.implicitly_wait(self.config["implicit_wait"])
        self.logger.info("WebDriver inicializado para %s", self.account.name)
        return driver

    # ------------------------------------------------------------------ #
    # Ayudantes
    # ------------------------------------------------------------------ #
    def human_wait(self, min_s: float = 1, max_s: float = 3) -> None:
        time.sleep(random.uniform(min_s, max_s))

    def _screenshot(self, filename: str) -> None:
        path = os.path.join(self.account.screenshots_dir, filename)
        try:
            self.driver.save_screenshot(path)
            self.logger.info("Captura guardada: %s", path)
        except Exception:
            self.logger.warning(
                "No se pudo guardar la captura %s", path, exc_info=True
            )

    # ------------------------------------------------------------------ #
    # Iniciar sesión
    # ------------------------------------------------------------------ #
    def login(self) -> bool:
        """Inicia sesión en Facebook. Devuelve True en caso de éxito."""
        self.logger.info("Iniciando sesión como %s …", self.account.name)
        try:
            self.driver.get("https://www.facebook.com/login")
            wait = WebDriverWait(self.driver, 20)

            email_input = wait.until(
                EC.presence_of_element_located((By.XPATH, "//input[@name='email']"))
            )
            self.human_wait()
            email_input.clear()
            email_input.send_keys(self.account.email)

            self.human_wait(0.5, 1.5)

            pass_input = wait.until(
                EC.presence_of_element_located((By.XPATH, "//input[@name='pass']"))
            )
            pass_input.clear()
            pass_input.send_keys(self.account.password)

            self.human_wait(0.5, 1.5)

            submit_btn = wait.until(
                EC.element_to_be_clickable((By.XPATH, "//button[@type='submit']"))
            )
            submit_btn.click()

            # Esperar redirección al feed de inicio
            wait.until(
                lambda d: "/home" in d.current_url or "/?sk=h_nor" in d.current_url
            )

            self.human_wait(
                self.config["wait_after_login_min"],
                self.config["wait_after_login_max"],
            )
            self.logger.info("Sesión iniciada correctamente para %s", self.account.name)
            return True

        except Exception:
            self.logger.error(
                "FALLÓ la sesión para %s", self.account.name, exc_info=True
            )
            self._screenshot("login_error.png")
            return False

    # ------------------------------------------------------------------ #
    # Navegación
    # ------------------------------------------------------------------ #
    def navigate_to_group(self, group_id: str) -> bool:
        """Navega a un grupo de Facebook y verifica que la página se cargó."""
        url = f"https://www.facebook.com/groups/{group_id}"
        self.logger.info("Navegando al grupo %s", group_id)
        try:
            self.driver.get(url)
            self.human_wait(3, 6)

            # Verificar que realmente alcanzamos un grupo (buscar área de compositor)
            WebDriverWait(self.driver, 15).until(
                EC.presence_of_element_located(
                    (By.XPATH, "//div[@role='main']")
                )
            )
            self.logger.info("Grupo %s cargado", group_id)
            return True
        except Exception:
            self.logger.error(
                "No se pudo cargar el grupo %s", group_id, exc_info=True
            )
            self._screenshot(f"nav_error_{group_id}.png")
            return False

    # ------------------------------------------------------------------ #
    # Publicar
    # ------------------------------------------------------------------ #
    def publish(self, group_id: str, text: str) -> bool:
        """Publica *text* en un único grupo. Devuelve True en caso de éxito."""

        for attempt in range(1, self.config["max_retries"] + 1):
            self.logger.info(
                "Intento de publicación %d/%d para el grupo %s",
                attempt,
                self.config["max_retries"],
                group_id,
            )
            try:
                if not self.navigate_to_group(group_id):
                    continue

                wait = WebDriverWait(self.driver, 15)

                # --- Abrir compositor de posts ----------------------------
                try:
                    create_btn = wait.until(
                        EC.element_to_be_clickable(
                            (By.XPATH, "//span[contains(text(),'Crear')]")
                        )
                    )
                except Exception:
                    create_btn = wait.until(
                        EC.element_to_be_clickable(
                            (
                                By.XPATH,
                                "//div[@role='button']"
                                "[contains(@aria-label,'publicación')]",
                            )
                        )
                    )

                create_btn.click()
                self.human_wait(2, 4)

                # --- Escribir en contenteditable ------------------------
                editor = wait.until(
                    EC.presence_of_element_located(
                        (
                            By.XPATH,
                            "//div[@role='dialog']"
                            "//div[@contenteditable='true']",
                        )
                    )
                )
                editor.click()
                self.human_wait(0.5, 1)
                editor.send_keys(text)
                self.human_wait(1, 2)

                # --- Hacer clic en Publicar (enviar) ----------------------
                try:
                    pub_btn = wait.until(
                        EC.element_to_be_clickable(
                            (By.XPATH, "//div[@aria-label='Publicar']")
                        )
                    )
                except Exception:
                    pub_btn = wait.until(
                        EC.element_to_be_clickable(
                            (
                                By.XPATH,
                                "//button[contains(@aria-label,'Publicar')]",
                            )
                        )
                    )

                pub_btn.click()

                # Esperar a que el modal desaparezca como confirmación
                WebDriverWait(self.driver, 20).until(
                    EC.invisibility_of_element_located(
                        (By.XPATH, "//div[@role='dialog']")
                    )
                )
                self.logger.info(
                    "Publicado en el grupo %s correctamente", group_id
                )
                return True

            except Exception:
                self.logger.error(
                    "Error al publicar en el grupo %s (intento %d)",
                    group_id,
                    attempt,
                    exc_info=True,
                )
                self._screenshot(f"error_{group_id}.png")

        self.logger.error(
            "Se agotaron todos los %d intentos para el grupo %s",
            self.config["max_retries"],
            group_id,
        )
        return False

    # ------------------------------------------------------------------ #
    # Publicar en todos los grupos
    # ------------------------------------------------------------------ #
    def publish_to_all_groups(self, text: str) -> dict[str, bool]:
        """Publica en todos los grupos asignados a esta cuenta.

        Devuelve un dict que asigna group_id -> booleano de éxito.
        """
        results: dict[str, bool] = {}

        # Aplicar variación de texto por cuenta si está habilitada
        if self.config.get("text_variation_mode", False):
            text = _vary_text(text, self.account.name)
            self.logger.debug(
                "Variación de texto aplicada para %s", self.account.name
            )

        groups = self.account.groups[: self.config["max_groups_per_session"]]

        for idx, group_id in enumerate(groups):
            success = self.publish(group_id, text)
            results[group_id] = success

            # Esperar entre grupos, pero no después del último
            if idx < len(groups) - 1:
                delay = random.uniform(
                    self.config["wait_between_groups_min"],
                    self.config["wait_between_groups_max"],
                )
                self.logger.info(
                    "Esperando %.0f s antes del siguiente grupo …", delay
                )
                time.sleep(delay)

        return results

    # ------------------------------------------------------------------ #
    # Limpieza
    # ------------------------------------------------------------------ #
    def close(self) -> None:
        try:
            self.driver.quit()
            self.logger.info("Navegador cerrado para %s", self.account.name)
        except Exception:
            self.logger.warning(
                "Error al cerrar el navegador para %s",
                self.account.name,
                exc_info=True,
            )
