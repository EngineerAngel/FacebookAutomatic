"""
text_variation.py — Parafraseo de anuncios con Gemini (Fase 2.2).

Facebook tokeniza y hashea el texto canónico de las publicaciones para
detectar duplicación entre grupos. Los zero-width chars solo sobreviven
al tokenizador si no se normalizan; en la práctica Facebook los elimina.
Este módulo genera una variación real por (anuncio, cuenta, grupo),
cacheada en SQLite con TTL.

Conserva: intención comercial, URLs, números (teléfonos, precios), emojis.
Varía: sinónimos, orden de frases, ganchos iniciales, tono.
"""

from __future__ import annotations

import hashlib
import logging
from typing import TYPE_CHECKING, Optional

import job_store

if TYPE_CHECKING:
    from gemini_commenter import GeminiCommenter


_DEFAULT_TTL_SECONDS = 7 * 24 * 3600  # 7 días


_PARAPHRASE_PROMPT = """Parafrasea el siguiente anuncio publicitario manteniendo estrictamente:
- Intención comercial exacta (vender lo mismo con el mismo call-to-action)
- TODOS los emojis (si hay) — no agregar ni quitar
- URLs completas sin modificar
- Números de teléfono, precios y fechas sin modificar
- Longitud aproximada (±20%)
- Español mexicano profesional

Varía libremente: sinónimos, orden de frases, gancho inicial, tono. Evita
construcciones que suenen a IA (p.ej. "¡Descubre hoy...!", "No te pierdas...").

NO uses prefijos como "Aquí tienes:" ni envuelvas en comillas.
Devuelve SOLO el texto parafraseado, sin comentarios adicionales.

TEXTO ORIGINAL:
{text}"""


class TextVariator:
    """Genera variaciones parafraseadas de un anuncio por (cuenta, grupo).

    Cache: (sha256(text)[:12], account, group) → variación. TTL 7 días.
    Si Gemini falla o está degraded, devuelve el texto original (graceful).
    """

    def __init__(
        self,
        gemini: "GeminiCommenter",
        logger: logging.Logger,
        ttl_seconds: int = _DEFAULT_TTL_SECONDS,
        cache_enabled: bool = True,
    ) -> None:
        self.gemini = gemini
        self.logger = logger
        self.ttl_seconds = ttl_seconds
        self.cache_enabled = cache_enabled

    def variate(self, text: str, account_name: str, group_id: str) -> str:
        """Retorna una versión parafraseada. Fallback al original si falla."""
        if not text or not text.strip():
            return text

        text_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]
        key = f"{text_hash}:{account_name}:{group_id}"

        if self.cache_enabled:
            cached = job_store.get_text_variation(key, self.ttl_seconds)
            if cached:
                self.logger.debug("[Variator] Cache hit para %s/%s", account_name, group_id)
                return cached

        if not self.gemini or not self.gemini.enabled:
            return text

        prompt = _PARAPHRASE_PROMPT.format(text=text)
        result = self.gemini.generate_text(prompt, max_tokens=800)

        if not result or len(result.strip()) < 20:
            self.logger.debug(
                "[Variator] Respuesta inválida — fallback al original (account=%s, group=%s)",
                account_name, group_id,
            )
            return text

        variated = _strip_wrapping(result)
        if self.cache_enabled:
            try:
                job_store.save_text_variation(key, text_hash, variated)
            except Exception:
                self.logger.debug("[Variator] No se pudo cachear la variación", exc_info=True)

        self.logger.info(
            "[Variator] Nueva variación para %s/%s (%d → %d chars)",
            account_name, group_id, len(text), len(variated),
        )
        return variated


def _strip_wrapping(text: str) -> str:
    """Quita comillas envolventes y prefijos comunes que Gemini a veces agrega."""
    cleaned = text.strip()
    for _ in range(2):
        if len(cleaned) >= 2 and cleaned[0] in '"\'' and cleaned[-1] == cleaned[0]:
            cleaned = cleaned[1:-1].strip()
    return cleaned
