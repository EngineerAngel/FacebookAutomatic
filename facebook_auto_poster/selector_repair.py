"""
selector_repair.py — Gemini fallback para reparación de selectores (Fase 3.4).

Se invoca cuando Scrapling no puede recuperar un selector.
Captura el HTML actual, llama a Gemini y guarda candidatos en DB
como 'pending' para que el admin los apruebe desde el panel.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re

import job_store
from config import CONFIG

logger = logging.getLogger("selector_repair")

_GEMINI_PROMPT = """\
Eres un experto en XPath y selectores CSS para Playwright.

El selector que se usaba anteriormente ya no funciona en la página de Facebook:
  Clave:             {selector_key}
  Selector original: {original_selector}

Dado el siguiente fragmento HTML (scripts y estilos eliminados), sugiere 1-3 \
selectores XPath alternativos que encuentren el elemento equivalente.

HTML:
{html_snippet}

Responde SOLO con JSON válido (sin markdown, sin texto extra):
[
  {{"selector": "//xpath/aqui", "confidence": 0.90, "reason": "por qué este"}},
  {{"selector": "//xpath/alternativo", "confidence": 0.70, "reason": "..."}}
]
"""

_HTML_TRUNCATE = 8_000


class SelectorRepairService:

    @staticmethod
    async def request_repair(
        selector_key: str,
        original_selector: str,
        html: str,
    ) -> None:
        """Llama a Gemini con el HTML actual y guarda candidatos en DB (fire-and-forget)."""
        from gemini_commenter import GeminiCommenter

        html_snippet = _strip_html(html)[:_HTML_TRUNCATE]
        prompt = _GEMINI_PROMPT.format(
            selector_key=selector_key,
            original_selector=original_selector,
            html_snippet=html_snippet,
        )

        try:
            commenter = GeminiCommenter(CONFIG)
            raw = await asyncio.to_thread(commenter.generate_text, prompt, 800)
            if not raw:
                logger.warning(
                    "[selector_repair] Gemini no devolvió respuesta para '%s'",
                    selector_key,
                )
                return

            candidates = _parse_candidates(raw)
            if not candidates:
                logger.warning(
                    "[selector_repair] JSON no parseable de Gemini para '%s': %.200s",
                    selector_key, raw,
                )
                return

            for c in candidates[:3]:
                job_store.create_selector_repair(
                    selector_key=selector_key,
                    original=original_selector,
                    candidate=c["selector"],
                    confidence=float(c.get("confidence", 0.5)),
                    source="gemini",
                    html_snippet=html_snippet[:500],
                )
                logger.warning(
                    "[selector_repair] Candidato guardado para '%s' (conf=%.2f): %s",
                    selector_key, c.get("confidence", 0), c["selector"],
                )

        except Exception:
            logger.exception(
                "[selector_repair] Error invocando Gemini para '%s'", selector_key
            )


def _strip_html(html: str) -> str:
    """Elimina scripts, estilos y comprime whitespace."""
    html = re.sub(r"<script[^>]*>.*?</script>", "", html, flags=re.DOTALL | re.IGNORECASE)
    html = re.sub(r"<style[^>]*>.*?</style>", "", html, flags=re.DOTALL | re.IGNORECASE)
    html = re.sub(r"\s+", " ", html)
    return html.strip()


def _parse_candidates(raw: str) -> list[dict]:
    """Parsea la respuesta JSON de Gemini, limpiando markdown si lo envuelve."""
    try:
        raw = re.sub(r"^```json\s*", "", raw.strip(), flags=re.IGNORECASE)
        raw = re.sub(r"```$", "", raw.strip())
        result = json.loads(raw)
        if isinstance(result, list):
            return [c for c in result if isinstance(c, dict) and "selector" in c]
        return []
    except Exception:
        return []
