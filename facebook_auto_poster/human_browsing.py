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

import random
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from typing import TYPE_CHECKING, Optional

import requests


# Pool compartido para descargas de imágenes (no bloquea el thread del browser
# por más de _IMG_FUTURE_TIMEOUT_S)
_IMG_HTTP_TIMEOUT_S = 3.0      # timeout dentro de requests.get
_IMG_FUTURE_TIMEOUT_S = 3.5    # límite duro desde el caller
_img_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="img-dl")

if TYPE_CHECKING:
    from facebook_poster import FacebookPoster
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


class HumanBrowsing:
    """Orquestador de comportamiento humano dentro de un grupo."""

    def __init__(
        self,
        poster: "FacebookPoster",
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
    def warmup_in_group(self, group_id: str) -> None:
        """Ejecuta el warmup probabilístico. Nunca levanta excepciones."""
        try:
            if random.random() > self.config.get("warmup_probability", 0.6):
                self.logger.debug("[Warmup] Skip por probabilidad en grupo %s", group_id)
                return

            t_start = time.time()
            duration_target = random.uniform(
                self.config.get("warmup_duration_min", 8),
                self.config.get("warmup_duration_max", 25),
            )
            self.logger.info("[Warmup] Iniciando warmup en grupo %s (target %.1fs)",
                             group_id, duration_target)

            self._scroll_feed()

            if random.random() < self.config.get("warmup_hover_probability", 0.5):
                self._hover_random_article()

            if random.random() < self.config.get("warmup_open_comments_probability", 0.3):
                self._peek_comments_on_random_article()

            if (
                self.gemini is not None
                and self.gemini.enabled
                and self._comments_made < self.config.get("gemini_comment_max_per_session", 2)
                and random.random() < self.config.get("gemini_comment_probability", 0.20)
            ):
                ok = self._post_gemini_comment_on_random_article()
                if ok:
                    self._comments_made += 1

            # Asegurar duración mínima del warmup
            elapsed = time.time() - t_start
            if elapsed < duration_target:
                self.poster.human_wait(0.5, max(0.6, duration_target - elapsed))

            # Volver al inicio del feed antes de devolver control
            try:
                self.page.evaluate("window.scrollTo({top: 0, behavior: 'smooth'})")
            except Exception:
                pass
            self.poster.human_wait(0.8, 1.6)

            self.logger.info("[Warmup] Completado en %.1fs (comments_made=%d)",
                             time.time() - t_start, self._comments_made)
        except Exception:
            self.logger.warning("[Warmup] Excepción no fatal capturada", exc_info=True)

    # ------------------------------------------------------------------ #
    # Building blocks
    # ------------------------------------------------------------------ #
    def _scroll_feed(self) -> None:
        n = random.randint(
            self.config.get("warmup_scrolls_min", 2),
            self.config.get("warmup_scrolls_max", 5),
        )
        self.logger.info("[Warmup] Scroll feed × %d", n)
        for i in range(n):
            distance = random.randint(200, 800)
            try:
                self.page.evaluate(
                    f"window.scrollBy({{top: {distance}, behavior: 'smooth'}})"
                )
            except Exception:
                self.logger.debug("[Warmup] scrollBy falló", exc_info=True)
            self.poster.human_wait(2, 5)
            # Ocasionalmente scroll hacia arriba (humano relee)
            if random.random() < 0.15:
                try:
                    self.page.evaluate(
                        f"window.scrollBy({{top: -{random.randint(80, 250)}, behavior: 'smooth'}})"
                    )
                except Exception:
                    pass
                self.poster.human_wait(1, 2)

    def _hover_random_article(self) -> None:
        article = self._pick_visible_article()
        if article is None:
            return
        try:
            article.scroll_into_view_if_needed(timeout=3000)
            self.poster.human_wait(0.4, 0.9)
            article.hover(timeout=3000)
            self.logger.info("[Warmup] Hover sobre publicación")
            self.poster.human_wait(1, 3)
        except Exception:
            self.logger.debug("[Warmup] Hover falló", exc_info=True)

    def _peek_comments_on_random_article(self) -> None:
        """Abre el botón Comentarios y mira el hilo sin escribir."""
        article = self._pick_visible_article()
        if article is None:
            return
        btn = self._first_match_in(article, _COMMENT_BUTTON_XPATHS, timeout=3)
        if btn is None:
            self.logger.debug("[Warmup] Sin botón Comentarios visible")
            return
        try:
            self.poster._human_click(btn)
            self.logger.info("[Warmup] Abierto hilo de comentarios")
            self.poster.human_wait(2, 4)
            # Scroll dentro del hilo
            try:
                self.page.evaluate(
                    f"window.scrollBy({{top: {random.randint(150, 400)}, behavior: 'smooth'}})"
                )
            except Exception:
                pass
            self.poster.human_wait(3, 7)
            # Cerrar con Escape o click fuera (Esc primero, más natural)
            try:
                self.page.keyboard.press("Escape")
            except Exception:
                pass
            self.poster.human_wait(0.8, 1.5)
        except Exception:
            self.logger.debug("[Warmup] Peek de comentarios falló", exc_info=True)

    def _post_gemini_comment_on_random_article(self) -> bool:
        """Genera y postea un comentario en una publicación ajena. True si OK."""
        article = self._pick_visible_article()
        if article is None:
            self.logger.debug("[Gemini] No hay article visible para comentar")
            return False

        post_text = self._extract_text(article)
        image_bytes, image_mime = self._extract_image_bytes(article)

        if not post_text and not image_bytes:
            self.logger.debug("[Gemini] Article sin texto ni imagen utilizable — skip")
            return False

        comment = self.gemini.generate_comment(post_text, image_bytes, image_mime or "image/jpeg")
        if not comment:
            self.logger.debug("[Gemini] No se generó comentario válido")
            return False

        self.logger.info("[Gemini] Comentario generado: %r", comment[:120])

        # Buscar input de comentario dentro del article
        try:
            article.scroll_into_view_if_needed(timeout=3000)
        except Exception:
            pass
        self.poster.human_wait(0.6, 1.2)

        comment_input = self._first_match_in(article, _COMMENT_INPUT_XPATHS, timeout=4)
        if comment_input is None:
            # Algunos posts requieren clickear el botón Comentar primero
            btn = self._first_match_in(article, _COMMENT_BUTTON_XPATHS, timeout=3)
            if btn is not None:
                try:
                    self.poster._human_click(btn)
                    self.poster.human_wait(1, 2)
                except Exception:
                    pass
            comment_input = self._first_match_in(article, _COMMENT_INPUT_XPATHS, timeout=4)

        if comment_input is None:
            self.logger.warning("[Gemini] No se encontró input de comentario en el article")
            return False

        try:
            self.poster._human_click(comment_input)
            self.poster.human_wait(0.5, 1.2)
            self.poster._human_type(comment_input, comment)
            self.poster.human_wait(0.8, 1.6)
            self.page.keyboard.press("Enter")
            self.logger.info("[Gemini] Comentario enviado (Enter)")
            self.poster.human_wait(2, 4)
            return True
        except Exception:
            self.logger.warning("[Gemini] Falló el envío del comentario", exc_info=True)
            return False

    # ------------------------------------------------------------------ #
    # Internals
    # ------------------------------------------------------------------ #
    def _pick_visible_article(self):
        """Devuelve un locator de un article visible al azar, o None."""
        try:
            articles = self.page.locator(_ARTICLE_XPATH)
            count = articles.count()
        except Exception:
            return None
        if count == 0:
            return None

        # Probar hasta 5 candidatos al azar para evitar elementos invisibles/anuncios
        indices = list(range(count))
        random.shuffle(indices)
        for idx in indices[:5]:
            try:
                cand = articles.nth(idx)
                if cand.is_visible():
                    return cand
            except Exception:
                continue
        return None

    def _first_match_in(self, parent_locator, xpaths, timeout: float = 3.0):
        """Devuelve el primer locator hijo que matchee uno de los xpaths."""
        per = max(round(timeout / len(xpaths), 1), 1.0)
        for xp in xpaths:
            try:
                loc = parent_locator.locator(f"xpath={xp}").first
                loc.wait_for(state="visible", timeout=int(per * 1000))
                return loc
            except Exception:
                continue
        return None

    def _extract_text(self, article) -> str:
        for xp in _POST_TEXT_XPATHS:
            try:
                loc = article.locator(f"xpath={xp}").first
                if loc.count() > 0 and loc.is_visible():
                    txt = (loc.inner_text(timeout=2000) or "").strip()
                    if txt:
                        return txt[:500]
            except Exception:
                continue
        return ""

    def _extract_image_bytes(self, article):
        """Devuelve (bytes, mime) de la primera imagen del post, o (None, '').

        La descarga HTTP se ejecuta en un ThreadPoolExecutor con timeout duro
        para que un CDN lento no bloquee al browser (señal de inactividad que
        Facebook podría detectar como automatización).
        """
        for xp in _POST_IMG_XPATHS:
            try:
                img = article.locator(f"xpath={xp}").first
                if img.count() == 0 or not img.is_visible():
                    continue
                src = img.get_attribute("src", timeout=2000) or ""
                if not src.startswith("http"):
                    continue
                future = _img_executor.submit(
                    requests.get, src, timeout=_IMG_HTTP_TIMEOUT_S
                )
                try:
                    resp = future.result(timeout=_IMG_FUTURE_TIMEOUT_S)
                except FutureTimeout:
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
                # Limitar tamaño a ~4 MB
                content = resp.content[: 4 * 1024 * 1024]
                return content, mime
            except Exception:
                continue
        return None, ""
