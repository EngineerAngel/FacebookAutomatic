"""
adaptive_selector.py — AdaptivePlaywrightBridge (Fase 3.4).

Puente entre Scrapling (adaptive HTML parsing) y Playwright (interacción).
100% genérico — no importa nada específico de Facebook.

Activar con ADAPTIVE_SELECTORS=1 en .env.
Sin la flag, get_locator() devuelve page.locator(selector) sin overhead.
"""
from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from config import CONFIG

if TYPE_CHECKING:
    from patchright.async_api import Locator, Page

logger = logging.getLogger("adaptive_selector")
_ENABLED = CONFIG.get("adaptive_selectors_enabled", False)


class AdaptivePlaywrightBridge:
    """
    Localiza elementos HTML usando Scrapling adaptive parsing como fallback
    cuando el selector original falla (Facebook cambió su interfaz).

    Flujo de resolución:
      1. ¿Hay un selector aprobado en DB para esta clave?  → usarlo
      2. Intenta el selector activo en Playwright
      3. Si falla → Scrapling busca por fingerprint (auto_match=True)
      4. Si Scrapling falla → dispara Gemini en background (fire-and-forget)
      5. Devuelve el locator original (puede fallar después — ya logueado)
    """

    def __init__(self, page: Page) -> None:
        self.page = page

    async def get_locator(
        self,
        selector_key: str,
        selector: str,
        *,
        timeout: int = 5000,
    ) -> Locator:
        """Resuelve selector_key/selector a un Playwright Locator."""
        if not _ENABLED:
            return self.page.locator(selector)

        import job_store

        # 1. Selector aprobado en DB
        approved = job_store.get_approved_selector(selector_key)
        active_selector = approved or selector
        if approved:
            logger.info(
                "[adaptive] '%s' → usando selector aprobado: %s", selector_key, approved
            )

        # 2. Intentar el selector activo
        locator = self.page.locator(active_selector)
        try:
            await locator.wait_for(state="attached", timeout=timeout)
            return locator
        except Exception:
            logger.warning(
                "[adaptive] '%s' falló (%s) — intentando Scrapling",
                selector_key, active_selector,
            )

        # 3. Scrapling adaptive
        scrapling_locator = await self._try_scrapling(selector_key, active_selector)
        if scrapling_locator:
            return scrapling_locator

        # 4. Gemini fallback — fire-and-forget, no bloquea el flujo actual
        asyncio.create_task(self._dispatch_repair(selector_key, active_selector))

        # 5. Mejor esfuerzo — devolver locator original
        return self.page.locator(selector)

    # ------------------------------------------------------------------

    async def _try_scrapling(self, selector_key: str, selector: str) -> Locator | None:
        """Localiza el elemento con Scrapling adaptive parsing."""
        try:
            from scrapling.defaults import Adaptor  # type: ignore[import]

            html = await self.page.content()
            page_url = self.page.url

            doc = Adaptor(html, url=page_url, auto_save=True, auto_match=True)

            if selector.startswith("//") or selector.startswith("xpath="):
                xpath = selector.removeprefix("xpath=")
                elements = doc.xpath(xpath, auto_match=True)
            else:
                elements = doc.css(selector, auto_match=True)

            if not elements:
                logger.warning(
                    "[adaptive] Scrapling: ningún candidato para '%s'", selector_key
                )
                return None

            element = elements[0]
            candidate_selector = _element_to_selector(element)
            logger.warning(
                "[adaptive] Scrapling recuperó '%s' → '%s' — posible cambio de UI",
                selector_key, candidate_selector,
            )

            import job_store
            job_store.create_selector_repair(
                selector_key=selector_key,
                original=selector,
                candidate=candidate_selector,
                confidence=0.80,
                source="scrapling",
                html_snippet=str(element)[:500],
            )

            return _element_to_locator(self.page, element)

        except Exception:
            logger.exception("[adaptive] Error en Scrapling para '%s'", selector_key)
            return None

    async def _dispatch_repair(self, selector_key: str, selector: str) -> None:
        """Fire-and-forget: invoca SelectorRepairService (Gemini) en background."""
        try:
            from selector_repair import SelectorRepairService
            html = await self.page.content()
            await SelectorRepairService.request_repair(selector_key, selector, html)
        except Exception:
            logger.exception(
                "[adaptive] Error en dispatch_repair para '%s'", selector_key
            )


# ---------------------------------------------------------------------------
# Helpers — conversión lxml element → Playwright Locator / selector string
# ---------------------------------------------------------------------------

def _element_to_locator(page: Page, element) -> Locator:
    """Convierte un elemento de Scrapling al Locator Playwright más robusto posible."""
    attrib = getattr(element, "attrib", {}) or {}

    testid = attrib.get("data-testid")
    if testid:
        return page.get_by_test_id(testid)

    aria = attrib.get("aria-label")
    if aria:
        return page.locator(f'[aria-label="{aria}"]')

    name = attrib.get("name")
    if name:
        return page.locator(f'[name="{name}"]')

    return page.locator(_element_to_selector(element))


def _element_to_selector(element) -> str:
    """Genera un selector XPath desde un elemento lxml de Scrapling."""
    try:
        attrib = getattr(element, "attrib", {}) or {}
        tag = getattr(element, "tag", "div") or "div"
        aria = attrib.get("aria-label", "")
        role = attrib.get("role", "")
        name = attrib.get("name", "")
        if aria:
            return f'//{tag}[@aria-label="{aria}"]'
        if name:
            return f'//{tag}[@name="{name}"]'
        if role:
            return f'//{tag}[@role="{role}"]'
        return f"//{tag}"
    except Exception:
        return "//div"
