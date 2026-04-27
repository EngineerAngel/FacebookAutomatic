"""
test_fixes.py — Tests para verificar que los 13 fixes se implementaron correctamente.

Cobertura:
- FIX #1-4 (Plantillas): validación, error handling, selectTemplate, XSS
- FIX #5-7 (Plantillas): template_id validation, logging, límites
- FIX #1-6 (Proxies): cache, health checker, race conditions, túnel
"""

import json
import logging
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

# Importar módulos a testear
sys.path.insert(0, str(Path(__file__).parent / "facebook_auto_poster"))

import proxy_manager
import job_store

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


class TestPlantillasFixes(unittest.TestCase):
    """Tests para los 7 fixes de plantillas."""

    def test_plantillas_fix5_template_id_validation(self):
        """FIX #5: Validar que _validate_template_id funciona."""
        # Importar desde api_server
        sys.path.insert(0, str(Path(__file__).parent / "facebook_auto_poster"))
        from api_server import _validate_template_id

        # IDs válidos (12 hex)
        self.assertTrue(_validate_template_id("0123456789ab"))
        self.assertTrue(_validate_template_id("ffffffffffffffff"[:12]))

        # IDs inválidos
        self.assertFalse(_validate_template_id("123"))  # muy corto
        self.assertFalse(_validate_template_id("0123456789ab "))  # espacio
        self.assertFalse(_validate_template_id("ABCDEFGH1234"))  # mayúscula
        self.assertFalse(_validate_template_id(""))  # vacío
        self.assertFalse(_validate_template_id("gggggggggggg"))  # caracteres inválidos

        logger.info("✅ FIX #5: Validación de template_id correcta")

    def test_plantillas_fix7_size_limits(self):
        """FIX #7: Verificar constantes de límite de tamaño."""
        from api_server import (
            MAX_TEMPLATE_TEXT_CHARS,
            MAX_TEMPLATE_NAME_CHARS,
            MIN_TEMPLATE_TEXT_CHARS,
            MAX_TEMPLATE_URL_CHARS,
        )

        # Verificar que las constantes existen y tienen valores razonables
        self.assertEqual(MAX_TEMPLATE_TEXT_CHARS, 50000)
        self.assertEqual(MAX_TEMPLATE_NAME_CHARS, 100)
        self.assertEqual(MIN_TEMPLATE_TEXT_CHARS, 10)
        self.assertEqual(MAX_TEMPLATE_URL_CHARS, 2048)

        logger.info("✅ FIX #7: Límites de tamaño configurados correctamente")

    def test_plantillas_fix1_selecttemplate_no_event(self):
        """FIX #1: Verificar que selectTemplate no usa event.currentTarget."""
        # Leer el archivo HTML y verificar que no usa event.currentTarget
        publish_html = Path(__file__).parent / "facebook_auto_poster/templates/publish.html"
        content = publish_html.read_text()

        # Contar ocurrencias de event.currentTarget en selectTemplate
        lines = content.split('\n')
        in_select_template = False
        event_current_found = False

        for i, line in enumerate(lines):
            if "function selectTemplate" in line:
                in_select_template = True
            elif in_select_template and "function " in line and "selectTemplate" not in line:
                in_select_template = False
            elif in_select_template and "event.currentTarget" in line:
                event_current_found = True
                break

        self.assertFalse(event_current_found, "selectTemplate aún usa event.currentTarget")
        logger.info("✅ FIX #1: selectTemplate no usa event.currentTarget")

    def test_plantillas_fix2_scheduled_validation(self):
        """FIX #2: Verificar que publish() valida scheduled_for."""
        publish_html = Path(__file__).parent / "facebook_auto_poster/templates/publish.html"
        content = publish_html.read_text()

        # Buscar la validación de scheduled_for
        self.assertIn("state.publishWhen === 'scheduled'", content)
        self.assertIn("if (!state.publishDatetime", content)
        self.assertIn("La fecha y hora deben ser en el futuro", content)

        logger.info("✅ FIX #2: Validación de scheduled_for presente")

    def test_plantillas_fix3_loadtemplates_error_handling(self):
        """FIX #3: Verificar que loadTemplates() valida res.ok."""
        publish_html = Path(__file__).parent / "facebook_auto_poster/templates/publish.html"
        content = publish_html.read_text()

        # Buscar validaciones en loadTemplates
        self.assertIn("if (!res.ok)", content)
        self.assertIn("!Array.isArray(templates)", content)
        self.assertIn("!tpl.id || !tpl.name", content)

        logger.info("✅ FIX #3: loadTemplates() valida HTTP + Array + campos")

    def test_plantillas_fix4_xss_no_innerhtml(self):
        """FIX #4: Verificar que showTemplatePreview no usa innerHTML."""
        publish_html = Path(__file__).parent / "facebook_auto_poster/templates/publish.html"
        content = publish_html.read_text()

        # Buscar la función showTemplatePreview
        lines = content.split('\n')
        in_preview = False
        innerhtml_found = False

        for i, line in enumerate(lines):
            if "function showTemplatePreview" in line:
                in_preview = True
            elif in_preview and "function " in line and "showTemplatePreview" not in line:
                in_preview = False
            elif in_preview and "body.innerHTML = html" in line:
                innerhtml_found = True
                break

        self.assertFalse(innerhtml_found, "showTemplatePreview aún usa innerHTML")

        # Verificar que usa createElement
        self.assertIn("document.createElement('div')", content)
        self.assertIn("textContent =", content)

        logger.info("✅ FIX #4: showTemplatePreview usa createElement + textContent")


class TestProxiesFixes(unittest.TestCase):
    """Tests para los 6 fixes de proxies."""

    def test_proxies_fix1_cache_exists(self):
        """FIX #1: Verificar que _proxy_cache existe en proxy_manager."""
        self.assertTrue(hasattr(proxy_manager, '_proxy_cache'))
        self.assertTrue(hasattr(proxy_manager, '_PROXY_CACHE_TTL_S'))
        self.assertEqual(proxy_manager._PROXY_CACHE_TTL_S, 30)

        logger.info("✅ FIX #1: Cache proxy con TTL configurado")

    def test_proxies_fix1_resolve_proxy_has_cache(self):
        """FIX #1: Verificar que resolve_proxy() usa cache."""
        import inspect

        source = inspect.getsource(proxy_manager.resolve_proxy)
        self.assertIn("_proxy_cache", source)
        self.assertIn("force_refresh", source)
        self.assertIn("_PROXY_CACHE_TTL_S", source)

        logger.info("✅ FIX #1: resolve_proxy() implementa cache")

    def test_proxies_fix2_check_node_validations(self):
        """FIX #2: Verificar que _check_node() valida JSON y HTTP."""
        import inspect

        source = inspect.getsource(proxy_manager._check_node)
        self.assertIn("resp.status_code != 200", source)
        self.assertIn("json_err", source)
        self.assertIn("requests.Timeout", source)
        self.assertIn("requests.ConnectionError", source)

        logger.info("✅ FIX #2: _check_node() con validaciones robustas")

    def test_proxies_fix3_alert_error_handling(self):
        """FIX #3: Verificar que _alert_node_down() tiene try/catch."""
        import inspect

        source = inspect.getsource(proxy_manager._alert_node_down)
        self.assertIn("try:", source)
        self.assertIn("except Exception", source)

        logger.info("✅ FIX #3: _alert_node_down() con error handling")

    def test_proxies_fix4_assign_lock_exists(self):
        """FIX #4: Verificar que _assign_lock existe."""
        self.assertTrue(hasattr(proxy_manager, '_assign_lock'))

        logger.info("✅ FIX #4: Lock para assign_proxy_to_account() presente")

    def test_proxies_fix4_assign_uses_lock(self):
        """FIX #4: Verificar que assign_proxy_to_account() usa lock."""
        import inspect

        source = inspect.getsource(proxy_manager.assign_proxy_to_account)
        self.assertIn("_assign_lock", source)
        self.assertIn("with _assign_lock", source)

        logger.info("✅ FIX #4: assign_proxy_to_account() usa lock")

    def test_proxies_fix5_endpoint_validation(self):
        """FIX #5: Verificar que admin_assign_proxy() valida nodos."""
        sys.path.insert(0, str(Path(__file__).parent / "facebook_auto_poster"))
        from api_server import admin_assign_proxy
        import inspect

        source = inspect.getsource(admin_assign_proxy)
        self.assertIn("job_store.get_proxy_node(primary)", source)
        self.assertIn("job_store.get_proxy_node(secondary)", source)
        self.assertIn("try:", source)

        logger.info("✅ FIX #5: admin_assign_proxy() valida nodos")

    def test_proxies_fix6_tunnel_functions(self):
        """FIX #6: Verificar que _read_static_url() y _read_backend() son robustos."""
        from facebook_auto_poster.main import _read_static_url, _read_backend, _ensure_tunnel_ready
        import inspect

        # Verificar que _read_static_url retorna Optional
        source_url = inspect.getsource(_read_static_url)
        self.assertIn("return None", source_url)
        self.assertIn("not url.startswith", source_url)

        # Verificar que _read_backend retorna Optional
        source_backend = inspect.getsource(_read_backend)
        self.assertIn("return None", source_backend)
        self.assertIn("not in ('cloudflare', 'ngrok')", source_backend)

        # Verificar que _ensure_tunnel_ready existe
        source_ensure = inspect.getsource(_ensure_tunnel_ready)
        self.assertIn("_read_static_url()", source_ensure)
        self.assertIn("_read_backend()", source_ensure)

        logger.info("✅ FIX #6: Tunnel functions con validaciones robustas")


class TestImports(unittest.TestCase):
    """Verificar que todos los módulos importan correctamente."""

    def test_api_server_imports(self):
        """Verificar que api_server.py compila sin errores."""
        try:
            from api_server import (
                _validate_template_id,
                MAX_TEMPLATE_TEXT_CHARS,
                admin_list_templates,
                admin_create_template,
            )
            logger.info("✅ api_server.py importa correctamente")
        except Exception as e:
            self.fail(f"api_server.py no importa: {e}")

    def test_proxy_manager_imports(self):
        """Verificar que proxy_manager.py compila sin errores."""
        try:
            from proxy_manager import (
                resolve_proxy,
                assign_proxy_to_account,
                _check_node,
                _alert_node_down,
            )
            logger.info("✅ proxy_manager.py importa correctamente")
        except Exception as e:
            self.fail(f"proxy_manager.py no importa: {e}")

    def test_main_imports(self):
        """Verificar que main.py compila sin errores."""
        try:
            from facebook_auto_poster.main import (
                _read_static_url,
                _read_backend,
                _ensure_tunnel_ready,
            )
            logger.info("✅ main.py importa correctamente")
        except Exception as e:
            self.fail(f"main.py no importa: {e}")


class TestIntegration(unittest.TestCase):
    """Tests de integración básicos."""

    def test_proxy_cache_ttl_value(self):
        """Verificar que el TTL del cache es razonable."""
        self.assertGreater(proxy_manager._PROXY_CACHE_TTL_S, 0)
        self.assertLess(proxy_manager._PROXY_CACHE_TTL_S, 300)  # < 5 minutos

        logger.info("✅ Cache TTL es razonable")

    def test_template_constants_consistency(self):
        """Verificar que las constantes de plantillas son consistentes."""
        from api_server import (
            MAX_TEMPLATE_TEXT_CHARS,
            MIN_TEMPLATE_TEXT_CHARS,
            MAX_TEMPLATE_NAME_CHARS,
        )

        # Min debe ser menor que Max
        self.assertLess(MIN_TEMPLATE_TEXT_CHARS, MAX_TEMPLATE_TEXT_CHARS)
        self.assertGreater(MAX_TEMPLATE_NAME_CHARS, 0)

        logger.info("✅ Constantes de plantillas son consistentes")


def run_all_tests():
    """Ejecutar todos los tests."""
    loader = unittest.TestLoader()
    suite = unittest.TestSuite()

    # Agregar todos los tests
    suite.addTests(loader.loadTestsFromTestCase(TestPlantillasFixes))
    suite.addTests(loader.loadTestsFromTestCase(TestProxiesFixes))
    suite.addTests(loader.loadTestsFromTestCase(TestImports))
    suite.addTests(loader.loadTestsFromTestCase(TestIntegration))

    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)

    return result.wasSuccessful()


if __name__ == "__main__":
    success = run_all_tests()
    sys.exit(0 if success else 1)
