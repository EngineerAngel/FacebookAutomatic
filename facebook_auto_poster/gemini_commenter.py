"""
gemini_commenter.py — Wrapper para generar comentarios humanos con Gemini.

Soporta múltiples API keys con rotación automática: si una clave agota
quota (429 / ResourceExhausted), rota a la siguiente disponible.

Coordinación entre cuentas (estado de clase, compartido en mismo proceso):
- Una sola petición a Gemini en vuelo a la vez.
- Tras timeout duro de 60s, Gemini entra en cooldown 5 min — pero la
  respuesta tardía (cuando llega en background) levanta el cooldown,
  sirviendo como confirmación de que la API está saludable de nuevo.
"""

from __future__ import annotations

import concurrent.futures
import logging
import re
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Optional

try:
    from google import genai
    from google.genai import types as genai_types
    _HAS_GENAI = True
except ImportError:
    genai = None  # type: ignore[assignment]
    genai_types = None  # type: ignore[assignment]
    _HAS_GENAI = False

# Tiempo máximo absoluto que esperamos una respuesta de Gemini (segundos).
GEMINI_HARD_TIMEOUT_S = 60

# Tras un timeout duro, no llamamos a Gemini durante este tiempo.
# Si la respuesta tardía llega antes, el cooldown se levanta automáticamente.
GEMINI_DEGRADED_COOLDOWN_S = 300

# Cooldown antes de reintentar una clave que agotó quota (segundos).
_QUOTA_COOLDOWN_S = 300

_QUOTA_ERROR_CODES = (429, 503)
_QUOTA_MSG_PATTERNS = (
    "quota",
    "resource_exhausted",
    "too many requests",
    "ratelimitexceeded",
)

_BANNED_PATTERNS = (
    re.compile(r"https?://", re.I),
    re.compile(r"www\.", re.I),
    re.compile(r"\S+@\S+\.\S+"),
    re.compile(r"#bot", re.I),
    re.compile(r"\b(IA|AI|chatbot|bot|GPT|Gemini|Claude)\b", re.I),
)

# Logger compartido para el callback de "respuesta tardía" — independiente
# del logger por cuenta (que pudo cerrar antes de que la API respondiera).
_shared_logger = logging.getLogger("gemini.shared")

_PROMPT_BY_LANG = {
    "es-MX": (
        "Eres un usuario real de Facebook en México. Vas a leer una "
        "publicación de un grupo y escribir UN comentario corto, casual, "
        "como lo haría una persona real al desplazarse por su feed.\n\n"
        "Reglas estrictas:\n"
        "- Máximo 1-2 frases. Idealmente menos de 80 caracteres.\n"
        "- Español coloquial mexicano (puedes usar 'qué padre', 'se ve "
        "bien', 'me interesa', 'cuánto?', 'sigue disponible?', etc.).\n"
        "- Sin emojis excesivos (máximo 1 si encaja, mejor ninguno).\n"
        "- Sin signos de exclamación múltiples (!!).\n"
        "- Nunca menciones que eres una IA, bot, asistente o modelo.\n"
        "- Nunca incluyas URLs, emails, hashtags ni @menciones.\n"
        "- No saludes ('Hola', 'Buenas'). Comenta directo.\n"
        "- Si la publicación es venta: pregunta precio o disponibilidad "
        "de forma natural.\n"
        "- Si es compartir/opinión: reacciona breve.\n\n"
        "Devuelve SOLO el comentario, sin comillas, sin explicación."
    ),
    "es-ES": (
        "Eres un usuario real de Facebook en España. Vas a escribir UN "
        "comentario corto y casual sobre la siguiente publicación.\n\n"
        "Reglas: máximo 1-2 frases, español coloquial, sin emojis "
        "excesivos, sin URLs ni hashtags, sin saludos. Nunca menciones "
        "que eres una IA. Devuelve SOLO el comentario."
    ),
}


def _is_quota_error(exc: BaseException) -> bool:
    msg = str(exc).lower()
    return any(p in msg for p in _QUOTA_MSG_PATTERNS)


class _KeySlot:
    """Un client de Gemini asociado a una clave."""

    def __init__(self, api_key: str, index: int, logger: logging.Logger) -> None:
        self.index = index
        self.key_hint = f"...{api_key[-6:]}" if len(api_key) > 6 else "?"
        self.client = None
        self.exhausted_until: float = 0.0  # timestamp hasta cuando no usarla

        try:
            self.client = genai.Client(api_key=api_key)
            logger.info("[Gemini] Clave %d inicializada (%s)", index + 1, self.key_hint)
        except Exception:
            logger.warning("[Gemini] Clave %d inválida (%s) — ignorada",
                           index + 1, self.key_hint, exc_info=True)

    @property
    def usable(self) -> bool:
        return self.client is not None and time.time() >= self.exhausted_until

    def mark_exhausted(self, logger: logging.Logger) -> None:
        self.exhausted_until = time.time() + _QUOTA_COOLDOWN_S
        logger.warning("[Gemini] Clave %d agotó quota — en cooldown %ds",
                       self.index + 1, _QUOTA_COOLDOWN_S)


class GeminiCommenter:
    """Genera comentarios humanos vía Gemini multimodal API.

    - Estado de clase serializa peticiones entre cuentas (1 en vuelo a la vez).
    - Tras timeout duro 60s: cooldown 5min, levantado automáticamente cuando
      la respuesta tardía llega en background (confirmación de salud).
    - Si todas las claves están agotadas o el SDK no está instalado,
      `enabled=False` y `generate_comment()` devuelve None sin error.
    """

    # ─── Estado compartido entre TODAS las instancias del mismo proceso ───
    _class_lock: threading.Lock = threading.Lock()
    _class_pending_future: Optional[Future] = None
    _class_pending_started_at: float = 0.0
    _class_degraded_until: float = 0.0
    _class_executor: ThreadPoolExecutor = ThreadPoolExecutor(
        max_workers=1, thread_name_prefix="gemini-shared",
    )

    def __init__(
        self,
        api_keys: list[str],
        model: str,
        timeout: int,
        lang: str,
        logger: logging.Logger,
    ) -> None:
        self.logger = logger
        self.model = model
        self.timeout = timeout          # timeout blando (no usado actualmente)
        self.lang = lang
        self.enabled = False
        self._slots: list[_KeySlot] = []
        self._current: int = 0

        if not _HAS_GENAI:
            self.logger.info("[Gemini] SDK google-genai no instalado — desactivado")
            return

        valid_keys = [k.strip() for k in api_keys if k.strip()]
        if not valid_keys:
            self.logger.info("[Gemini] Sin claves configuradas — desactivado")
            return

        for i, key in enumerate(valid_keys):
            slot = _KeySlot(key, i, logger)
            if slot.client is not None:
                self._slots.append(slot)

        if self._slots:
            self.enabled = True
            self.logger.info(
                "[Gemini] %d clave(s) activa(s), modelo=%s, "
                "timeout_duro=%ds, degraded_cooldown=%ds",
                len(self._slots), model,
                GEMINI_HARD_TIMEOUT_S, GEMINI_DEGRADED_COOLDOWN_S,
            )
        else:
            self.logger.warning("[Gemini] Todas las claves inválidas — desactivado")

    # ------------------------------------------------------------------ #
    # Public
    # ------------------------------------------------------------------ #
    def generate_comment(
        self,
        post_text: str,
        image_bytes: Optional[bytes] = None,
        image_mime: str = "image/jpeg",
    ) -> Optional[str]:
        """Genera un comentario. Devuelve string o None. Nunca levanta excepción."""
        if not self.enabled:
            return None

        text_part = (post_text or "").strip()
        if not text_part and not image_bytes:
            self.logger.debug("[Gemini] Sin contenido — skip")
            return None

        contents = self._build_contents(text_part, image_bytes, image_mime)

        cls = type(self)
        tried: set[int] = set()
        while True:
            future: Optional[Future] = None
            slot: Optional[_KeySlot] = None
            t0: float = 0.0

            with cls._class_lock:
                now = time.time()

                if now < cls._class_degraded_until:
                    remaining = int(cls._class_degraded_until - now)
                    self.logger.info(
                        "[Gemini] En cooldown por timeout previo (%ds restantes) — skip",
                        remaining,
                    )
                    return None

                pending = cls._class_pending_future
                if pending is not None and not pending.done():
                    elapsed = now - cls._class_pending_started_at
                    self.logger.info(
                        "[Gemini] Petición en vuelo (%.1fs) — skip para no saturar",
                        elapsed,
                    )
                    return None

                slot = self._next_usable_slot(skip=tried)
                if slot is None:
                    self.logger.warning("[Gemini] Sin claves disponibles para generar comentario")
                    return None
                tried.add(slot.index)

                future = cls._class_executor.submit(
                    slot.client.models.generate_content,
                    model=self.model,
                    contents=contents,
                )
                cls._class_pending_future = future
                cls._class_pending_started_at = now
                t0 = now
                future.add_done_callback(cls._on_request_done)

            # Fuera del lock: esperar resultado con timeout duro.
            try:
                resp = future.result(timeout=GEMINI_HARD_TIMEOUT_S)
                elapsed = time.time() - t0
                raw = (resp.text or "").strip()
                self.logger.info(
                    "[Gemini] Clave %d respondió en %.2fs (%d chars)",
                    slot.index + 1, elapsed, len(raw),
                )
                clean = self._sanitize(raw)
                if clean:
                    return clean
                self.logger.debug(
                    "[Gemini] Respuesta descartada por sanitización: %r", raw[:120]
                )
                return None

            except concurrent.futures.TimeoutError:
                # Timeout duro: marcar degraded, NO limpiar pending_future.
                # El callback lo limpiará y levantará el cooldown si la API
                # responde tarde (señal de que volvió a estar saludable).
                with cls._class_lock:
                    cls._class_degraded_until = time.time() + GEMINI_DEGRADED_COOLDOWN_S
                self.logger.warning(
                    "[Gemini] Timeout duro %ds (clave %d) — cooldown %ds, "
                    "esperando respuesta tardía en background",
                    GEMINI_HARD_TIMEOUT_S, slot.index + 1, GEMINI_DEGRADED_COOLDOWN_S,
                )
                return None

            except Exception as exc:
                elapsed = time.time() - t0
                if _is_quota_error(exc):
                    slot.mark_exhausted(self.logger)
                    # El callback ya limpió pending_future (excepción ≠ timeout).
                    continue
                self.logger.warning(
                    "[Gemini] Error en clave %d (%.2fs): %s",
                    slot.index + 1, elapsed, exc,
                )
                return None

    def is_available(self) -> bool:
        """True si no está degradado ni hay petición en vuelo. Solo informativo."""
        if not self.enabled:
            return False
        cls = type(self)
        with cls._class_lock:
            now = time.time()
            if now < cls._class_degraded_until:
                return False
            pending = cls._class_pending_future
            if pending is not None and not pending.done():
                return False
        return True

    def health_status(self) -> str:
        """Devuelve 'ok' | 'in_flight' | 'degraded(Xs)' | 'disabled' para logs."""
        if not self.enabled:
            return "disabled"
        cls = type(self)
        with cls._class_lock:
            now = time.time()
            if now < cls._class_degraded_until:
                return f"degraded({int(cls._class_degraded_until - now)}s)"
            pending = cls._class_pending_future
            if pending is not None and not pending.done():
                return "in_flight"
        return "ok"

    # ------------------------------------------------------------------ #
    # Internals
    # ------------------------------------------------------------------ #
    @classmethod
    def _on_request_done(cls, future: Future) -> None:
        """Callback que corre en el thread del executor cuando termina la petición.

        - Limpia _class_pending_future (solo si sigue siendo este future).
        - Si la petición fue exitosa y había cooldown activo, lo levanta:
          la respuesta tardía es confirmación de que Gemini está saludable.
        """
        with cls._class_lock:
            if cls._class_pending_future is not future:
                # Otro future ya tomó el lugar (caso raro de race) — no tocar.
                return
            elapsed = time.time() - cls._class_pending_started_at
            try:
                exc = future.exception(timeout=0)
            except Exception:
                exc = RuntimeError("future cancelled")
            if exc is None and cls._class_degraded_until > time.time():
                _shared_logger.info(
                    "[Gemini] Respuesta tardía recibida en %.1fs — "
                    "Gemini operativa de nuevo, cooldown levantado",
                    elapsed,
                )
                cls._class_degraded_until = 0.0
            cls._class_pending_future = None

    def _next_usable_slot(self, skip: Optional[set[int]] = None) -> Optional[_KeySlot]:
        """Devuelve el próximo slot usable rotando por orden, saltando los ya intentados."""
        skip = skip or set()
        for _ in range(len(self._slots)):
            slot = self._slots[self._current % len(self._slots)]
            self._current += 1
            if slot.usable and slot.index not in skip:
                return slot
        return None

    def _build_contents(
        self,
        text_part: str,
        image_bytes: Optional[bytes],
        image_mime: str,
    ) -> list:
        prompt = _PROMPT_BY_LANG.get(self.lang, _PROMPT_BY_LANG["es-MX"])
        user_block = f"Publicación a comentar:\n---\n{text_part or '(sin texto)'}\n---"
        parts: list = []
        if image_bytes:
            try:
                parts.append(
                    genai_types.Part.from_bytes(data=image_bytes, mime_type=image_mime)
                )
            except Exception:
                self.logger.debug("[Gemini] Imagen inválida — solo texto", exc_info=True)
        parts.append(f"{prompt}\n\n{user_block}")
        return parts

    @staticmethod
    def _sanitize(text: str) -> Optional[str]:
        if not text:
            return None
        cleaned = text.strip().strip('"').strip("'").strip()
        for line in cleaned.splitlines():
            line = line.strip()
            if line:
                cleaned = line
                break
        if len(cleaned) > 200:
            cleaned = cleaned[:200].rsplit(" ", 1)[0]
        if len(cleaned) < 3:
            return None
        for pat in _BANNED_PATTERNS:
            if pat.search(cleaned):
                return None
        return cleaned
