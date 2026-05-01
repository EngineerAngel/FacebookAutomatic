"""
test_logging_config.py — Tests ancla para logging_config (Fase 3.3a).

Verifica que:
- Modo texto (flag OFF) produce salida clásica, no JSON.
- Modo estructurado (flag ON) produce JSON válido con los campos requeridos.
- bind_account inyecta el campo 'account' en los logs estructurados.
- bind_account/unbind_account son no-ops cuando el flag está OFF.
- get_formatter devuelve el formatter correcto según el flag activo.
"""

import importlib
import io
import json
import logging
import sys

import pytest


# ---------------------------------------------------------------------------
# Fixture: resetea logging_config entre tests para evitar estado compartido
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def reset_logging_config():
    """Recarga logging_config y resetea el root logger entre tests."""
    import logging_config
    # Guardar estado original
    original_structured = logging_config._structured
    root = logging.getLogger()
    original_handlers = root.handlers[:]
    original_level = root.level

    yield

    # Restaurar estado
    logging_config._structured = original_structured
    root.handlers = original_handlers
    root.level = original_level

    # Resetear structlog si fue configurado
    try:
        import structlog
        structlog.reset_defaults()
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _capture_log(tmp_path, structured: bool, message: str, **kwargs) -> str:
    """Configura logging con el flag indicado y captura la salida en un buffer."""
    import logging_config

    buf = io.StringIO()
    logging_config.setup_logging(
        structured=structured,
        log_dir=tmp_path,
        file_level=logging.DEBUG,
        console_level=logging.DEBUG,
    )

    # Reemplazar el StreamHandler del root por uno que escriba al buffer
    root = logging.getLogger()
    for h in root.handlers:
        if isinstance(h, logging.StreamHandler) and not isinstance(h, logging.FileHandler):
            h.stream = buf
            break

    logger = logging.getLogger("test.capture")
    logger.info(message, **kwargs)

    return buf.getvalue()


# ---------------------------------------------------------------------------
# Modo texto (flag OFF)
# ---------------------------------------------------------------------------

def test_text_mode_produces_plain_text(tmp_path):
    output = _capture_log(tmp_path, structured=False, message="hola mundo")
    assert "hola mundo" in output
    # No debe ser JSON
    assert not output.strip().startswith("{")


def test_text_mode_includes_level(tmp_path):
    output = _capture_log(tmp_path, structured=False, message="nivel test")
    assert "INFO" in output


def test_text_mode_includes_logger_name(tmp_path):
    output = _capture_log(tmp_path, structured=False, message="nombre logger")
    assert "test.capture" in output


# ---------------------------------------------------------------------------
# Modo estructurado (flag ON)
# ---------------------------------------------------------------------------

def test_structured_mode_produces_valid_json(tmp_path):
    output = _capture_log(tmp_path, structured=True, message="evento json")
    line = output.strip()
    assert line, "No hubo salida de log"
    data = json.loads(line)  # lanza si no es JSON válido
    assert isinstance(data, dict)


def test_structured_mode_has_required_fields(tmp_path):
    output = _capture_log(tmp_path, structured=True, message="campos requeridos")
    data = json.loads(output.strip())
    assert "event" in data
    assert "level" in data
    assert "timestamp" in data


def test_structured_mode_event_contains_message(tmp_path):
    output = _capture_log(tmp_path, structured=True, message="mi evento unico")
    data = json.loads(output.strip())
    assert "mi evento unico" in data["event"]


def test_structured_mode_includes_logger_name(tmp_path):
    output = _capture_log(tmp_path, structured=True, message="con logger")
    data = json.loads(output.strip())
    assert "logger" in data
    assert "test.capture" in data["logger"]


def test_structured_mode_level_is_lowercase(tmp_path):
    output = _capture_log(tmp_path, structured=True, message="nivel lower")
    data = json.loads(output.strip())
    assert data["level"] == "info"


# ---------------------------------------------------------------------------
# bind_account — inyección de contexto
# ---------------------------------------------------------------------------

def test_bind_account_adds_field_when_structured(tmp_path):
    import logging_config

    output = _capture_log(tmp_path, structured=True, message="pre-bind")
    # Sin bind aún, el campo account no debe estar
    data_before = json.loads(output.strip())
    assert "account" not in data_before

    # Ahora bind
    logging_config.bind_account("elena")
    buf = io.StringIO()
    root = logging.getLogger()
    for h in root.handlers:
        if isinstance(h, logging.StreamHandler) and not isinstance(h, logging.FileHandler):
            h.stream = buf
            break
    logging.getLogger("test.bind").info("post-bind")
    data_after = json.loads(buf.getvalue().strip())

    assert data_after.get("account") == "elena"

    logging_config.unbind_account()


def test_bind_account_is_noop_when_not_structured(tmp_path):
    import logging_config

    # Flag OFF — bind no debe lanzar ni importar structlog
    logging_config._structured = False
    try:
        logging_config.bind_account("carlos")
        logging_config.unbind_account()
    except Exception as exc:
        pytest.fail(f"bind_account lanzó excepción con flag OFF: {exc}")


def test_unbind_account_clears_context(tmp_path):
    import logging_config

    _capture_log(tmp_path, structured=True, message="setup")
    logging_config.bind_account("rosa")
    logging_config.unbind_account()

    buf = io.StringIO()
    root = logging.getLogger()
    for h in root.handlers:
        if isinstance(h, logging.StreamHandler) and not isinstance(h, logging.FileHandler):
            h.stream = buf
            break
    logging.getLogger("test.unbind").info("after unbind")
    line = buf.getvalue().strip()
    if line:
        data = json.loads(line)
        assert "account" not in data


# ---------------------------------------------------------------------------
# get_formatter
# ---------------------------------------------------------------------------

def test_get_formatter_returns_text_when_flag_off(tmp_path):
    import logging_config
    logging_config._structured = False
    fmt = logging_config.get_formatter()
    assert isinstance(fmt, logging.Formatter)
    # El formato texto tiene asctime
    assert "asctime" in fmt._fmt


def test_get_formatter_returns_structlog_formatter_when_flag_on(tmp_path):
    import logging_config
    import structlog

    _capture_log(tmp_path, structured=True, message="activar flag")
    fmt = logging_config.get_formatter()
    assert isinstance(fmt, structlog.stdlib.ProcessorFormatter)


# ---------------------------------------------------------------------------
# Creación de archivo de log
# ---------------------------------------------------------------------------

def test_setup_logging_creates_log_file(tmp_path):
    import logging_config
    logging_config.setup_logging(structured=False, log_dir=tmp_path)
    assert (tmp_path / "main.log").exists()


def test_setup_logging_writes_to_file(tmp_path):
    import logging_config
    logging_config.setup_logging(structured=False, log_dir=tmp_path)
    logging.getLogger("test.file").warning("escrito en archivo")
    content = (tmp_path / "main.log").read_text(encoding="utf-8")
    assert "escrito en archivo" in content
