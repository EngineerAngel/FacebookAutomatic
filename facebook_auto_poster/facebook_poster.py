"""
facebook_poster.py — Selenium-based Facebook group poster.

Each FacebookPoster instance is bound to a single AccountConfig and
manages its own WebDriver, logger, and screenshots directory.
"""

import json
import logging
import os
import random
import time
from pathlib import Path

from selenium.common.exceptions import WebDriverException
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.chrome.webdriver import WebDriver as ChromeWebDriver
from selenium.webdriver.common.action_chains import ActionChains
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

try:
    from webdriver_manager.chrome import ChromeDriverManager
    _HAS_WDM = True
except ImportError:
    ChromeDriverManager = None  # type: ignore[assignment,misc]
    _HAS_WDM = False

from config import AccountConfig
import job_store

# ---------------------------------------------------------------------------
# Text-variation helpers
# ---------------------------------------------------------------------------
_PREFIXES = [
    "",
    "\u200b",          # zero-width space (invisible variation)
    "\u00a0",          # non-breaking space
]

_SUFFIXES = [
    "",
    " \u200b",
    ".",
    " \u2063",         # invisible separator
    "  ",
]


def _vary_text(text: str, account_name: str) -> str:
    """Return a subtly varied copy of *text* that is stable per account."""
    rng = random.Random(account_name)
    prefix = rng.choice(_PREFIXES)
    suffix = rng.choice(_SUFFIXES)
    return f"{prefix}{text}{suffix}"


# ---------------------------------------------------------------------------
# FacebookPoster
# ---------------------------------------------------------------------------
class FacebookPoster:
    """Drives a single Facebook session for one account."""

    def __init__(self, account: AccountConfig, config: dict) -> None:
        self.account = account
        self.config = config

        # -- per-account logger ------------------------------------------
        self.logger = logging.getLogger(f"poster.{account.name}")
        self.logger.setLevel(logging.DEBUG)

        # File handler (per-account)
        os.makedirs(os.path.dirname(account.log_file), exist_ok=True)
        fh = logging.FileHandler(account.log_file, encoding="utf-8")
        fh.setLevel(logging.DEBUG)
        fh.setFormatter(
            logging.Formatter(
                "%(asctime)s - [%(name)s] - %(levelname)s - %(message)s"
            )
        )
        self.logger.addHandler(fh)

        # Console handler — DEBUG para ver cada paso en la terminal
        ch = logging.StreamHandler()
        ch.setLevel(logging.DEBUG)
        ch.setFormatter(
            logging.Formatter(
                "%(asctime)s - [%(name)s] - %(levelname)s - %(message)s"
            )
        )
        self.logger.addHandler(ch)

        # -- screenshots dir ---------------------------------------------
        Path(account.screenshots_dir).mkdir(parents=True, exist_ok=True)

        # -- WebDriver ---------------------------------------------------
        self.driver = self._build_driver()

    # ------------------------------------------------------------------ #
    # WebDriver setup
    # ------------------------------------------------------------------ #
    def _build_driver(self) -> ChromeWebDriver:
        self.logger.info("[Driver] Construyendo WebDriver para %s ...", self.account.name)
        opts = Options()

        # Anti-detección
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
            self.logger.info("[Driver] Modo headless activado")

        opts.add_argument("--disable-notifications")
        opts.add_argument("--lang=es")

        # Service — prefer explicit path, then webdriver-manager, then PATH
        chromedriver_path = os.getenv("CHROMEDRIVER_PATH", "").strip()
        if chromedriver_path:
            self.logger.info("[Driver] ChromeDriver explícito: %s", chromedriver_path)
            service = Service(executable_path=chromedriver_path)
        elif _HAS_WDM and ChromeDriverManager is not None:
            self.logger.info("[Driver] Usando webdriver-manager para localizar ChromeDriver ...")
            service = Service(ChromeDriverManager().install())
        else:
            self.logger.info("[Driver] Buscando ChromeDriver en PATH del sistema ...")
            service = Service()

        self.logger.info("[Driver] Iniciando Chrome ...")
        try:
            driver = ChromeWebDriver(service=service, options=opts)
        except WebDriverException:
            self.logger.error("[Driver] FALLO al iniciar Chrome", exc_info=True)
            raise

        self.logger.info("[Driver] Chrome abierto. URL inicial: %s", driver.current_url)

        try:
            driver.execute_cdp_cmd(
                "Page.addScriptToEvaluateOnNewDocument",
                {"source": "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"},
            )
        except Exception:
            self.logger.warning("[Driver] No se pudo aplicar CDP anti-detección", exc_info=True)

        driver.implicitly_wait(self.config["implicit_wait"])
        self.logger.info("[Driver] WebDriver listo para %s", self.account.name)
        return driver

    # ------------------------------------------------------------------ #
    # Helpers
    # ------------------------------------------------------------------ #
    def human_wait(self, min_s: float = 1, max_s: float = 3) -> None:
        time.sleep(random.uniform(min_s, max_s))

    def _human_type(self, element, text: str) -> None:
        """Types text character by character simulating human keystroke timing."""
        for char in text:
            element.send_keys(char)
            if char == " ":
                time.sleep(random.uniform(0.10, 0.30))
            else:
                time.sleep(random.uniform(0.05, 0.18))
            if random.random() < 0.05:
                time.sleep(random.uniform(0.30, 0.80))

    def _human_click(self, element) -> None:
        """Moves mouse to element with slight random offset before clicking."""
        try:
            self._scroll_into_view(element)
            actions = ActionChains(self.driver)
            actions.move_to_element_with_offset(
                element,
                random.randint(-4, 4),
                random.randint(-4, 4),
            )
            actions.pause(random.uniform(0.10, 0.35))
            actions.click()
            actions.perform()
        except Exception:
            element.click()

    def _scroll_into_view(self, element) -> None:
        """Scrolls element into view smoothly before interacting."""
        self.driver.execute_script(
            "arguments[0].scrollIntoView({behavior:'smooth', block:'center'});",
            element,
        )
        time.sleep(random.uniform(0.30, 0.70))

    def _find_first(
        self,
        selectors: list[str],
        timeout: float = 10.0,
        condition=EC.element_to_be_clickable,
    ):
        """Tries each XPath selector in order, returns first element found.

        Each selector gets an equal share of the total timeout.
        Raises the last exception if none succeed.
        """
        per_timeout = max(round(timeout / len(selectors), 1), 2.0)
        last_exc: Exception = TimeoutError("No selector matched")
        for xpath in selectors:
            try:
                return WebDriverWait(self.driver, per_timeout).until(
                    condition((By.XPATH, xpath))
                )
            except Exception as exc:
                last_exc = exc
                self.logger.debug("[Selector] no encontrado: %s", xpath[:70])
        raise last_exc

    def _screenshot(self, filename: str) -> None:
        path = os.path.join(self.account.screenshots_dir, filename)
        try:
            self.driver.save_screenshot(path)
            self.logger.info("Screenshot saved: %s", path)
        except Exception:
            self.logger.warning("Failed to save screenshot %s", path, exc_info=True)

    def _attach_image(self, image_path: str, wait: WebDriverWait) -> None:
        """Adjunta imagen al compositor usando el input file oculto vía JS.

        Evita hacer clic en el botón 'Foto/vídeo' que puede recargar el compositor.
        """
        abs_path = os.path.abspath(image_path)

        # Revelar el input file oculto con JS y enviar el archivo directamente
        file_input = wait.until(
            EC.presence_of_element_located((By.XPATH, "//input[@type='file']"))
        )
        self.driver.execute_script(
            "arguments[0].style.display='block'; arguments[0].style.visibility='visible';",
            file_input,
        )
        file_input.send_keys(abs_path)
        self.logger.info("[Image] Archivo enviado al input: %s", abs_path)

        # Esperar a que el thumbnail de la imagen aparezca en el compositor
        try:
            wait.until(
                EC.presence_of_element_located(
                    (By.XPATH,
                     "//div[@role='dialog']//img[contains(@src,'blob:') "
                     "or contains(@src,'scontent')]")
                )
            )
            self.logger.info("[Image] Thumbnail de imagen cargado correctamente")
        except Exception:
            self.logger.warning("[Image] Timeout esperando thumbnail — continuando igual")
            self.human_wait(3, 5)

    def _dismiss_link_preview(self) -> None:
        """Cierra la previsualización de link que Facebook genera automáticamente.

        Cuando el texto contiene una URL, Facebook agrega un preview card al compositor.
        Si no se elimina, puede interferir con el botón Publicar o generar errores.
        """
        self.human_wait(3, 5)
        try:
            close_btn = self.driver.find_element(
                By.XPATH,
                "//div[@role='dialog']"
                "//div[@aria-label='Eliminar adjunto' "
                "or @aria-label='Remove attachment' "
                "or @aria-label='Quitar']",
            )
            close_btn.click()
            self.logger.info("[Publish] Previsualización de link eliminada")
            self.human_wait(1, 2)
        except Exception:
            self.logger.debug("[Publish] Sin previsualización de link — continuando")

    def _save_cookies(self) -> None:
        cookies = self.driver.get_cookies()
        job_store.save_cookies(self.account.email, cookies)
        self.logger.info("[Cookies] Cookies guardadas en DB para %s", self.account.email)

    def _load_cookies(self) -> bool:
        """Carga cookies desde DB por email. Devuelve True si las encontró y cargó.
        Nunca levanta excepción — cualquier error retorna False."""
        self.logger.info("[Cookies] Buscando cookies en DB para %s", self.account.email)
        cookies = job_store.load_cookies(self.account.email)
        if cookies is None:
            self.logger.info("[Cookies] No hay cookies guardadas — se hará login normal")
            return False
        try:
            self.logger.info("[Cookies] Cookies encontradas. Navegando a facebook.com para inyectar ...")
            self.driver.get("https://www.facebook.com/")
            self.logger.info("[Cookies] URL actual tras navegar: %s", self.driver.current_url)
            self.human_wait(1, 2)
            self.logger.info("[Cookies] Inyectando %d cookies ...", len(cookies))
            ok = 0
            for cookie in cookies:
                cookie.pop("sameSite", None)
                try:
                    self.driver.add_cookie(cookie)
                    ok += 1
                except Exception:
                    pass
            self.logger.info("[Cookies] %d/%d cookies inyectadas", ok, len(cookies))
            return True
        except Exception:
            self.logger.warning(
                "[Cookies] Error al cargar cookies para %s — borrando de DB y haciendo login normal",
                self.account.email, exc_info=True,
            )
            job_store.delete_cookies(self.account.email)
            return False

    def _is_logged_in(self) -> bool:
        """Comprueba si la sesión está activa probando múltiples selectores."""
        if "/login" in self.driver.current_url:
            return False
        for xpath in [
            "//div[@role='navigation']",
            "//div[@role='banner']",
            "//div[@aria-label='Facebook']",
            "//div[@data-pagelet='LeftRail']",
        ]:
            try:
                WebDriverWait(self.driver, 3).until(
                    EC.presence_of_element_located((By.XPATH, xpath))
                )
                return True
            except Exception:
                continue
        return False

    def _detect_challenge(self) -> str:
        """Detecta si Facebook está mostrando un CAPTCHA o checkpoint.

        Returns 'captcha', 'checkpoint', o 'clear'.
        """
        url = self.driver.current_url
        if "/checkpoint/" in url or "/identity/" in url:
            return "checkpoint"
        try:
            self.driver.find_element(By.XPATH, "//iframe[contains(@src,'recaptcha')]")
            return "captcha"
        except Exception:
            pass
        try:
            self.driver.find_element(
                By.XPATH,
                "//*[contains(text(),'Verifica tu identidad') "
                "or contains(text(),'Verify your identity') "
                "or contains(text(),'security check')]",
            )
            return "captcha"
        except Exception:
            pass
        return "clear"

    def _wait_for_manual_resolution(self) -> bool:
        """Pausa hasta que el operador resuelva el CAPTCHA/checkpoint manualmente.

        Polling cada 10s, timeout 60s (6 intentos). Retorna True si se resolvió.
        """
        self._screenshot("captcha_detected.png")
        print(f"\n{'='*60}")
        print(f"  CAPTCHA/CHECKPOINT detectado — cuenta: {self.account.name}")
        print(f"  Resuelve manualmente en el navegador Chrome.")
        print(f"  Tiempo máximo: 60 segundos (verificando cada 10s)")
        print(f"{'='*60}\n")
        self.logger.warning("[CAPTCHA] Esperando resolución manual para %s", self.account.name)

        for attempt in range(1, 7):  # 6 × 10s = 60s
            time.sleep(10)
            if self._detect_challenge() == "clear" and self._is_logged_in():
                self.logger.info("[CAPTCHA] Resuelto manualmente en intento %d/6", attempt)
                return True
            self.logger.info("[CAPTCHA] Intento %d/6 — aún pendiente", attempt)

        self.logger.error(
            "[CAPTCHA] Timeout 60s — no se resolvió. Cerrando cuenta %s", self.account.name
        )
        print(f"\n[!] Timeout CAPTCHA — se cierra la cuenta {self.account.name}\n")
        return False

    # ------------------------------------------------------------------ #
    # Login
    # ------------------------------------------------------------------ #
    def login(self) -> bool:
        """Inicia sesión en Facebook. Primero intenta con cookies guardadas."""
        self.logger.info("[Login] ── Iniciando sesión como %s ──", self.account.name)
        try:
            # --- [1] Intentar con cookies guardadas ----------------------
            self.logger.info("[Login] Paso 1/4 — Intentando restaurar sesión desde cookies")
            try:
                if self._load_cookies():
                    self.logger.info("[Login] Cookies cargadas. Refrescando página ...")
                    self.driver.refresh()
                    self.logger.info("[Login] URL tras refresh: %s", self.driver.current_url)
                    self.human_wait(2, 4)
                    self.logger.info("[Login] Verificando si la sesión está activa ...")
                    if self._is_logged_in():
                        self.logger.info(
                            "[Login] ✓ Sesión restaurada desde cookies para %s", self.account.name
                        )
                        job_store.record_login(self.account.name, True)
                        return True
                    self.logger.info("[Login] Cookies cargadas pero sesión inactiva (expiradas) — login normal")
                else:
                    self.logger.info("[Login] Sin cookies válidas — procediendo con login normal")
            except Exception:
                self.logger.warning(
                    "[Login] Fallo en restauración de cookies — continuando con login normal",
                    exc_info=True,
                )

            # --- [2] Login normal ----------------------------------------
            self.logger.info("[Login] Paso 2/4 — Navegando a facebook.com/login ...")
            self.driver.get("https://www.facebook.com/login")
            self.logger.info("[Login] URL actual: %s", self.driver.current_url)
            wait = WebDriverWait(self.driver, 20)

            self.logger.info("[Login] Paso 3/4 — Esperando formulario de login ...")
            email_input = wait.until(
                EC.presence_of_element_located((By.XPATH, "//input[@name='email']"))
            )
            self.logger.info("[Login] Formulario encontrado. Ingresando credenciales ...")
            self.human_wait()
            self._human_click(email_input)
            email_input.clear()
            self._human_type(email_input, self.account.email)

            self.human_wait(0.5, 1.5)

            pass_input = wait.until(
                EC.presence_of_element_located((By.XPATH, "//input[@name='pass']"))
            )
            self._human_click(pass_input)
            pass_input.clear()
            self._human_type(pass_input, self.account.password)

            self.human_wait(0.5, 1.5)
            self.logger.info("[Login] Enviando formulario ...")
            pass_input.send_keys(Keys.RETURN)

            # --- [3] Esperar redirección (con detección de CAPTCHA) ------
            self.logger.info("[Login] Paso 4/4 — Esperando redirección fuera de /login ...")
            deadline = time.monotonic() + 20
            resolved = False
            while time.monotonic() < deadline:
                if "/login" not in self.driver.current_url:
                    resolved = True
                    break
                challenge = self._detect_challenge()
                if challenge != "clear":
                    self.logger.warning("[Login] Desafío detectado: %s", challenge)
                    if not self._wait_for_manual_resolution():
                        job_store.record_login(self.account.name, False)
                        return False
                    resolved = True
                    break
                time.sleep(1)

            if not resolved:
                self.logger.error(
                    "[Login] TIMEOUT sin redirección. URL: %s — "
                    "credenciales incorrectas o CAPTCHA no detectado",
                    self.driver.current_url,
                )
                self._screenshot("login_blocked.png")
                job_store.record_login(self.account.name, False)
                return False

            self.logger.info("[Login] Redirección exitosa. URL actual: %s", self.driver.current_url)
            self.human_wait(2, 4)

            # Cerrar diálogo "Recordar contraseña" si aparece
            for xpath in [
                "//div[@aria-label='Cerrar']",
                "//div[@aria-label='Close']",
                "//span[contains(text(),'Ahora no')]",
                "//a[contains(text(),'Ahora no')]",
            ]:
                try:
                    self.driver.find_element(By.XPATH, xpath).click()
                    break
                except Exception:
                    pass

            self.human_wait(
                self.config["wait_after_login_min"],
                self.config["wait_after_login_max"],
            )

            # Guardar cookies para la próxima ejecución
            self._save_cookies()

            self.logger.info("Login exitoso para %s", self.account.name)
            job_store.record_login(self.account.name, True)
            return True

        except Exception:
            self.logger.error("Login FAILED for %s", self.account.name, exc_info=True)
            self._screenshot("login_error.png")
            job_store.record_login(self.account.name, False)
            return False

    # ------------------------------------------------------------------ #
    # Navigation
    # ------------------------------------------------------------------ #
    def navigate_to_group(self, group_id: str) -> bool:
        """Navigate to a Facebook group and verify the page loaded."""
        url = f"https://www.facebook.com/groups/{group_id}"
        self.logger.info("Navigating to group %s", group_id)
        try:
            self.driver.get(url)
            self.human_wait(3, 6)

            # Pausa de "lectura" — simula que el usuario mira la página al cargar
            self.driver.execute_script("window.scrollBy({top: random_px, behavior:'smooth'})".replace(
                "random_px", str(random.randint(80, 250))
            ))
            self.human_wait(1, 2)

            # Detectar CAPTCHA/checkpoint antes de verificar contenido
            challenge = self._detect_challenge()
            if challenge != "clear":
                self.logger.warning("[Nav] Desafío detectado al navegar a grupo %s: %s", group_id, challenge)
                if not self._wait_for_manual_resolution():
                    return False

            WebDriverWait(self.driver, 15).until(
                EC.presence_of_element_located(
                    (By.XPATH, "//div[@role='main']")
                )
            )
            self.logger.info("Group %s loaded", group_id)
            return True
        except Exception:
            self.logger.error("Failed to load group %s", group_id, exc_info=True)
            self._screenshot(f"nav_error_{group_id}.png")
            return False

    # ------------------------------------------------------------------ #
    # Publish
    # ------------------------------------------------------------------ #
    def publish(self, group_id: str, text: str, image_path: str | None = None) -> bool:
        """Post *text* (y opcionalmente imagen) to a single group. Returns True on success."""

        for attempt in range(1, self.config["max_retries"] + 1):
            self.logger.info(
                "Publish attempt %d/%d for group %s",
                attempt,
                self.config["max_retries"],
                group_id,
            )
            try:
                if not self.navigate_to_group(group_id):
                    continue

                wait = WebDriverWait(self.driver, 15)

                # --- Abrir compositor -----------------------------------
                composer = self._find_first([
                    "//span[contains(text(),'Escribe algo')]",
                    "//span[contains(text(),'Write something')]",
                    "//span[contains(text(),'¿Qué estás pensando')]",
                    "//span[contains(text(),'What')]",
                    "//div[@role='button'][contains(@aria-label,'post')]",
                    "//div[@data-pagelet='GroupInlineComposer']//div[@role='button']",
                ], timeout=15)
                self._human_click(composer)
                self.human_wait(2, 4)

                # --- Escribir en el editor del modal --------------------
                editor = self._find_first([
                    "//div[@role='dialog']//div[@contenteditable='true']",
                    "//div[@contenteditable='true'][@data-lexical-editor='true']",
                    "//div[@contenteditable='true'][contains(@class,'notranslate')]",
                    "//div[@contenteditable='true']",
                ], timeout=10, condition=EC.presence_of_element_located)
                self._human_click(editor)
                self.human_wait(0.5, 1)
                self._human_type(editor, text)

                # --- Si el texto tiene URL, esperar y cerrar el preview ----
                if any(s in text for s in ("http://", "https://", "www.")):
                    self._dismiss_link_preview()
                else:
                    self.human_wait(1, 2)

                # --- Adjuntar imagen si se proporcionó --------------------
                if image_path:
                    self._attach_image(image_path, wait)

                # --- Click Publicar (submit) ------------------------------
                pub_btn = self._find_first([
                    "//div[@aria-label='Publicar']",
                    "//div[@aria-label='Post']",
                    "//button[@aria-label='Publicar']",
                    "//button[@aria-label='Post']",
                    "//div[@role='dialog']//div[@role='button'][contains(@aria-label,'ublicar')]",
                    "//div[@role='dialog']//div[@role='button'][contains(@aria-label,'ost')]",
                ], timeout=10)
                self.human_wait(0.3, 0.8)
                self._human_click(pub_btn)

                # Wait for the modal to disappear as confirmation
                WebDriverWait(self.driver, 20).until(
                    EC.invisibility_of_element_located(
                        (By.XPATH, "//div[@role='dialog']")
                    )
                )
                self.logger.info(
                    "Published to group %s successfully", group_id
                )
                return True

            except Exception:
                self.logger.error(
                    "Error publishing to group %s (attempt %d)",
                    group_id,
                    attempt,
                    exc_info=True,
                )
                self._screenshot(f"error_{group_id}.png")

        self.logger.error(
            "All %d attempts exhausted for group %s",
            self.config["max_retries"],
            group_id,
        )
        return False

    # ------------------------------------------------------------------ #
    # Publish to all groups
    # ------------------------------------------------------------------ #
    def publish_to_all_groups(self, text: str, image_path: str | None = None) -> dict[str, bool]:
        """Post to every group assigned to this account.

        Returns a dict mapping group_id -> success boolean.
        """
        results: dict[str, bool] = {}

        # Apply per-account text variation if enabled
        if self.config.get("text_variation_mode", False):
            text = _vary_text(text, self.account.name)
            self.logger.debug("Text variation applied for %s", self.account.name)

        groups = self.account.groups[: self.config["max_groups_per_session"]]

        for idx, group_id in enumerate(groups):
            success = self.publish(group_id, text, image_path=image_path)
            results[group_id] = success

            # Wait between groups, but not after the last one
            if idx < len(groups) - 1:
                delay = random.uniform(
                    self.config["wait_between_groups_min"],
                    self.config["wait_between_groups_max"],
                )
                self.logger.info(
                    "Waiting %.0f s before next group …", delay
                )
                time.sleep(delay)

        return results

    # ------------------------------------------------------------------ #
    # Cleanup
    # ------------------------------------------------------------------ #
    def close(self) -> None:
        try:
            self.driver.quit()
            self.logger.info("Browser closed for %s", self.account.name)
        except Exception:
            self.logger.warning(
                "Error closing browser for %s", self.account.name, exc_info=True
            )
