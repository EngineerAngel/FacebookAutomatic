"""
facebook_poster_async.py — Patchright async API + Emunium based Facebook group poster.

Async counterpart of facebook_poster.py. Use as an async context manager:

    async with FacebookPosterAsync(account, config) as poster:
        if await poster.login():
            results = await poster.publish_to_all_groups(text)

Feature flag: CONFIG["use_async_poster"] (default False).
When False, api_server.py continues using FacebookPoster + AccountManager unchanged.

Warmup usa HumanBrowsingAsync (Commit 3). Gemini calls usan asyncio.to_thread.
"""

from __future__ import annotations

import asyncio
import logging
import os
import random
import time
from pathlib import Path
from typing import Optional

from patchright.async_api import (
    BrowserContext,
    Page,
    TimeoutError as PatchrightTimeout,
    async_playwright,
)

try:
    from emunium import Emunium
    _HAS_EMUNIUM = True
except ImportError:
    Emunium = None  # type: ignore[assignment,misc]
    _HAS_EMUNIUM = False

from config import AccountConfig
import job_store
import proxy_manager
import webhook
import metrics
from adaptive_selector import AdaptivePlaywrightBridge
from gemini_commenter import GeminiCommenter
from human_browsing import HumanBrowsingAsync
from logging_config import bind_account, get_formatter, unbind_account
from text_variation import TextVariator

# ---------------------------------------------------------------------------
# Text-variation helpers (same as sync version)
# ---------------------------------------------------------------------------
_PREFIXES = ["", "​", " "]
_SUFFIXES = ["", " ​", ".", " ⁣", "  "]


def _vary_text(text: str, account_name: str) -> str:
    rng = random.Random(account_name)
    return f"{rng.choice(_PREFIXES)}{text}{rng.choice(_SUFFIXES)}"


_CHROME_UI_Y_OFFSET = 85

_BAN_WINDOW_S = 600
_BAN_COOLDOWN_HOURS = 48


# ---------------------------------------------------------------------------
# FacebookPosterAsync
# ---------------------------------------------------------------------------
class FacebookPosterAsync:
    """Drives a single Facebook session for one account using Patchright async API."""

    _TYPO_ALPHABET = "abcdefghijklmnopqrstuvwxyz"

    def __init__(
        self,
        account: AccountConfig,
        config: dict,
        callback_url: str | None = None,
    ) -> None:
        self.account = account
        self.config = config
        self._callback_url = callback_url

        # per-account logger
        self.logger = logging.getLogger(f"poster_async.{account.name}")
        self.logger.setLevel(logging.DEBUG)
        self.logger.propagate = False

        os.makedirs(os.path.dirname(account.log_file), exist_ok=True)
        fh = logging.FileHandler(account.log_file, encoding="utf-8")
        fh.setLevel(logging.DEBUG)
        fh.setFormatter(get_formatter())
        self.logger.addHandler(fh)

        ch = logging.StreamHandler()
        ch.setLevel(logging.DEBUG)
        ch.setFormatter(get_formatter())
        self.logger.addHandler(ch)

        Path(account.screenshots_dir).mkdir(parents=True, exist_ok=True)

        self._banned: bool = False
        self._publish_count: int = 0
        self._ban_detection_times: list[float] = []

        pos_x, pos_y = config.get("browser_window_position", (0, 0))
        self._window_x_offset = pos_x
        self._window_y_offset = pos_y + _CHROME_UI_Y_OFFSET

        # Playwright lifecycle (initialised in _setup)
        self._pw = None
        self.context: BrowserContext | None = None
        self.page: Page | None = None
        self._em: Emunium | None = None  # type: ignore[type-arg]
        self._bridge: AdaptivePlaywrightBridge | None = None

        # Gemini / text variator — same init as sync version
        self._gemini: GeminiCommenter | None = None
        if config.get("gemini_comment_enabled", False):
            api_keys = config.get("gemini_api_keys", [])
            if isinstance(api_keys, str):
                api_keys = [api_keys]
            self._gemini = GeminiCommenter(
                api_keys=api_keys,
                model=config.get("gemini_model", "gemini-2.5-flash"),
                timeout=config.get("gemini_timeout", 15),
                lang=config.get("gemini_comment_lang", "es-MX"),
                logger=self.logger,
            )

        self._text_variator: TextVariator | None = None
        if config.get("text_variation_mode", "off") == "gemini":
            api_keys = config.get("gemini_api_keys", [])
            if isinstance(api_keys, str):
                api_keys = [api_keys]
            _gemini_for_variation = self._gemini or (
                GeminiCommenter(
                    api_keys=api_keys,
                    model=config.get("gemini_model", "gemini-2.5-flash"),
                    timeout=config.get("gemini_timeout", 15),
                    lang=config.get("gemini_comment_lang", "es-MX"),
                    logger=self.logger,
                ) if api_keys else None
            )
            self._text_variator = TextVariator(
                gemini=_gemini_for_variation,
                logger=self.logger,
            )

        self._browsing: HumanBrowsingAsync | None = None
        if config.get("human_browsing_enabled", False):
            self._browsing = HumanBrowsingAsync(
                poster=self,
                config=config,
                gemini=self._gemini,
            )

    # ------------------------------------------------------------------ #
    # Async context manager
    # ------------------------------------------------------------------ #
    async def _setup(self) -> None:
        """Start Playwright and build browser. Called by __aenter__."""
        self._pw = await async_playwright().start()
        self.context, self.page = await self._build_browser()
        self._bridge = AdaptivePlaywrightBridge(self.page)

        if self.config.get("emunium_enabled", True) and _HAS_EMUNIUM and not self.config["browser_headless"]:
            try:
                self._em = await asyncio.to_thread(Emunium)
                self.logger.info(
                    "[Emunium] Activo (coords OS: offset=%d,%d)",
                    self._window_x_offset, self._window_y_offset,
                )
            except Exception:
                self.logger.warning("[Emunium] Error inicializando — fallback a clicks Patchright", exc_info=True)
                self._em = None

    async def __aenter__(self) -> "FacebookPosterAsync":
        await self._setup()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        await self.close()

    # ------------------------------------------------------------------ #
    # Browser setup
    # ------------------------------------------------------------------ #
    def _resolve_user_data_dir(self) -> Path:
        base = Path(__file__).resolve().parent / "browser_profiles"
        base.mkdir(parents=True, exist_ok=True)
        profile_dir = base / self.account.name
        lock_file = profile_dir / ".lock"

        if lock_file.exists():
            corrupt_name = f"{self.account.name}.corrupt.{int(time.time())}"
            corrupt_dir = base / corrupt_name
            self.logger.warning(
                "[Driver] Profile %s tiene .lock stale — renombrando a %s",
                self.account.name, corrupt_name,
            )
            try:
                profile_dir.rename(corrupt_dir)
            except Exception:
                self.logger.exception("[Driver] Error renombrando profile corrupto")

        profile_dir.mkdir(parents=True, exist_ok=True)
        try:
            lock_file.touch(exist_ok=True)
        except Exception:
            pass
        return profile_dir

    async def _migrate_cookies_if_needed(self, context: BrowserContext, profile_dir: Path) -> None:
        marker = profile_dir / ".cookies_migrated"
        if marker.exists():
            return
        try:
            cookies = job_store.load_cookies(self.account.email)
            if cookies:
                await context.add_cookies(cookies)
                self.logger.info(
                    "[Driver] Migradas %d cookies desde DB al profile persistente",
                    len(cookies),
                )
            marker.touch()
        except Exception:
            self.logger.warning("[Driver] Falló migración de cookies", exc_info=True)

    async def _build_browser(self) -> tuple[BrowserContext, Page]:
        fp = self.account.fingerprint
        w, h = fp.get("viewport", self.config["browser_window_size"])
        pos_x, pos_y = self.config.get("browser_window_position", (0, 0))
        headless = bool(self.config["browser_headless"])
        locale = fp.get("locale", "es-MX")

        self.logger.info("[Driver] Construyendo browser async | cuenta=%s fp=%s",
                         self.account.name, fp.get("id", "generico"))

        args = [
            f"--window-position={pos_x},{pos_y}",
            f"--window-size={w},{h}",
            "--disable-notifications",
            f"--lang={locale.split('-')[0]}",
            "--disable-blink-features=AutomationControlled",
        ]

        if headless:
            self.logger.info("[Driver] Modo headless activado")

        user_agent = fp.get("user_agent") or (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/132.0.0.0 Safari/537.36"
        )

        user_data_dir = self._resolve_user_data_dir()

        proxy = proxy_manager.resolve_proxy(self.account.name)
        if proxy:
            self.logger.info("[Driver] Usando proxy %s", proxy["server"])
        else:
            self.logger.warning(
                "[Driver] Sin proxy asignado para '%s' — usando IP directa del servidor",
                self.account.name,
            )

        launch_kwargs: dict = dict(
            user_data_dir=str(user_data_dir),
            headless=headless,
            args=args,
            user_agent=user_agent,
            viewport={"width": w, "height": h},
            locale=locale,
            timezone_id=fp.get("timezone") or self.account.timezone,
            color_scheme=fp.get("color_scheme", "light"),
            extra_http_headers={
                "sec-ch-ua": fp.get("sec_ch_ua", ""),
                "sec-ch-ua-platform": fp.get("sec_ch_ua_platform", '"Windows"'),
                "sec-ch-ua-mobile": "?0",
            },
        )
        if proxy:
            launch_kwargs["proxy"] = proxy

        try:
            context = await self._pw.chromium.launch_persistent_context(**launch_kwargs)
        except Exception:
            self.logger.error("[Driver] FALLO al lanzar Chromium parcheado", exc_info=True)
            raise

        hw = fp.get("hardware_concurrency", 8)
        dm = fp.get("device_memory", 8)
        platform = fp.get("platform", "Win32")
        await context.add_init_script(f"""
            Object.defineProperty(navigator, 'hardwareConcurrency', {{get: () => {hw}}});
            Object.defineProperty(navigator, 'deviceMemory', {{get: () => {dm}}});
            Object.defineProperty(navigator, 'platform', {{get: () => '{platform}'}});
        """)

        context.set_default_timeout(self.config.get("implicit_wait", 10) * 1000)

        await self._migrate_cookies_if_needed(context, user_data_dir)

        page = context.pages[0] if context.pages else await context.new_page()
        self.logger.info("[Driver] Patchright async listo | ua=%s...", user_agent[:50])
        return context, page

    # ------------------------------------------------------------------ #
    # Helpers
    # ------------------------------------------------------------------ #
    async def human_wait(self, min_s: float = 1, max_s: float = 3) -> None:
        await asyncio.sleep(random.uniform(min_s, max_s))

    async def _human_type(self, locator, text: str) -> None:
        try:
            await locator.click(timeout=5000)
        except Exception:
            try:
                await locator.focus(timeout=3000)
            except Exception:
                pass

        kb = self.page.keyboard
        i = 0
        while i < len(text):
            char = text[i]

            if char != " " and random.random() < 0.015:
                n_wrong = random.choices([1, 2, 3], weights=[70, 25, 5])[0]
                wrong_chars = random.choices(self._TYPO_ALPHABET, k=n_wrong)
                for wc in wrong_chars:
                    await kb.type(wc)
                    await asyncio.sleep(random.uniform(0.06, 0.14))
                await asyncio.sleep(random.uniform(0.25, 0.60))
                for _ in range(n_wrong):
                    await kb.press("Backspace")
                    await asyncio.sleep(random.uniform(0.03, 0.07))
                await asyncio.sleep(random.uniform(0.10, 0.25))

            await kb.type(char)

            if char == " ":
                await asyncio.sleep(random.lognormvariate(-1.4, 0.4))
            elif char in ".,;:!?":
                await asyncio.sleep(random.lognormvariate(-1.5, 0.4))
            else:
                await asyncio.sleep(random.lognormvariate(-1.7, 0.35))

            if random.random() < 0.02:
                await asyncio.sleep(random.uniform(0.40, 1.10))

            i += 1

    async def _human_click(self, locator) -> None:
        try:
            await locator.scroll_into_view_if_needed(timeout=5000)
        except Exception:
            pass
        await asyncio.sleep(random.uniform(0.30, 0.70))

        if self._em is not None:
            try:
                box = await locator.bounding_box(timeout=5000)
                if box:
                    center = {
                        "x": int(box["x"] + box["width"] / 2) + self._window_x_offset,
                        "y": int(box["y"] + box["height"] / 2) + self._window_y_offset,
                    }
                    await asyncio.to_thread(self._em.move_to, center)
                    await asyncio.sleep(random.uniform(0.10, 0.35))
                    await asyncio.to_thread(self._em.click_at, center)
                    return
            except Exception:
                self.logger.debug("[Emunium] click falló, fallback a Patchright click", exc_info=True)

        try:
            await locator.click(
                timeout=10000,
                position={
                    "x": random.randint(5, 20),
                    "y": random.randint(5, 20),
                },
            )
        except Exception:
            await locator.click(timeout=10000, force=True)

    async def _find_first(self, selectors: list[str], timeout: float = 10.0, visible: bool = True):
        per_timeout = max(round(timeout / len(selectors), 1), 2.0)
        last_exc: Exception = TimeoutError("No selector matched")
        state = "visible" if visible else "attached"
        for xpath in selectors:
            try:
                loc = self.page.locator(xpath).first
                await loc.wait_for(state=state, timeout=int(per_timeout * 1000))
                return loc
            except Exception as exc:
                last_exc = exc
                self.logger.debug("[Selector] no encontrado: %s", xpath[:70])
        raise last_exc

    async def _screenshot(self, filename: str) -> None:
        path = os.path.join(self.account.screenshots_dir, filename)
        try:
            await self.page.screenshot(path=path)
            self.logger.info("Screenshot saved: %s", path)
        except Exception:
            self.logger.warning("Failed to save screenshot %s", path, exc_info=True)

    async def _attach_image(self, image_paths: list[str]) -> bool:
        """Adjunta 1–N imágenes al compositor. Playwright acepta lista de paths
        en set_files/set_input_files, lo que activa el carrusel multi-foto de FB."""
        if not image_paths:
            return False
        abs_paths = [os.path.abspath(p) for p in image_paths]
        self.logger.info("[Image] Subiendo %d archivo(s)", len(abs_paths))

        photo_btn_loc = self.page.locator(
            "//div[@role='dialog']//div["
            "@aria-label='Foto/video' or @aria-label='Photo/video' "
            "or @aria-label='Agregar fotos/videos' or @aria-label='Add photos/videos' "
            "or @aria-label='Foto/Video']"
        ).first

        file_sent = False

        try:
            await photo_btn_loc.wait_for(state="visible", timeout=5000)
            async with self.page.expect_file_chooser(timeout=5000) as fc_info:
                await self._human_click(photo_btn_loc)
            fc = fc_info.value
            await fc.set_files(abs_paths)
            self.logger.info("[Image] %d archivo(s) enviados via FileChooser", len(abs_paths))
            file_sent = True
        except Exception:
            self.logger.debug("[Image] FileChooser no interceptado — usando set_input_files")

        if not file_sent:
            try:
                file_input = self.page.locator(
                    "//div[@role='dialog']//input[@type='file']"
                ).first
                await file_input.set_input_files(abs_paths, timeout=15000)
                self.logger.info("[Image] %d archivo(s) enviados via set_input_files (dialog)", len(abs_paths))
                file_sent = True
            except Exception:
                pass

        if not file_sent:
            await self.page.set_input_files("//input[@type='file']", abs_paths, timeout=15000)
            self.logger.info("[Image] %d archivo(s) enviados via set_input_files (global)", len(abs_paths))

        for p in abs_paths:
            self.logger.info("[Image] Ruta: %s", p)

        try:
            await self.page.locator(
                "//div[@role='dialog']//div[@role='progressbar']"
            ).first.wait_for(state="visible", timeout=5000)
            self.logger.info("[Image] Upload en progreso...")
            await self.page.locator(
                "//div[@role='dialog']//div[@role='progressbar']"
            ).first.wait_for(state="hidden", timeout=30000)
            self.logger.info("[Image] Upload completado")
        except Exception:
            pass

        thumbnail_selectors = [
            "//div[@role='dialog']//img[contains(@src,'blob:')]",
            "//div[@role='dialog']//img[contains(@src,'scontent')]",
            "//div[@role='dialog']//img[contains(@src,'fbcdn')]",
            "//div[@role='dialog']//div[contains(@style,'blob:')]",
            "//div[@role='dialog']//div[@aria-label and .//img]",
        ]
        for sel in thumbnail_selectors:
            try:
                await self.page.locator(sel).first.wait_for(state="visible", timeout=8000)
                self.logger.info("[Image] Thumbnail confirmado con: %s", sel)
                await self.human_wait(1, 2)
                return True
            except Exception:
                continue

        self.logger.warning("[Image] Thumbnail NO detectado. Guardando diagnóstico...")
        await self._screenshot("image_no_thumbnail.png")
        await self.human_wait(4, 6)
        return False

    async def _check_page_health(self, context: str = "") -> str:
        url = self.page.url
        if "login" in url or "checkpoint" in url:
            self.logger.warning("[Health%s] Página redirigida a login/checkpoint: %s",
                                f":{context}" if context else "", url)
            return "login_required"

        error_indicators = [
            "//div[contains(text(),'algo salió mal') or contains(text(),'something went wrong')]",
            "//span[contains(text(),'algo salió mal') or contains(text(),'something went wrong')]",
            "//div[contains(text(),'Volver a intentar') or contains(text(),'Try again')]",
            "//a[contains(text(),'Volver a cargar') or contains(text(),'Reload')]",
        ]
        for sel in error_indicators:
            try:
                if await self.page.locator(sel).first.is_visible():
                    self.logger.warning("[Health%s] Error detectado en página: %s",
                                        f":{context}" if context else "", sel)
                    await self._screenshot(f"page_error_{context or 'unknown'}.png")
                    return "error"
            except Exception:
                pass

        return "ok"

    async def _dismiss_link_preview(self) -> None:
        await self.human_wait(3, 5)
        try:
            close_btn = self.page.locator(
                "//div[@role='dialog']"
                "//div[@aria-label='Eliminar adjunto' "
                "or @aria-label='Remove attachment' "
                "or @aria-label='Quitar']"
            ).first
            await close_btn.click(timeout=3000)
            self.logger.info("[Publish] Previsualización de link eliminada")
            await self.human_wait(1, 2)
        except Exception:
            self.logger.debug("[Publish] Sin previsualización de link — continuando")

    async def _handle_buy_sell_form(self) -> bool:
        """Detecta si se abrió un formulario de venta (grupos buy/sell) y cambia
        a la pestaña 'Publicación' para poder publicar texto normal.

        Retorna True si se detectó y resolvió el formulario de venta.
        """
        sell_indicators = [
            "//div[@role='dialog']//*[text()='Elige el tipo']",
            "//div[@role='dialog']//*[text()='Choose listing type']",
            "//div[@role='dialog']//*[text()='Precio']",
            "//div[@role='dialog']//*[text()='Price']",
            "//div[@role='dialog']//*[text()='Condición']",
            "//div[@role='dialog']//*[text()='Condition']",
        ]
        for xpath in sell_indicators:
            try:
                el = self.page.locator(xpath).first
                await el.wait_for(state="visible", timeout=1500)
                self.logger.info("[Publish] Formulario de venta detectado — buscando pestaña 'Publicación'")
                break
            except Exception:
                continue
        else:
            return False

        post_tab_selectors = [
            "//div[@role='dialog']//span[text()='Publicación']",
            "//div[@role='dialog']//span[text()='Post']",
            "//div[@role='dialog']//div[@role='tab'][.//span[text()='Publicación']]",
            "//div[@role='dialog']//div[@role='tab'][.//span[text()='Post']]",
        ]
        try:
            tab = await self._find_first(post_tab_selectors, timeout=5)
            await self._human_click(tab)
            await self.human_wait(1, 2)
            self.logger.info("[Publish] Cambiado a pestaña 'Publicación' desde formulario de venta")
            return True
        except Exception:
            self.logger.warning("[Publish] Formulario de venta — no se encontró pestaña 'Publicación'")
            return False

    def _save_cookies(self) -> None:
        cookies = self.context.cookies()
        job_store.save_cookies(self.account.email, cookies)
        self.logger.info("[Cookies] Cookies guardadas en DB para %s (%d cookies)",
                         self.account.email, len(cookies))

    def _normalize_cookies(self, cookies: list[dict]) -> list[dict]:
        out = []
        for raw in cookies:
            c = dict(raw)
            if "expiry" in c and "expires" not in c:
                c["expires"] = c.pop("expiry")
            if "expires" in c:
                try:
                    c["expires"] = float(c["expires"])
                except (TypeError, ValueError):
                    c.pop("expires", None)
            ss = c.get("sameSite")
            if isinstance(ss, str):
                ss_norm = ss.capitalize() if ss.lower() in ("lax", "strict", "none") else None
                if ss_norm is None:
                    c.pop("sameSite", None)
                elif ss_norm == "None" and not c.get("secure"):
                    c.pop("sameSite", None)
                else:
                    c["sameSite"] = ss_norm
            if not c.get("name") or "value" not in c:
                continue
            if not c.get("domain") and not c.get("url"):
                c["url"] = "https://www.facebook.com/"
            out.append(c)
        return out

    async def _load_cookies(self) -> bool:
        self.logger.info("[Cookies] Buscando cookies en DB para %s", self.account.email)
        cookies = job_store.load_cookies(self.account.email)
        if cookies is None:
            self.logger.info("[Cookies] No hay cookies guardadas — se hará login normal")
            return False
        try:
            self.logger.info("[Cookies] Cookies encontradas. Navegando a facebook.com para inyectar ...")
            await self.page.goto("https://www.facebook.com/", timeout=30000)
            self.logger.info("[Cookies] URL actual tras navegar: %s", self.page.url)
            await self.human_wait(1, 2)
            normalized = self._normalize_cookies(cookies)
            self.logger.info("[Cookies] Inyectando %d cookies ...", len(normalized))
            await self.context.add_cookies(normalized)
            self.logger.info("[Cookies] %d/%d cookies inyectadas", len(normalized), len(cookies))
            return True
        except Exception:
            self.logger.warning(
                "[Cookies] Error al cargar cookies para %s — borrando de DB y haciendo login normal",
                self.account.email, exc_info=True,
            )
            job_store.delete_cookies(self.account.email)
            return False

    async def _is_logged_in(self) -> bool:
        if "/login" in self.page.url:
            return False
        for xpath in [
            "//div[@role='navigation']",
            "//div[@role='banner']",
            "//div[@aria-label='Facebook']",
            "//div[@data-pagelet='LeftRail']",
        ]:
            try:
                await self.page.locator(xpath).first.wait_for(state="attached", timeout=3000)
                return True
            except Exception:
                continue
        return False

    async def _detect_challenge(self) -> str:
        url = self.page.url
        if "/checkpoint/" in url or "/identity/" in url:
            return "checkpoint"
        try:
            if await self.page.locator("//iframe[contains(@src,'recaptcha')]").count() > 0:
                return "captcha"
        except Exception:
            pass
        try:
            if await self.page.locator(
                "//*[contains(text(),'Verifica tu identidad') "
                "or contains(text(),'Verify your identity') "
                "or contains(text(),'security check')]"
            ).count() > 0:
                return "captcha"
        except Exception:
            pass
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
            if await self.page.locator(ban_xpath).count() > 0:
                return "banned"
        except Exception:
            pass
        return "clear"

    async def _handle_banned(self, context: str) -> None:
        self.logger.critical(
            "[BANNED] Soft-ban detectado en %s para cuenta %s — URL: %s",
            context, self.account.name, self.page.url,
        )
        print(f"\n[!] SOFT-BAN detectado en cuenta {self.account.name} "
              f"({context}) — abortando retries.\n")

        screenshot_name = f"banned_{self.account.name}_{int(time.time())}.png"
        await self._screenshot(screenshot_name)
        screenshot_path = os.path.join(self.account.screenshots_dir, screenshot_name)

        now = time.time()
        self._ban_detection_times = [
            t for t in self._ban_detection_times if now - t < _BAN_WINDOW_S
        ]
        self._ban_detection_times.append(now)

        if len(self._ban_detection_times) < 2:
            self.logger.warning(
                "[BANNED] Primera detección en ventana de %ds — esperando confirmación "
                "(posible falso positivo)", _BAN_WINDOW_S,
            )
            return

        self.logger.critical(
            "[BANNED] CONFIRMADO (%d detecciones en %ds) — activando cooldown %dh",
            len(self._ban_detection_times), _BAN_WINDOW_S, _BAN_COOLDOWN_HOURS,
        )
        try:
            job_store.record_ban(
                account_name=self.account.name,
                context=context,
                screenshot_path=screenshot_path,
            )
            job_store.set_account_ban_cooldown(
                self.account.name, hours=_BAN_COOLDOWN_HOURS,
            )
        except Exception:
            self.logger.error("[BANNED] Error persistiendo ban en DB", exc_info=True)

        webhook.fire_account_banned(
            url=self._callback_url,
            account_name=self.account.name,
            context=context,
            cooldown_hours=_BAN_COOLDOWN_HOURS,
        )

    async def _wait_for_manual_resolution(self) -> bool:
        await self._screenshot("captcha_detected.png")
        print(f"\n{'='*60}")
        print(f"  CAPTCHA/CHECKPOINT detectado — cuenta: {self.account.name}")
        print(f"  Resuelve manualmente en el navegador Chrome.")
        print(f"  Tiempo máximo: 60 segundos (verificando cada 10s)")
        print(f"{'='*60}\n")
        self.logger.warning("[CAPTCHA] Esperando resolución manual para %s", self.account.name)

        for attempt in range(1, 7):
            await asyncio.sleep(10)
            if await self._detect_challenge() == "clear" and await self._is_logged_in():
                self.logger.info("[CAPTCHA] Resuelto manualmente en intento %d/6", attempt)
                return True
            self.logger.info("[CAPTCHA] Intento %d/6 — aún pendiente", attempt)

        self.logger.error("[CAPTCHA] Timeout 60s — no se resolvió. Cerrando cuenta %s", self.account.name)
        return False

    async def _maybe_refresh_session(self) -> None:
        n = self.config.get("refresh_every_n_posts", 0)
        if n <= 0 or self._publish_count == 0 or self._publish_count % n != 0:
            return
        pause = random.uniform(
            self.config["refresh_pause_min"],
            self.config["refresh_pause_max"],
        )
        self.logger.info(
            "[Refresh] %d publicaciones — refrescando sesión y pausando %.0f s",
            self._publish_count, pause,
        )
        try:
            await self.page.goto("https://www.facebook.com/", timeout=30000)
            await self.human_wait(2, 4)
            await self.page.evaluate(
                f"window.scrollBy({{top: {random.randint(150, 500)}, behavior:'smooth'}})"
            )
        except Exception:
            self.logger.warning("[Refresh] Error navegando al home — pausando igual", exc_info=True)
        await asyncio.sleep(pause)

    # ------------------------------------------------------------------ #
    # Login
    # ------------------------------------------------------------------ #
    async def login(self) -> bool:
        """Inicia sesión en Facebook. Primero intenta con cookies guardadas."""
        bind_account(self.account.name)
        self.logger.info("[Login] ── Iniciando sesión como %s ──", self.account.name)
        try:
            self.logger.info("[Login] Paso 1/4 — Intentando restaurar sesión desde cookies")
            try:
                if await self._load_cookies():
                    self.logger.info("[Login] Cookies cargadas. Refrescando página ...")
                    await self.page.reload(timeout=30000)
                    self.logger.info("[Login] URL tras refresh: %s", self.page.url)
                    await self.human_wait(2, 4)
                    if await self._is_logged_in():
                        self.logger.info("[Login] ✓ Sesión restaurada desde cookies para %s", self.account.name)
                        job_store.record_login(self.account.name, True)
                        metrics.inc_login(self.account.name, True)
                        return True
                    self.logger.info("[Login] Cookies cargadas pero sesión inactiva — login normal")
                else:
                    self.logger.info("[Login] Sin cookies válidas — procediendo con login normal")
            except Exception:
                self.logger.warning("[Login] Fallo en restauración de cookies — continuando", exc_info=True)

            self.logger.info("[Login] Paso 2/4 — Navegando a facebook.com/login ...")
            await self.page.goto("https://www.facebook.com/login", timeout=30000)
            self.logger.info("[Login] URL actual: %s", self.page.url)

            self.logger.info("[Login] Paso 3/4 — Esperando formulario de login ...")
            email_input = await self._bridge.get_locator("login_email", "//input[@name='email']", timeout=20000)
            await email_input.wait_for(state="visible", timeout=20000)
            self.logger.info("[Login] Formulario encontrado. Ingresando credenciales ...")
            await self.human_wait()
            await self._human_click(email_input)
            await email_input.fill("")
            await self._human_type(email_input, self.account.email)

            await self.human_wait(0.5, 1.5)

            pass_input = await self._bridge.get_locator("login_password", "//input[@name='pass']", timeout=20000)
            await pass_input.wait_for(state="visible", timeout=20000)
            await self._human_click(pass_input)
            await pass_input.fill("")
            await self._human_type(pass_input, self.account.password)

            await self.human_wait(0.5, 1.5)
            self.logger.info("[Login] Enviando formulario ...")
            await pass_input.press("Enter")

            self.logger.info("[Login] Paso 4/4 — Esperando redirección fuera de /login ...")
            deadline = asyncio.get_event_loop().time() + 20
            resolved = False
            while asyncio.get_event_loop().time() < deadline:
                if "/login" not in self.page.url:
                    resolved = True
                    break
                challenge = await self._detect_challenge()
                if challenge == "banned":
                    self._banned = True
                    await self._handle_banned("login")
                    job_store.record_login(self.account.name, False)
                    metrics.inc_login(self.account.name, False)
                    return False
                if challenge != "clear":
                    self.logger.warning("[Login] Desafío detectado: %s", challenge)
                    if not await self._wait_for_manual_resolution():
                        job_store.record_login(self.account.name, False)
                        metrics.inc_login(self.account.name, False)
                        return False
                    resolved = True
                    break
                await asyncio.sleep(1)

            if not resolved:
                self.logger.error(
                    "[Login] TIMEOUT sin redirección. URL: %s — "
                    "credenciales incorrectas o CAPTCHA no detectado",
                    self.page.url,
                )
                await self._screenshot("login_blocked.png")
                job_store.record_login(self.account.name, False)
                metrics.inc_login(self.account.name, False)
                return False

            self.logger.info("[Login] Redirección exitosa. URL actual: %s", self.page.url)
            await self.human_wait(2, 4)

            for xpath in [
                "//div[@aria-label='Cerrar']",
                "//div[@aria-label='Close']",
                "//span[contains(text(),'Ahora no')]",
                "//a[contains(text(),'Ahora no')]",
            ]:
                try:
                    await self.page.locator(xpath).first.click(timeout=2000)
                    break
                except Exception:
                    pass

            await self.human_wait(
                self.config["wait_after_login_min"],
                self.config["wait_after_login_max"],
            )

            self._save_cookies()

            self.logger.info("Login exitoso para %s", self.account.name)
            job_store.record_login(self.account.name, True)
            metrics.inc_login(self.account.name, True)
            return True

        except Exception:
            self.logger.error("Login FAILED for %s", self.account.name, exc_info=True)
            await self._screenshot("login_error.png")
            job_store.record_login(self.account.name, False)
            metrics.inc_login(self.account.name, False)
            return False

    # ------------------------------------------------------------------ #
    # Navigation
    # ------------------------------------------------------------------ #
    async def navigate_to_group(self, group_id: str) -> bool:
        url = f"https://www.facebook.com/groups/{group_id}"
        self.logger.info("Navigating to group %s", group_id)
        try:
            await self.page.goto(url, timeout=30000)
            await self.human_wait(3, 6)

            await self.page.evaluate(
                f"window.scrollBy({{top: {random.randint(80, 250)}, behavior:'smooth'}})"
            )
            await self.human_wait(1, 2)

            challenge = await self._detect_challenge()
            if challenge == "banned":
                self._banned = True
                await self._handle_banned(f"navigate_to_group({group_id})")
                return False
            if challenge != "clear":
                self.logger.warning("[Nav] Desafío al navegar a grupo %s: %s", group_id, challenge)
                if not await self._wait_for_manual_resolution():
                    return False

            main_div = await self._bridge.get_locator("group_loaded", "//div[@role='main']", timeout=15000)
            await main_div.wait_for(state="attached", timeout=15000)
            self.logger.info("Group %s loaded", group_id)
            return True
        except Exception:
            self.logger.error("Failed to load group %s", group_id, exc_info=True)
            await self._screenshot(f"nav_error_{group_id}.png")
            return False

    # ------------------------------------------------------------------ #
    # Publish
    # ------------------------------------------------------------------ #
    async def publish(self, group_id: str, text: str, image_paths: list[str] | None = None) -> bool:
        """Post *text* (y opcionalmente imágenes) to a single group. Returns True on success."""
        _t0 = time.monotonic()

        for attempt in range(1, self.config["max_retries"] + 1):
            if self._banned:
                self.logger.warning(
                    "[BANNED] Cuenta %s marcada como baneada — abortando publish() en grupo %s",
                    self.account.name, group_id,
                )
                return False
            self.logger.info(
                "Publish attempt %d/%d for group %s",
                attempt, self.config["max_retries"], group_id,
            )
            try:
                if not await self.navigate_to_group(group_id):
                    if self._banned:
                        return False
                    continue

                if self._browsing and attempt == 1:
                    await self._browsing.warmup_in_group(group_id)

                challenge = await self._detect_challenge()
                if challenge == "banned":
                    self._banned = True
                    await self._handle_banned(f"post_warmup({group_id})")
                    return False
                if challenge != "clear":
                    self.logger.warning("[Publish] Desafío post-warmup en grupo %s: %s", group_id, challenge)
                    if not await self._wait_for_manual_resolution():
                        continue

                health = await self._check_page_health("post_warmup")
                if health != "ok":
                    self.logger.warning("[Publish] Página con estado '%s' post-warmup — reintentando", health)
                    continue

                self.logger.info("[Publish] Buscando compositor del grupo...")
                try:
                    composer = await self._bridge.get_locator(
                        "composer_open", "//div[@aria-label='Crear publicación']"
                    )
                    await composer.wait_for(state="visible", timeout=5000)
                except Exception:
                    composer = await self._find_first([
                        "//span[text()='Escribe algo...']",
                        "//span[text()='Write something...']",
                        "//span[contains(text(),'Escribe algo')]",
                        "//span[contains(text(),'Write something')]",
                        "//div[@aria-label='Escribe algo...']",
                        "//div[@aria-label='Write something...']",
                        "//div[@data-pagelet='GroupInlineComposer']//div[@role='button']",
                        "//div[@aria-label='Crear publicación']",
                        "//div[@aria-label='Create post']",
                        "//span[contains(text(),'Crear publicaci')]",
                    ], timeout=15)
                await self._human_click(composer)
                await self.human_wait(2, 4)
                await self._handle_buy_sell_form()

                self.logger.info("[Publish] Esperando modal de publicacion...")
                modal = await self._find_first([
                    "//div[@role='dialog'][.//div[@contenteditable='true']][.//div[@aria-label='Publicar' or @aria-label='Post']]",
                    "//div[@role='dialog'][.//div[@contenteditable='true']][.//span[contains(text(),'Crear publicaci')]]",
                    "//div[@role='dialog'][.//div[@contenteditable='true']][.//span[contains(text(),'Create post')]]",
                    "//div[@role='dialog'][.//div[@contenteditable='true']]",
                ], timeout=10)

                editor = await self._bridge.get_locator(
                    "composer_editor",
                    "//div[@role='dialog']//div[@contenteditable='true']",
                    timeout=5000,
                )
                await editor.wait_for(state="visible", timeout=5000)
                await self._human_click(editor)
                await self.human_wait(0.5, 1)
                await self._human_type(editor, text)

                if any(s in text for s in ("http://", "https://", "www.")):
                    await self._dismiss_link_preview()
                else:
                    await self.human_wait(1, 2)

                if image_paths:
                    img_ok = await self._attach_image(image_paths)
                    if not img_ok:
                        health = await self._check_page_health("after_image")
                        if health != "ok":
                            self.logger.warning("[Image] Salud de página: %s — reintentando", health)
                            continue
                        self.logger.warning("[Image] Continuando sin confirmación de thumbnail")

                health = await self._check_page_health("before_publish")
                if health != "ok":
                    self.logger.warning("[Publish] Página con estado %s antes de publicar — reintentando", health)
                    continue

                try:
                    pub_btn = await self._bridge.get_locator(
                        "publish_button", "//div[@role='dialog']//div[@aria-label='Publicar']"
                    )
                    await pub_btn.wait_for(state="visible", timeout=5000)
                except Exception:
                    pub_btn = await self._find_first([
                        "//div[@role='dialog']//div[@aria-label='Publicar']",
                        "//div[@role='dialog']//div[@aria-label='Post']",
                        "//div[@role='dialog']//button[@aria-label='Publicar']",
                        "//div[@role='dialog']//button[@aria-label='Post']",
                        "//div[@role='dialog']//div[@role='button'][contains(@aria-label,'ublicar')]",
                        "//div[@role='dialog']//div[@role='button'][contains(@aria-label,'ost')]",
                    ], timeout=10)
                await self.human_wait(0.3, 0.8)
                await self._human_click(pub_btn)

                modal_closed = False
                try:
                    await pub_btn.wait_for(state="detached", timeout=20000)
                    modal_closed = True
                except Exception:
                    await self.human_wait(4, 6)

                post_health = await self._check_page_health("after_publish")
                if post_health == "error":
                    self.logger.error("[Publish] Error de página detectado después de publicar en %s", group_id)
                    await self._screenshot(f"post_publish_error_{group_id}.png")
                    continue
                if post_health == "login_required":
                    self.logger.error("[Publish] Sesión perdida después de publicar en %s", group_id)
                    continue

                if not modal_closed:
                    self.logger.warning("[Publish] Modal no cerró para grupo %s — reintentando", group_id)
                    await self._screenshot(f"modal_stuck_{group_id}.png")
                    continue

                self.logger.info("Published to group %s successfully", group_id)
                self._publish_count += 1
                metrics.inc_publish(self.account.name, True)
                metrics.observe_publish_duration(self.account.name, time.monotonic() - _t0)
                await self._maybe_refresh_session()
                return True

            except Exception:
                self.logger.error(
                    "Error publishing to group %s (attempt %d)",
                    group_id, attempt, exc_info=True,
                )
                await self._screenshot(f"error_{group_id}.png")
                await self._check_page_health(f"exception_attempt_{attempt}")

        self.logger.error("All %d attempts exhausted for group %s", self.config["max_retries"], group_id)
        metrics.inc_publish(self.account.name, False)
        metrics.observe_publish_duration(self.account.name, time.monotonic() - _t0)
        return False

    # ------------------------------------------------------------------ #
    # Publish to all groups
    # ------------------------------------------------------------------ #
    async def publish_to_all_groups(self, text: str, image_paths: list[str] | str | None = None) -> dict[str, bool]:
        # Backward compat: acepta string legacy (una sola imagen)
        if isinstance(image_paths, str):
            image_paths = [image_paths] if image_paths else None

        results: dict[str, bool] = {}
        variation_mode = self.config.get("text_variation_mode", "off")

        groups = self.account.groups[: self.config["max_groups_per_session"]]

        for idx, group_id in enumerate(groups):
            if random.random() < self.config.get("idle_probability", 0.0):
                idle = random.uniform(
                    self.config["idle_min_seconds"],
                    self.config["idle_max_seconds"],
                )
                self.logger.info("[Idle] Pausa aleatoria de %.1f s antes de grupo %s", idle, group_id)
                await asyncio.sleep(idle)

            if self._banned:
                self.logger.warning(
                    "[BANNED] Cuenta %s baneada — se saltan grupos restantes (%d pendientes)",
                    self.account.name, len(groups) - idx,
                )
                results[group_id] = False
                for remaining in groups[idx + 1:]:
                    results[remaining] = False
                break

            group_text = text
            if variation_mode == "gemini" and self._text_variator:
                group_text = await asyncio.to_thread(
                    self._text_variator.variate, text, self.account.name, group_id
                )
            elif variation_mode == "zero_width":
                group_text = _vary_text(text, self.account.name)

            self.logger.debug(
                "[Variation] mode=%s account=%s group=%s orig=%d final=%d chars",
                variation_mode, self.account.name, group_id, len(text), len(group_text),
            )

            success = await self.publish(group_id, group_text, image_paths=image_paths)
            results[group_id] = success

            if idx < len(groups) - 1:
                delay = random.uniform(
                    self.config["wait_between_groups_min"],
                    self.config["wait_between_groups_max"],
                )
                self.logger.info("Waiting %.0f s before next group …", delay)
                await asyncio.sleep(delay)

        return results

    # ------------------------------------------------------------------ #
    # Cleanup
    # ------------------------------------------------------------------ #
    async def close(self) -> None:
        unbind_account()
        try:
            if self.context:
                await self.context.close()
        except Exception:
            self.logger.debug("Error cerrando context", exc_info=True)
        try:
            if self._pw:
                await self._pw.stop()
                self.logger.info("Browser async closed for %s", self.account.name)
        except Exception:
            self.logger.warning("Error closing Playwright for %s", self.account.name, exc_info=True)
        try:
            lock_file = (
                Path(__file__).resolve().parent
                / "browser_profiles"
                / self.account.name
                / ".lock"
            )
            if lock_file.exists():
                lock_file.unlink()
        except Exception:
            self.logger.debug("No se pudo eliminar .lock del profile", exc_info=True)
