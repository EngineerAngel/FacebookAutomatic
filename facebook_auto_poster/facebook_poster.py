"""
facebook_poster.py — Patchright + Emunium based Facebook group poster.

Each FacebookPoster instance is bound to a single AccountConfig and
manages its own Playwright lifecycle (Playwright → Browser → Context → Page),
logger, and screenshots directory.

Anti-detection stack:
- Patchright: binario Chromium parcheado (oculta navigator.webdriver y otros
  fingerprints a nivel bajo). Reemplaza los CDP tricks manuales de Selenium.
- Emunium (standalone): movimientos de mouse y teclado a nivel OS con curvas
  de Bézier reales. Se usa solo para clics; el tipeo va por page.keyboard
  para no depender del foco de ventana del OS.
"""

import logging
import os
import random
import time
from pathlib import Path

from patchright.sync_api import (
    Browser,
    BrowserContext,
    Page,
    TimeoutError as PatchrightTimeout,
    sync_playwright,
)

try:
    from emunium import Emunium
    _HAS_EMUNIUM = True
except ImportError:
    Emunium = None  # type: ignore[assignment,misc]
    _HAS_EMUNIUM = False

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
# Constantes de la UI de Chrome (para sincronizar coordenadas con Emunium)
# ---------------------------------------------------------------------------
# Offset vertical aprox desde el borde superior de la ventana al viewport en Windows
# (title bar + tab strip + address bar). Ajustable si hace falta.
_CHROME_UI_Y_OFFSET = 85


# ---------------------------------------------------------------------------
# FacebookPoster
# ---------------------------------------------------------------------------
class FacebookPoster:
    """Drives a single Facebook session for one account using Patchright."""

    # Pool de "palabras fantasma" para simular un tipeo humano con errores.
    _FAKE_WORDS = ("aaa", "zzz", "hmm", "err")
    _TYPO_ALPHABET = "abcdefghijklmnopqrstuvwxyz"

    def __init__(self, account: AccountConfig, config: dict) -> None:
        self.account = account
        self.config = config

        # -- per-account logger ------------------------------------------
        self.logger = logging.getLogger(f"poster.{account.name}")
        self.logger.setLevel(logging.DEBUG)

        os.makedirs(os.path.dirname(account.log_file), exist_ok=True)
        fh = logging.FileHandler(account.log_file, encoding="utf-8")
        fh.setLevel(logging.DEBUG)
        fh.setFormatter(
            logging.Formatter("%(asctime)s - [%(name)s] - %(levelname)s - %(message)s")
        )
        self.logger.addHandler(fh)

        ch = logging.StreamHandler()
        ch.setLevel(logging.DEBUG)
        ch.setFormatter(
            logging.Formatter("%(asctime)s - [%(name)s] - %(levelname)s - %(message)s")
        )
        self.logger.addHandler(ch)

        # -- screenshots dir ---------------------------------------------
        Path(account.screenshots_dir).mkdir(parents=True, exist_ok=True)

        # -- Runtime flags (per-account scope) ---------------------------
        self._banned: bool = False
        self._publish_count: int = 0

        # -- Window offsets for Emunium screen-coord translation ---------
        pos_x, pos_y = config.get("browser_window_position", (0, 0))
        self._window_x_offset = pos_x
        self._window_y_offset = pos_y + _CHROME_UI_Y_OFFSET

        # -- Playwright lifecycle ----------------------------------------
        self._pw = sync_playwright().start()
        self.browser, self.context, self.page = self._build_browser()

        # -- Emunium (standalone, opera en coords OS) --------------------
        self._em = None
        if config.get("emunium_enabled", True) and _HAS_EMUNIUM and not config["browser_headless"]:
            try:
                self._em = Emunium()
                self.logger.info("[Emunium] Activo (coords OS: offset=%d,%d)",
                                 self._window_x_offset, self._window_y_offset)
            except Exception:
                self.logger.warning("[Emunium] Error inicializando — fallback a clicks Patchright", exc_info=True)
                self._em = None

    # ------------------------------------------------------------------ #
    # Browser setup
    # ------------------------------------------------------------------ #
    def _build_browser(self) -> tuple[Browser, BrowserContext, Page]:
        self.logger.info("[Driver] Construyendo browser Patchright para %s ...", self.account.name)

        w, h = self.config["browser_window_size"]
        pos_x, pos_y = self.config.get("browser_window_position", (0, 0))
        headless = bool(self.config["browser_headless"])

        args = [
            f"--window-position={pos_x},{pos_y}",
            f"--window-size={w},{h}",
            "--disable-notifications",
            "--lang=es",
        ]

        if headless:
            self.logger.info("[Driver] Modo headless activado")

        user_agent = (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        )

        try:
            browser = self._pw.chromium.launch(headless=headless, args=args)
        except Exception:
            self.logger.error("[Driver] FALLO al lanzar Chromium parcheado", exc_info=True)
            raise

        context = browser.new_context(
            user_agent=user_agent,
            viewport={"width": w, "height": h},
            locale="es-ES",
        )
        # Timeout por defecto (ms) para operaciones que no lo especifiquen
        context.set_default_timeout(self.config.get("implicit_wait", 10) * 1000)

        page = context.new_page()
        self.logger.info("[Driver] Patchright listo. URL inicial: %s", page.url or "(blank)")
        return browser, context, page

    # ------------------------------------------------------------------ #
    # Helpers
    # ------------------------------------------------------------------ #
    def human_wait(self, min_s: float = 1, max_s: float = 3) -> None:
        time.sleep(random.uniform(min_s, max_s))

    def _human_type(self, locator, text: str) -> None:
        """Tipea con delays humanos + typos falsos + palabras fantasma.

        Asume que el locator ya está enfocado (hacer _human_click antes).
        Usa page.keyboard en vez de emunium.type_text para no depender del
        foco de ventana del OS.
        """
        try:
            locator.click(timeout=5000)
        except Exception:
            # Si no podemos clickear, intentamos enfocar igual
            try:
                locator.focus(timeout=3000)
            except Exception:
                pass

        kb = self.page.keyboard
        chars_since_fake_word = 0

        for char in text:
            # Palabra fantasma en fronteras de espacio (cada ~10 chars)
            if (
                chars_since_fake_word >= 10
                and char == " "
                and random.random() < 0.05
            ):
                fake = random.choice(self._FAKE_WORDS)
                for fc in fake:
                    kb.type(fc)
                    time.sleep(random.uniform(0.05, 0.15))
                time.sleep(random.uniform(0.30, 0.60))
                for _ in fake:
                    kb.press("Backspace")
                    time.sleep(random.uniform(0.04, 0.10))
                time.sleep(random.uniform(0.10, 0.25))
                chars_since_fake_word = 0

            # Typo puntual: 5% chance de char incorrecto + corrección
            if char != " " and random.random() < 0.05:
                wrong = random.choice(self._TYPO_ALPHABET)
                kb.type(wrong)
                time.sleep(random.uniform(0.15, 0.35))
                kb.press("Backspace")
                time.sleep(random.uniform(0.08, 0.18))

            kb.type(char)
            if char == " ":
                time.sleep(random.uniform(0.10, 0.30))
            else:
                time.sleep(random.uniform(0.05, 0.18))
            if random.random() < 0.05:
                time.sleep(random.uniform(0.30, 0.80))
            chars_since_fake_word += 1

    def _human_click(self, locator) -> None:
        """Mueve el mouse a un locator con curva Bézier vía Emunium y clickea.

        Si Emunium no está activo (headless o falló init), usa el click nativo
        de Patchright que ya hace auto-wait de actionability.
        """
        try:
            locator.scroll_into_view_if_needed(timeout=5000)
        except Exception:
            pass
        time.sleep(random.uniform(0.30, 0.70))

        if self._em is not None:
            try:
                box = locator.bounding_box(timeout=5000)
                if box:
                    center = {
                        "x": int(box["x"] + box["width"] / 2) + self._window_x_offset,
                        "y": int(box["y"] + box["height"] / 2) + self._window_y_offset,
                    }
                    self._em.move_to(center)
                    time.sleep(random.uniform(0.10, 0.35))
                    self._em.click_at(center)
                    return
            except Exception:
                self.logger.debug("[Emunium] click falló, fallback a Patchright click", exc_info=True)

        # Fallback: Patchright click (auto-wait actionability + pequeño offset)
        try:
            locator.click(
                timeout=10000,
                position={
                    "x": random.randint(5, 20),
                    "y": random.randint(5, 20),
                },
            )
        except Exception:
            locator.click(timeout=10000, force=True)

    def _find_first(self, selectors: list[str], timeout: float = 10.0, visible: bool = True):
        """Intenta cada XPath selector en orden, devuelve el primer locator listo.

        Comparte el timeout total entre los selectores como en la versión
        Selenium. Retorna el locator ya esperado (listo para click/type).
        """
        per_timeout = max(round(timeout / len(selectors), 1), 2.0)
        last_exc: Exception = TimeoutError("No selector matched")
        state = "visible" if visible else "attached"
        for xpath in selectors:
            try:
                loc = self.page.locator(xpath).first
                loc.wait_for(state=state, timeout=int(per_timeout * 1000))
                return loc
            except Exception as exc:
                last_exc = exc
                self.logger.debug("[Selector] no encontrado: %s", xpath[:70])
        raise last_exc

    def _screenshot(self, filename: str) -> None:
        path = os.path.join(self.account.screenshots_dir, filename)
        try:
            self.page.screenshot(path=path)
            self.logger.info("Screenshot saved: %s", path)
        except Exception:
            self.logger.warning("Failed to save screenshot %s", path, exc_info=True)

    def _attach_image(self, image_path: str) -> None:
        """Adjunta imagen al compositor usando el input file oculto.

        Patchright maneja inputs ocultos nativamente — no hace falta el
        display:block via JS como en Selenium.
        """
        abs_path = os.path.abspath(image_path)

        self.page.set_input_files("//input[@type='file']", abs_path, timeout=15000)
        self.logger.info("[Image] Archivo enviado al input: %s", abs_path)

        # Esperar thumbnail
        try:
            self.page.locator(
                "//div[@role='dialog']//img[contains(@src,'blob:') "
                "or contains(@src,'scontent')]"
            ).first.wait_for(state="visible", timeout=15000)
            self.logger.info("[Image] Thumbnail de imagen cargado correctamente")
        except Exception:
            self.logger.warning("[Image] Timeout esperando thumbnail — continuando igual")
            self.human_wait(3, 5)

    def _dismiss_link_preview(self) -> None:
        """Cierra la previsualización de link que Facebook genera automáticamente."""
        self.human_wait(3, 5)
        try:
            close_btn = self.page.locator(
                "//div[@role='dialog']"
                "//div[@aria-label='Eliminar adjunto' "
                "or @aria-label='Remove attachment' "
                "or @aria-label='Quitar']"
            ).first
            close_btn.click(timeout=3000)
            self.logger.info("[Publish] Previsualización de link eliminada")
            self.human_wait(1, 2)
        except Exception:
            self.logger.debug("[Publish] Sin previsualización de link — continuando")

    def _save_cookies(self) -> None:
        cookies = self.context.cookies()
        job_store.save_cookies(self.account.email, cookies)
        self.logger.info("[Cookies] Cookies guardadas en DB para %s (%d cookies)",
                         self.account.email, len(cookies))

    def _normalize_cookies(self, cookies: list[dict]) -> list[dict]:
        """Normaliza cookies guardadas (ya sea formato Selenium o Playwright)
        al formato esperado por context.add_cookies()."""
        out = []
        for raw in cookies:
            c = dict(raw)
            # Selenium usaba 'expiry', Playwright usa 'expires'
            if "expiry" in c and "expires" not in c:
                c["expires"] = c.pop("expiry")
            if "expires" in c:
                try:
                    c["expires"] = float(c["expires"])
                except (TypeError, ValueError):
                    c.pop("expires", None)
            # sameSite 'None' solo es válido con secure=True
            ss = c.get("sameSite")
            if isinstance(ss, str):
                ss_norm = ss.capitalize() if ss.lower() in ("lax", "strict", "none") else None
                if ss_norm is None:
                    c.pop("sameSite", None)
                elif ss_norm == "None" and not c.get("secure"):
                    c.pop("sameSite", None)
                else:
                    c["sameSite"] = ss_norm
            # Requeridos: name + value
            if not c.get("name") or "value" not in c:
                continue
            # Requerido: domain o url
            if not c.get("domain") and not c.get("url"):
                c["url"] = "https://www.facebook.com/"
            out.append(c)
        return out

    def _load_cookies(self) -> bool:
        """Carga cookies desde DB por email. Devuelve True si las encontró y cargó."""
        self.logger.info("[Cookies] Buscando cookies en DB para %s", self.account.email)
        cookies = job_store.load_cookies(self.account.email)
        if cookies is None:
            self.logger.info("[Cookies] No hay cookies guardadas — se hará login normal")
            return False
        try:
            self.logger.info("[Cookies] Cookies encontradas. Navegando a facebook.com para inyectar ...")
            self.page.goto("https://www.facebook.com/", timeout=30000)
            self.logger.info("[Cookies] URL actual tras navegar: %s", self.page.url)
            self.human_wait(1, 2)
            normalized = self._normalize_cookies(cookies)
            self.logger.info("[Cookies] Inyectando %d cookies ...", len(normalized))
            self.context.add_cookies(normalized)
            self.logger.info("[Cookies] %d/%d cookies inyectadas", len(normalized), len(cookies))
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
        if "/login" in self.page.url:
            return False
        for xpath in [
            "//div[@role='navigation']",
            "//div[@role='banner']",
            "//div[@aria-label='Facebook']",
            "//div[@data-pagelet='LeftRail']",
        ]:
            try:
                self.page.locator(xpath).first.wait_for(state="attached", timeout=3000)
                return True
            except Exception:
                continue
        return False

    def _detect_challenge(self) -> str:
        """Detecta si Facebook está mostrando CAPTCHA, checkpoint o soft-ban.

        Returns 'captcha', 'checkpoint', 'banned', o 'clear'.
        """
        url = self.page.url
        if "/checkpoint/" in url or "/identity/" in url:
            return "checkpoint"
        try:
            if self.page.locator("//iframe[contains(@src,'recaptcha')]").count() > 0:
                return "captcha"
        except Exception:
            pass
        try:
            if self.page.locator(
                "//*[contains(text(),'Verifica tu identidad') "
                "or contains(text(),'Verify your identity') "
                "or contains(text(),'security check')]"
            ).count() > 0:
                return "captcha"
        except Exception:
            pass
        # Soft-ban: Facebook bloqueó temporalmente la acción o la cuenta.
        try:
            ban_xpath = (
                "//*[contains(translate(text(),"
                "'ABCDEFGHIJKLMNOPQRSTUVWXYZÁÉÍÓÚÑ',"
                "'abcdefghijklmnopqrstuvwxyzáéíóúñ'),"
                "'temporarily blocked') "
                "or contains(translate(text(),"
                "'ABCDEFGHIJKLMNOPQRSTUVWXYZÁÉÍÓÚÑ',"
                "'abcdefghijklmnopqrstuvwxyzáéíóúñ'),"
                "'temporalmente bloqueado') "
                "or contains(translate(text(),"
                "'ABCDEFGHIJKLMNOPQRSTUVWXYZÁÉÍÓÚÑ',"
                "'abcdefghijklmnopqrstuvwxyzáéíóúñ'),"
                "'action blocked') "
                "or contains(translate(text(),"
                "'ABCDEFGHIJKLMNOPQRSTUVWXYZÁÉÍÓÚÑ',"
                "'abcdefghijklmnopqrstuvwxyzáéíóúñ'),"
                "'acción bloqueada') "
                "or contains(translate(text(),"
                "'ABCDEFGHIJKLMNOPQRSTUVWXYZÁÉÍÓÚÑ',"
                "'abcdefghijklmnopqrstuvwxyzáéíóúñ'),"
                "'we suspended your account') "
                "or contains(translate(text(),"
                "'ABCDEFGHIJKLMNOPQRSTUVWXYZÁÉÍÓÚÑ',"
                "'abcdefghijklmnopqrstuvwxyzáéíóúñ'),"
                "'suspendimos tu cuenta') "
                "or contains(translate(text(),"
                "'ABCDEFGHIJKLMNOPQRSTUVWXYZÁÉÍÓÚÑ',"
                "'abcdefghijklmnopqrstuvwxyzáéíóúñ'),"
                "\"you can't use this feature\") "
                "or contains(translate(text(),"
                "'ABCDEFGHIJKLMNOPQRSTUVWXYZÁÉÍÓÚÑ',"
                "'abcdefghijklmnopqrstuvwxyzáéíóúñ'),"
                "'no puedes usar esta función')]"
            )
            if self.page.locator(ban_xpath).count() > 0:
                return "banned"
        except Exception:
            pass
        return "clear"

    def _handle_banned(self, context: str) -> None:
        """Registra y toma screenshot cuando se detecta un soft-ban."""
        self.logger.critical(
            "[BANNED] Soft-ban detectado en %s para cuenta %s — URL: %s",
            context, self.account.name, self.page.url,
        )
        print(f"\n[!] SOFT-BAN detectado en cuenta {self.account.name} "
              f"({context}) — abortando retries.\n")
        self._screenshot(f"banned_{self.account.name}.png")

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

    def _maybe_refresh_session(self) -> None:
        """Cada N publicaciones exitosas, navega al home y hace pausa larga."""
        n = self.config.get("refresh_every_n_posts", 0)
        if n <= 0 or self._publish_count == 0 or self._publish_count % n != 0:
            return
        pause = random.uniform(
            self.config["refresh_pause_min"],
            self.config["refresh_pause_max"],
        )
        self.logger.info(
            "[Refresh] %d publicaciones alcanzadas — refrescando sesión y pausando %.0f s",
            self._publish_count, pause,
        )
        try:
            self.page.goto("https://www.facebook.com/", timeout=30000)
            self.human_wait(2, 4)
            self.page.evaluate(
                f"window.scrollBy({{top: {random.randint(150, 500)}, behavior:'smooth'}})"
            )
        except Exception:
            self.logger.warning("[Refresh] Error navegando al home — continuando con pausa", exc_info=True)
        time.sleep(pause)

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
                    self.page.reload(timeout=30000)
                    self.logger.info("[Login] URL tras refresh: %s", self.page.url)
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
            self.page.goto("https://www.facebook.com/login", timeout=30000)
            self.logger.info("[Login] URL actual: %s", self.page.url)

            self.logger.info("[Login] Paso 3/4 — Esperando formulario de login ...")
            email_input = self.page.locator("//input[@name='email']").first
            email_input.wait_for(state="visible", timeout=20000)
            self.logger.info("[Login] Formulario encontrado. Ingresando credenciales ...")
            self.human_wait()
            self._human_click(email_input)
            email_input.fill("")
            self._human_type(email_input, self.account.email)

            self.human_wait(0.5, 1.5)

            pass_input = self.page.locator("//input[@name='pass']").first
            pass_input.wait_for(state="visible", timeout=20000)
            self._human_click(pass_input)
            pass_input.fill("")
            self._human_type(pass_input, self.account.password)

            self.human_wait(0.5, 1.5)
            self.logger.info("[Login] Enviando formulario ...")
            pass_input.press("Enter")

            # --- [3] Esperar redirección (con detección de CAPTCHA/ban) --
            self.logger.info("[Login] Paso 4/4 — Esperando redirección fuera de /login ...")
            deadline = time.monotonic() + 20
            resolved = False
            while time.monotonic() < deadline:
                if "/login" not in self.page.url:
                    resolved = True
                    break
                challenge = self._detect_challenge()
                if challenge == "banned":
                    self._banned = True
                    self._handle_banned("login")
                    job_store.record_login(self.account.name, False)
                    return False
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
                    self.page.url,
                )
                self._screenshot("login_blocked.png")
                job_store.record_login(self.account.name, False)
                return False

            self.logger.info("[Login] Redirección exitosa. URL actual: %s", self.page.url)
            self.human_wait(2, 4)

            # Cerrar diálogo "Recordar contraseña" si aparece
            for xpath in [
                "//div[@aria-label='Cerrar']",
                "//div[@aria-label='Close']",
                "//span[contains(text(),'Ahora no')]",
                "//a[contains(text(),'Ahora no')]",
            ]:
                try:
                    self.page.locator(xpath).first.click(timeout=2000)
                    break
                except Exception:
                    pass

            self.human_wait(
                self.config["wait_after_login_min"],
                self.config["wait_after_login_max"],
            )

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
    # Setup interactivo (para setup_accounts.py)
    # ------------------------------------------------------------------ #
    def setup_interactive(self) -> bool:
        """Flujo interactivo: intenta restaurar cookies, si no hace login manual.

        El usuario resuelve CAPTCHA si aparece y presiona ENTER. Al final
        guarda las cookies en DB.
        """
        self.logger.info("=" * 50)
        self.logger.info("Configurando cuenta: %s", self.account.name)
        self.logger.info("=" * 50)

        try:
            self.page.goto("https://www.facebook.com/login", timeout=30000)
            self.human_wait(2, 3)

            if self._load_cookies():
                self.page.reload(timeout=30000)
                self.human_wait(3, 5)
                if self._is_logged_in():
                    self.logger.info(">>> %s ya tiene sesión activa (cookies válidas)", self.account.name)
                    return True

            self.page.goto("https://www.facebook.com/login", timeout=30000)

            email_input = self.page.locator("//input[@name='email']").first
            email_input.wait_for(state="visible", timeout=20000)
            email_input.fill("")
            email_input.fill(self.account.email)
            self.human_wait(0.5, 1)

            pass_input = self.page.locator("//input[@name='pass']").first
            pass_input.wait_for(state="visible", timeout=20000)
            pass_input.fill("")
            pass_input.fill(self.account.password)
            self.human_wait(0.5, 1)
            pass_input.press("Enter")

            print(f"\n{'='*50}")
            print(f"  CUENTA: {self.account.name}")
            print(f"{'='*50}")
            print(f"  Si aparece un CAPTCHA, resuélvelo manualmente.")
            print(f"  Cuando veas el feed de Facebook (página de inicio),")
            print(f"  presiona ENTER aquí para guardar las cookies.")
            print(f"{'='*50}\n")

            input("  >>> Presiona ENTER cuando estés en el feed de inicio: ")

            if self._is_logged_in():
                self._save_cookies()
                self.logger.info(">>> Cookies guardadas para %s", self.account.name)
                return True

            self.logger.error(">>> No se detectó sesión activa para %s", self.account.name)
            self._screenshot("setup_error.png")
            return False

        except Exception:
            self.logger.error("Error configurando %s", self.account.name, exc_info=True)
            return False

    # ------------------------------------------------------------------ #
    # Navigation
    # ------------------------------------------------------------------ #
    def navigate_to_group(self, group_id: str) -> bool:
        """Navigate to a Facebook group and verify the page loaded."""
        url = f"https://www.facebook.com/groups/{group_id}"
        self.logger.info("Navigating to group %s", group_id)
        try:
            self.page.goto(url, timeout=30000)
            self.human_wait(3, 6)

            # Pausa de "lectura" — simula que el usuario mira la página al cargar
            self.page.evaluate(
                f"window.scrollBy({{top: {random.randint(80, 250)}, behavior:'smooth'}})"
            )
            self.human_wait(1, 2)

            # Detectar CAPTCHA/checkpoint/ban antes de verificar contenido
            challenge = self._detect_challenge()
            if challenge == "banned":
                self._banned = True
                self._handle_banned(f"navigate_to_group({group_id})")
                return False
            if challenge != "clear":
                self.logger.warning("[Nav] Desafío detectado al navegar a grupo %s: %s", group_id, challenge)
                if not self._wait_for_manual_resolution():
                    return False

            self.page.locator("//div[@role='main']").first.wait_for(state="attached", timeout=15000)
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
            if self._banned:
                self.logger.warning(
                    "[BANNED] Cuenta %s marcada como baneada — "
                    "abortando publish() en grupo %s (intento %d)",
                    self.account.name, group_id, attempt,
                )
                return False
            self.logger.info(
                "Publish attempt %d/%d for group %s",
                attempt,
                self.config["max_retries"],
                group_id,
            )
            try:
                if not self.navigate_to_group(group_id):
                    if self._banned:
                        return False
                    continue

                # --- Abrir compositor del grupo (NO campo de comentarios) ---
                self.logger.info("[Publish] Buscando compositor del grupo...")
                composer = self._find_first([
                    # Placeholder visible en el compositor (confirmado en screenshot)
                    "//span[text()='Escribe algo...']",
                    "//span[text()='Write something...']",
                    "//span[contains(text(),'Escribe algo')]",
                    "//span[contains(text(),'Write something')]",
                    # aria-label del div compositor
                    "//div[@aria-label='Escribe algo...']",
                    "//div[@aria-label='Write something...']",
                    # Compositor via data-pagelet (algunos grupos)
                    "//div[@data-pagelet='GroupInlineComposer']//div[@role='button']",
                    # Botón Crear publicación
                    "//div[@aria-label='Crear publicación']",
                    "//div[@aria-label='Create post']",
                    "//span[contains(text(),'Crear publicaci')]",
                ], timeout=15)
                self._human_click(composer)
                self.human_wait(2, 4)

                # --- Esperar el modal de publicación (NO comentario) --------
                self.logger.info("[Publish] Esperando modal de publicacion...")
                modal = self._find_first([
                    "//div[@role='dialog'][.//div[@contenteditable='true']][.//div[@aria-label='Publicar' or @aria-label='Post']]",
                    "//div[@role='dialog'][.//div[@contenteditable='true']][.//span[contains(text(),'Crear publicaci')]]",
                    "//div[@role='dialog'][.//div[@contenteditable='true']][.//span[contains(text(),'Create post')]]",
                    "//div[@role='dialog'][.//div[@contenteditable='true']]",
                ], timeout=10)

                # --- Escribir en el editor DENTRO del modal ----------------
                # xpath= prefix requerido para XPath en locator encadenado
                editor = modal.locator("xpath=.//div[@contenteditable='true']").first
                editor.wait_for(state="visible", timeout=5000)
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
                    self._attach_image(image_path)

                # --- Click Publicar (submit) dentro del modal --------------
                pub_btn = self._find_first([
                    "//div[@role='dialog']//div[@aria-label='Publicar']",
                    "//div[@role='dialog']//div[@aria-label='Post']",
                    "//div[@role='dialog']//button[@aria-label='Publicar']",
                    "//div[@role='dialog']//button[@aria-label='Post']",
                    "//div[@role='dialog']//div[@role='button'][contains(@aria-label,'ublicar')]",
                    "//div[@role='dialog']//div[@role='button'][contains(@aria-label,'ost')]",
                ], timeout=10)
                self.human_wait(0.3, 0.8)
                self._human_click(pub_btn)

                # Éxito: esperar que el botón Publicar desaparezca (modal cerrado)
                # NO usar //div[@role='dialog'] porque hay otros dialogs en la página
                try:
                    pub_btn.wait_for(state="detached", timeout=20000)
                except Exception:
                    self.human_wait(4, 6)

                self.logger.info("Published to group %s successfully", group_id)
                self._publish_count += 1
                self._maybe_refresh_session()
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

        if self.config.get("text_variation_mode", False):
            text = _vary_text(text, self.account.name)
            self.logger.debug("Text variation applied for %s", self.account.name)

        groups = self.account.groups[: self.config["max_groups_per_session"]]

        for idx, group_id in enumerate(groups):
            # Idle aleatorio antes de cada grupo — simula distracción humana
            if random.random() < self.config.get("idle_probability", 0.0):
                idle = random.uniform(
                    self.config["idle_min_seconds"],
                    self.config["idle_max_seconds"],
                )
                self.logger.info("[Idle] Pausa aleatoria de %.1f s antes de grupo %s", idle, group_id)
                time.sleep(idle)

            if self._banned:
                self.logger.warning(
                    "[BANNED] Cuenta %s baneada — se saltan grupos restantes (%d pendientes)",
                    self.account.name, len(groups) - idx,
                )
                results[group_id] = False
                for remaining in groups[idx + 1:]:
                    results[remaining] = False
                break

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
            self.context.close()
        except Exception:
            self.logger.debug("Error cerrando context", exc_info=True)
        try:
            self.browser.close()
        except Exception:
            self.logger.debug("Error cerrando browser", exc_info=True)
        try:
            self._pw.stop()
            self.logger.info("Browser closed for %s", self.account.name)
        except Exception:
            self.logger.warning(
                "Error closing Playwright for %s", self.account.name, exc_info=True
            )
