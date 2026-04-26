"""
test_text_variation.py — Tests ancla para text_variation.

Cubre:
  - _strip_wrapping: función pura, sin dependencias
  - TextVariator.variate: lógica de caché + fallback al original
"""

import logging
from unittest.mock import MagicMock, patch

import pytest

from text_variation import TextVariator, _strip_wrapping


# ---------------------------------------------------------------------------
# _strip_wrapping — función pura
# ---------------------------------------------------------------------------

def test_strip_wrapping_no_quotes():
    assert _strip_wrapping("texto limpio") == "texto limpio"


def test_strip_wrapping_double_quotes():
    assert _strip_wrapping('"texto entre comillas"') == "texto entre comillas"


def test_strip_wrapping_single_quotes():
    assert _strip_wrapping("'texto entre comillas simples'") == "texto entre comillas simples"


def test_strip_wrapping_nested_double():
    # Dos capas de comillas se deben quitar
    assert _strip_wrapping('""doble capa""') == "doble capa"


def test_strip_wrapping_preserves_inner_quotes():
    # Comilla interna no envolvente no se toca
    result = _strip_wrapping('"precio "especial" hoy"')
    assert "especial" in result


def test_strip_wrapping_whitespace():
    assert _strip_wrapping("  texto con espacios  ") == "texto con espacios"


def test_strip_wrapping_empty():
    assert _strip_wrapping("") == ""


def test_strip_wrapping_only_quotes():
    # Solo comillas: quita las envolventes, queda vacío
    result = _strip_wrapping('""')
    assert result == ""


# ---------------------------------------------------------------------------
# TextVariator — lógica de caché y fallback
# ---------------------------------------------------------------------------

def _make_variator(gemini_response: str | None = None, cache_enabled: bool = True):
    """Factoría de TextVariator con mocks de Gemini y logger."""
    mock_gemini = MagicMock()
    mock_gemini.enabled = True
    mock_gemini.generate_text.return_value = gemini_response
    logger = logging.getLogger("test_variator")
    return TextVariator(gemini=mock_gemini, logger=logger, cache_enabled=cache_enabled), mock_gemini


def test_variate_empty_text_returns_empty(tmp_db):
    import job_store
    variator, _ = _make_variator()
    assert variator.variate("", "cuenta1", "111") == ""


def test_variate_whitespace_returns_whitespace(tmp_db):
    import job_store
    variator, _ = _make_variator()
    result = variator.variate("   ", "cuenta1", "111")
    assert result == "   "


def test_variate_cache_hit_returns_cached(tmp_db, monkeypatch):
    import job_store
    monkeypatch.setattr(job_store, "get_text_variation", lambda key, ttl: "texto cacheado")
    monkeypatch.setattr(job_store, "save_text_variation", lambda k, h, v: None)

    variator, mock_gemini = _make_variator()
    result = variator.variate("texto original", "cuenta1", "111")

    assert result == "texto cacheado"
    mock_gemini.generate_text.assert_not_called()


def test_variate_gemini_disabled_returns_original(tmp_db, monkeypatch):
    import job_store
    monkeypatch.setattr(job_store, "get_text_variation", lambda key, ttl: None)
    monkeypatch.setattr(job_store, "save_text_variation", lambda k, h, v: None)

    variator, mock_gemini = _make_variator()
    mock_gemini.enabled = False

    result = variator.variate("texto original largo", "cuenta1", "111")
    assert result == "texto original largo"


def test_variate_gemini_returns_none_fallback(tmp_db, monkeypatch):
    import job_store
    monkeypatch.setattr(job_store, "get_text_variation", lambda key, ttl: None)
    monkeypatch.setattr(job_store, "save_text_variation", lambda k, h, v: None)

    variator, _ = _make_variator(gemini_response=None)
    result = variator.variate("texto original para fallback", "cuenta1", "111")
    assert result == "texto original para fallback"


def test_variate_gemini_short_response_fallback(tmp_db, monkeypatch):
    """Respuesta < 20 chars se considera inválida — devuelve original."""
    import job_store
    monkeypatch.setattr(job_store, "get_text_variation", lambda key, ttl: None)
    monkeypatch.setattr(job_store, "save_text_variation", lambda k, h, v: None)

    variator, _ = _make_variator(gemini_response="corto")
    result = variator.variate("texto original suficientemente largo", "cuenta1", "111")
    assert result == "texto original suficientemente largo"


def test_variate_valid_gemini_response(tmp_db, monkeypatch):
    import job_store
    saved = {}
    monkeypatch.setattr(job_store, "get_text_variation", lambda key, ttl: None)
    monkeypatch.setattr(
        job_store, "save_text_variation",
        lambda k, h, v: saved.update({"key": k, "value": v})
    )

    respuesta = "Oferta increíble en departamentos con todas las amenidades. Llama ahora."
    variator, _ = _make_variator(gemini_response=respuesta)
    result = variator.variate("Depto en venta, llama ya.", "cuenta1", "111")

    assert result == respuesta
    assert "key" in saved  # se guardó en caché


def test_variate_strips_quotes_from_gemini(tmp_db, monkeypatch):
    """Respuesta de Gemini envuelta en comillas se limpian antes de devolver."""
    import job_store
    monkeypatch.setattr(job_store, "get_text_variation", lambda key, ttl: None)
    monkeypatch.setattr(job_store, "save_text_variation", lambda k, h, v: None)

    respuesta_con_comillas = '"Gran oportunidad de inversión en bienes raíces de lujo hoy."'
    variator, _ = _make_variator(gemini_response=respuesta_con_comillas)
    result = variator.variate("Depto en venta, gran oportunidad.", "cuenta1", "222")

    assert not result.startswith('"')
    assert not result.endswith('"')


def test_variate_cache_disabled_always_calls_gemini(tmp_db, monkeypatch):
    import job_store
    monkeypatch.setattr(job_store, "get_text_variation", lambda key, ttl: "no debería leerse")
    monkeypatch.setattr(job_store, "save_text_variation", lambda k, h, v: None)

    respuesta = "Variación fresca generada por Gemini para este anuncio ahora."
    variator, mock_gemini = _make_variator(
        gemini_response=respuesta, cache_enabled=False
    )
    result = variator.variate("Anuncio original de prueba aquí.", "cuenta1", "333")

    mock_gemini.generate_text.assert_called_once()
    assert result == respuesta
