"""
facebook_poster.py — Selenium-based Facebook group poster.

Each FacebookPoster instance is bound to a single AccountConfig and
manages its own WebDriver, logger, and screenshots directory.
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

        # Console handler
        ch = logging.StreamHandler()
        ch.setLevel(logging.INFO)
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
    def _build_driver(self) -> webdriver.Chrome:
        opts = Options()

        # Anti-detection basics
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
        opts.add_argument("--lang=es")  # match Spanish UI XPaths

        # Service — prefer explicit path, then webdriver-manager, then PATH
        chromedriver_path = os.getenv("CHROMEDRIVER_PATH", "").strip()
        if chromedriver_path:
            service = Service(executable_path=chromedriver_path)
        elif ChromeDriverManager is not None:
            service = Service(ChromeDriverManager().install())
        else:
            service = Service()  # rely on PATH

        driver = webdriver.Chrome(service=service, options=opts)

        # Remove navigator.webdriver flag
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
        self.logger.info("WebDriver initialised for %s", self.account.name)
        return driver

    # ------------------------------------------------------------------ #
    # Helpers
    # ------------------------------------------------------------------ #
    def human_wait(self, min_s: float = 1, max_s: float = 3) -> None:
        time.sleep(random.uniform(min_s, max_s))

    def _screenshot(self, filename: str) -> None:
        path = os.path.join(self.account.screenshots_dir, filename)
        try:
            self.driver.save_screenshot(path)
            self.logger.info("Screenshot saved: %s", path)
        except Exception:
            self.logger.warning("Failed to save screenshot %s", path, exc_info=True)

    # ------------------------------------------------------------------ #
    # Login
    # ------------------------------------------------------------------ #
    def login(self) -> bool:
        """Log into Facebook. Returns True on success."""
        self.logger.info("Logging in as %s …", self.account.name)
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

            pass_input.send_keys(Keys.RETURN)

            # Esperar a que desaparezca la página de login
            wait.until(
                lambda d: "/login" not in d.current_url
            )

            self.human_wait(2, 4)

            # Cerrar diálogo "Recordar contraseña" si aparece
            try:
                dismiss_btn = WebDriverWait(self.driver, 5).until(
                    EC.element_to_be_clickable(
                        (By.XPATH, "//div[@aria-label='Cerrar' or @aria-label='Close']")
                    )
                )
                dismiss_btn.click()
                self.logger.info("Diálogo 'Recordar contraseña' cerrado")
            except Exception:
                # Intentar con "Ahora no"
                try:
                    ahora_no = self.driver.find_element(
                        By.XPATH, "//a[contains(text(),'Ahora no')] | //span[contains(text(),'Ahora no')]"
                    )
                    ahora_no.click()
                    self.logger.info("Clic en 'Ahora no'")
                except Exception:
                    pass  # No apareció el diálogo, continuar

            self.human_wait(
                self.config["wait_after_login_min"],
                self.config["wait_after_login_max"],
            )
            self.logger.info("Login exitoso para %s", self.account.name)
            return True

        except Exception:
            self.logger.error(
                "Login FAILED for %s", self.account.name, exc_info=True
            )
            self._screenshot("login_error.png")
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

            # Verify we actually reached a group page (look for composer area)
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
    def publish(self, group_id: str, text: str) -> bool:
        """Post *text* to a single group. Returns True on success."""

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

                # --- Abrir compositor: clic en "Escribe algo..." -----------
                composer = wait.until(
                    EC.element_to_be_clickable(
                        (By.XPATH,
                         "//span[contains(text(),'Escribe algo')] "
                         "| //span[contains(text(),'Write something')] "
                         "| //span[contains(text(),'¿Qué estás pensando')]")
                    )
                )
                composer.click()
                self.human_wait(2, 4)

                # --- Escribir en el editor del modal ----------------------
                editor = wait.until(
                    EC.presence_of_element_located(
                        (By.XPATH,
                         "//div[@role='dialog']//div[@contenteditable='true']")
                    )
                )
                editor.click()
                self.human_wait(0.5, 1)
                editor.send_keys(text)
                self.human_wait(1, 2)

                # --- Click Publicar (submit) ------------------------------
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
    def publish_to_all_groups(self, text: str) -> dict[str, bool]:
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
            success = self.publish(group_id, text)
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
