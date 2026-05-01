"""
human_browsing.py — Calentamiento humano antes de publicar en un grupo.

Antes de abrir el compositor, la cuenta "consume" contenido del grupo:
  - Scroll random en el feed.
  - Hover ocasional sobre una publicación.
  - Apertura ocasional de un hilo de comentarios (sin escribir).
  - (Opcional) Comentario sutil generado por Gemini en una publicación
    ajena con texto+imagen.

Diseñado para no romper la publicación principal: cualquier excepción se
captura, se loguea como warning y la función devuelve sin levantar.
"""

from __future__ import annotations

import asyncio
import random
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from facebook_poster_async import FacebookPosterAsync
    from gemini_commenter import GeminiCommenter


_ARTICLE_XPATH = "//div[@role='article']"

_COMMENT_BUTTON_XPATHS = (
    ".//div[@role='button'][.//span[contains(translate(text(),'COMENTAR','comentar'),'comentar')]]",
    ".//div[@role='button'][.//span[contains(translate(text(),'COMMENT','comment'),'comment')]]",
    ".//span[contains(text(),'Comentar')]/ancestor::div[@role='button'][1]",
    ".//span[contains(text(),'Comment')]/ancestor::div[@role='button'][1]",
)

_COMMENT_INPUT_XPATHS = (
    ".//div[@aria-label='Escribe un comentario...' or @aria-label='Escribe un comentario']",
    ".//div[@aria-label='Write a comment...' or @aria-label='Write a comment']",
    ".//div[contains(@aria-label,'comentario público') or contains(@aria-label,'public comment')]",
    ".//div[@contenteditable='true' and (@role='textbox' or @aria-label)]",
)

_POST_TEXT_XPATHS = (
    ".//div[@data-ad-preview='message']",
    ".//div[@data-ad-comet-preview='message']",
    ".//div[contains(@data-testid,'post_message')]",
)

_POST_IMG_XPATHS = (
    ".//img[contains(@src,'scontent') and not(contains(@src,'static'))]",
    ".//img[contains(@src,'fbcdn') and not(contains(@src,'static'))]",
)

_IMG_HTTP_TIMEOUT_S = 3.0
_IMG_FUTURE_TIMEOUT_S = 3.5


# ---------------------------------------------------------------------------
# HumanBrowsingAsync — versión async para FacebookPosterAsync (Fase 3.1)
# ---------------------------------------------------------------------------
class HumanBrowsingAsync:
    """Orquestador async de comportamiento humano dentro de un grupo.

    Equivalente a HumanBrowsing pero para FacebookPosterAsync:
    - time.sleep → await asyncio.sleep
    - requests.get → httpx.AsyncClient
    - Todas las ops de página son awaited
    - Gemini (sync internamente) se envuelve con asyncio.to_thread
    """

    def __init__(
        self,
        poster: "FacebookPosterAsync",
        config: dict,
        gemini: Optional["GeminiCommenter"] = None,
    ) -> None:
        self.poster = poster
        self.page = poster.page
        self.logger = poster.logger
        self.config = config
        self.gemini = gemini
        self._comments_made = 0

    # ------------------------------------------------------------------ #
    # Public entry point
    # ------------------------------------------------------------------ #
    async def warmup_in_group(self, group_id: str) -> None:
        """Ejecuta el warmup probabilístico. Nunca levanta excepciones."""
        try:
            if random.random() > self.config.get("warmup_probability", 0.6):
                self.logger.debug("[Warmup] Skip por probabilidad en grupo %s", group_id)
                return

            t_start = asyncio.get_event_loop().time()
            duration_target = random.uniform(
                self.config.get("warmup_duration_min", 8),
                self.config.get("warmup_duration_max", 25),
            )
            self.logger.info("[Warmup] Iniciando warmup async en grupo %s (target %.1fs)",
                             group_id, duration_target)

            await self._scroll_feed()

            if random.random() < self.config.get("warmup_hover_probability", 0.5):
                await self._hover_random_article()

            if random.random() < self.config.get("warmup_open_comments_probability", 0.3):
                await self._peek_comments_on_random_article()

            if (
                self.gemini is not None
                and self.gemini.enabled
                and self._comments_made < self.config.get("gemini_comment_max_per_session", 2)
                and random.random() < self.config.get("gemini_comment_probability", 0.20)
            ):
                ok = await self._post_gemini_comment_on_random_article()
                if ok:
                    self._comments_made += 1

            elapsed = asyncio.get_event_loop().time() - t_start
            if elapsed < duration_target:
                await self.poster.human_wait(0.5, max(0.6, duration_target - elapsed))

            try:
                await self.page.evaluate("window.scrollTo({top: 0, behavior: 'smooth'})")
            except Exception:
                pass
            await self.poster.human_wait(0.8, 1.6)

            self.logger.info("[Warmup] Completado en %.1fs (comments_made=%d)",
                             asyncio.get_event_loop().time() - t_start, self._comments_made)
        except Exception:
            self.logger.warning("[Warmup] Excepción no fatal capturada", exc_info=True)

    # ------------------------------------------------------------------ #
    # Building blocks
    # ------------------------------------------------------------------ #
    async def _scroll_feed(self) -> None:
        n = random.randint(
            self.config.get("warmup_scrolls_min", 2),
            self.config.get("warmup_scrolls_max", 5),
        )
        self.logger.info("[Warmup] Scroll feed × %d", n)
        for i in range(n):
            distance = random.randint(200, 800)
            try:
                await self.page.evaluate(
                    f"window.scrollBy({{top: {distance}, behavior: 'smooth'}})"
                )
            except Exception:
                self.logger.debug("[Warmup] scrollBy falló", exc_info=True)
            await self.poster.human_wait(2, 5)
            if random.random() < 0.15:
                try:
                    await self.page.evaluate(
                        f"window.scrollBy({{top: -{random.randint(80, 250)}, behavior: 'smooth'}})"
                    )
                except Exception:
                    pass
                await self.poster.human_wait(1, 2)

    async def _hover_random_article(self) -> None:
        article = await self._pick_visible_article()
        if article is None:
            return
        try:
            await article.scroll_into_view_if_needed(timeout=3000)
            await self.poster.human_wait(0.4, 0.9)
            await article.hover(timeout=3000)
            self.logger.info("[Warmup] Hover sobre publicación")
            await self.poster.human_wait(1, 3)
        except Exception:
            self.logger.debug("[Warmup] Hover falló", exc_info=True)

    async def _peek_comments_on_random_article(self) -> None:
        article = await self._pick_visible_article()
        if article is None:
            return
        btn = await self._first_match_in(article, _COMMENT_BUTTON_XPATHS, timeout=3)
        if btn is None:
            self.logger.debug("[Warmup] Sin botón Comentarios visible")
            return
        try:
            await self.poster._human_click(btn)
            self.logger.info("[Warmup] Abierto hilo de comentarios")
            await self.poster.human_wait(2, 4)
            try:
                await self.page.evaluate(
                    f"window.scrollBy({{top: {random.randint(150, 400)}, behavior: 'smooth'}})"
                )
            except Exception:
                pass
            await self.poster.human_wait(3, 7)
            try:
                await self.page.keyboard.press("Escape")
            except Exception:
                pass
            await self.poster.human_wait(0.8, 1.5)
        except Exception:
            self.logger.debug("[Warmup] Peek de comentarios falló", exc_info=True)

    async def _post_gemini_comment_on_random_article(self) -> bool:
        article = await self._pick_visible_article()
        if article is None:
            self.logger.debug("[Gemini] No hay article visible para comentar")
            return False

        post_text = await self._extract_text(article)
        image_bytes, image_mime = await self._extract_image_bytes(article)

        if not post_text and not image_bytes:
            self.logger.debug("[Gemini] Article sin texto ni imagen utilizable — skip")
            return False

        # Gemini es sync internamente — lo ejecutamos en un thread para no bloquear el loop
        comment = await asyncio.to_thread(
            self.gemini.generate_comment,
            post_text,
            image_bytes,
            image_mime or "image/jpeg",
        )
        if not comment:
            self.logger.debug("[Gemini] No se generó comentario válido")
            return False

        self.logger.info("[Gemini] Comentario generado: %r", comment[:120])

        try:
            await article.scroll_into_view_if_needed(timeout=3000)
        except Exception:
            pass
        await self.poster.human_wait(0.6, 1.2)

        comment_input = await self._first_match_in(article, _COMMENT_INPUT_XPATHS, timeout=4)
        if comment_input is None:
            btn = await self._first_match_in(article, _COMMENT_BUTTON_XPATHS, timeout=3)
            if btn is not None:
                try:
                    await self.poster._human_click(btn)
                    await self.poster.human_wait(1, 2)
                except Exception:
                    pass
            comment_input = await self._first_match_in(article, _COMMENT_INPUT_XPATHS, timeout=4)

        if comment_input is None:
            self.logger.warning("[Gemini] No se encontró input de comentario en el article")
            return False

        try:
            await self.poster._human_click(comment_input)
            await self.poster.human_wait(0.5, 1.2)
            await self.poster._human_type(comment_input, comment)
            await self.poster.human_wait(0.8, 1.6)
            await self.page.keyboard.press("Enter")
            self.logger.info("[Gemini] Comentario enviado (Enter)")
            await self.poster.human_wait(2, 4)
            return True
        except Exception:
            self.logger.warning("[Gemini] Falló el envío del comentario", exc_info=True)
            return False

    # ------------------------------------------------------------------ #
    # Internals
    # ------------------------------------------------------------------ #
    async def _pick_visible_article(self):
        try:
            articles = self.page.locator(_ARTICLE_XPATH)
            count = await articles.count()
        except Exception:
            return None
        if count == 0:
            return None

        indices = list(range(count))
        random.shuffle(indices)
        for idx in indices[:5]:
            try:
                cand = articles.nth(idx)
                if await cand.is_visible():
                    return cand
            except Exception:
                continue
        return None

    async def _first_match_in(self, parent_locator, xpaths, timeout: float = 3.0):
        per = max(round(timeout / len(xpaths), 1), 1.0)
        for xp in xpaths:
            try:
                loc = parent_locator.locator(f"xpath={xp}").first
                await loc.wait_for(state="visible", timeout=int(per * 1000))
                return loc
            except Exception:
                continue
        return None

    async def _extract_text(self, article) -> str:
        for xp in _POST_TEXT_XPATHS:
            try:
                loc = article.locator(f"xpath={xp}").first
                if await loc.count() > 0 and await loc.is_visible():
                    txt = (await loc.inner_text(timeout=2000) or "").strip()
                    if txt:
                        return txt[:500]
            except Exception:
                continue
        return ""

    async def _extract_image_bytes(self, article):
        """Descarga la primera imagen del post con httpx async. (None, '') si falla."""
        try:
            import httpx
        except ImportError:
            self.logger.debug("[Gemini] httpx no instalado — skip imagen async")
            return None, ""

        for xp in _POST_IMG_XPATHS:
            try:
                img = article.locator(f"xpath={xp}").first
                if await img.count() == 0 or not await img.is_visible():
                    continue
                src = await img.get_attribute("src", timeout=2000) or ""
                if not src.startswith("http"):
                    continue
                try:
                    async with httpx.AsyncClient(timeout=_IMG_HTTP_TIMEOUT_S) as client:
                        resp = await asyncio.wait_for(
                            client.get(src),
                            timeout=_IMG_FUTURE_TIMEOUT_S,
                        )
                except asyncio.TimeoutError:
                    self.logger.debug(
                        "[Gemini] Imagen no descargó en %.1fs — skip",
                        _IMG_FUTURE_TIMEOUT_S,
                    )
                    continue
                if resp.status_code != 200:
                    continue
                mime = resp.headers.get("content-type", "image/jpeg").split(";")[0].strip()
                if not mime.startswith("image/"):
                    mime = "image/jpeg"
                content = bytes(resp.content)[: 4 * 1024 * 1024]
                return content, mime
            except Exception:
                continue
        return None, ""
