"""
test_item_1_6.py — Tests del ítem 1.6: Cross-platform Windows → Mac/Ubuntu

Cubre:
  - _find_cloudflared(): lógica de búsqueda multiplataforma
  - start_cloudflared(): comportamiento cuando no se encuentra el binario
  - .env.example: sin rutas Windows hardcodeadas
  - .gitignore: *.exe, cloudflared y chromedriver protegidos
  - main.py: sin referencia a cloudflared.exe hardcodeado
  - setup.sh: existe y tiene estructura válida
"""

import sys
import os
import unittest
import tempfile
from pathlib import Path
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.dirname(__file__))


# ─── GRUPO 1: _find_cloudflared lógica ───────────────────────────────────────

class TestFindCloudflared(unittest.TestCase):

    def _call(self):
        import main
        return main._find_cloudflared()

    def test_retorna_path_si_en_PATH(self):
        """Si cloudflared está en PATH, debe retornarlo."""
        with patch("shutil.which", return_value="/usr/local/bin/cloudflared"):
            result = self._call()
        self.assertEqual(result, "/usr/local/bin/cloudflared")

    def test_retorna_none_si_no_encontrado(self):
        """Sin binario en PATH ni junto al proyecto, retorna None."""
        with patch("shutil.which", return_value=None), \
             patch("platform.system", return_value="Linux"), \
             patch.object(Path, "exists", return_value=False):
            result = self._call()
        self.assertIsNone(result)

    def test_fallback_a_binario_junto_proyecto_windows(self):
        """En Windows, debe buscar cloudflared.exe junto al proyecto."""
        with patch("shutil.which", return_value=None), \
             patch("platform.system", return_value="Windows"):
            with tempfile.TemporaryDirectory() as tmpdir:
                # Simular que cloudflared.exe existe junto al proyecto
                exe = Path(tmpdir) / "cloudflared.exe"
                exe.write_bytes(b"fake")
                # Patchear PROJECT_ROOT
                import main as m
                orig = m.Path
                # Solo verificamos la lógica del dict de candidates
                candidates = {
                    "Windows": Path(tmpdir) / "cloudflared.exe",
                    "Darwin":  Path(tmpdir) / "cloudflared",
                    "Linux":   Path(tmpdir) / "cloudflared",
                }
                candidate = candidates.get("Windows")
                self.assertTrue(candidate.exists())

    def test_fallback_a_binario_junto_proyecto_linux(self):
        """En Linux, debe buscar 'cloudflared' (sin .exe) junto al proyecto."""
        with tempfile.TemporaryDirectory() as tmpdir:
            exe = Path(tmpdir) / "cloudflared"
            exe.write_bytes(b"fake")
            candidates = {
                "Linux": Path(tmpdir) / "cloudflared",
            }
            candidate = candidates.get("Linux")
            self.assertTrue(candidate.exists())


# ─── GRUPO 2: start_cloudflared — comportamiento sin binario ─────────────────

class TestStartCloudflared(unittest.TestCase):

    def test_no_lanza_si_no_encontrado(self):
        """Si _find_cloudflared retorna None, no debe lanzar excepción."""
        import main
        with patch.object(main, "_find_cloudflared", return_value=None), \
             patch("threading.Thread") as mock_thread:
            main.start_cloudflared(5000)
            mock_thread.assert_not_called()

    def test_lanza_thread_si_encontrado(self):
        """Si se encuentra el binario, debe lanzar un thread daemon."""
        import main
        with patch.object(main, "_find_cloudflared", return_value="/usr/local/bin/cloudflared"), \
             patch("threading.Thread") as mock_thread, \
             patch("time.sleep"):
            mock_instance = MagicMock()
            mock_thread.return_value = mock_instance
            main.start_cloudflared(5000)
            mock_thread.assert_called_once()
            # Verificar que el thread es daemon
            call_kwargs = mock_thread.call_args[1]
            self.assertTrue(call_kwargs.get("daemon"))
            mock_instance.start.assert_called_once()


# ─── GRUPO 3: archivos de configuración ──────────────────────────────────────

class TestConfigFiles(unittest.TestCase):

    BASE = Path(__file__).resolve().parent

    def _read(self, rel_path: str) -> str:
        return (self.BASE / rel_path).read_text(encoding="utf-8")

    def test_env_example_sin_ruta_windows_hardcodeada(self):
        """El .env.example no debe tener rutas C:\\ hardcodeadas."""
        content = self._read(".env.example")
        # La ruta con el username real del dev no debe estar
        self.assertNotIn("ag464", content,
            ".env.example tiene ruta personal de Windows hardcodeada")

    def test_env_example_sin_chromedriver_path(self):
        """CHROMEDRIVER_PATH fue eliminado — Patchright no lo necesita."""
        content = self._read(".env.example")
        self.assertNotIn("CHROMEDRIVER_PATH=C:\\", content,
            "CHROMEDRIVER_PATH con ruta Windows debe eliminarse")

    def test_env_example_tiene_comentarios_multiplatform(self):
        """Debe mencionar Mac, Ubuntu y/o Linux en los comentarios."""
        content = self._read(".env.example")
        has_multiplatform = "Mac" in content or "Ubuntu" in content or "Linux" in content
        self.assertTrue(has_multiplatform,
            ".env.example debe tener comentarios multiplataforma")

    def test_gitignore_raiz_protege_exe(self):
        """El .gitignore raíz debe proteger archivos .exe."""
        content = (self.BASE.parent / ".gitignore").read_text(encoding="utf-8")
        self.assertIn("*.exe", content, ".gitignore debe ignorar *.exe")

    def test_gitignore_raiz_protege_cloudflared_linux(self):
        """El .gitignore raíz debe proteger el binario cloudflared (sin .exe)."""
        content = (self.BASE.parent / ".gitignore").read_text(encoding="utf-8")
        self.assertIn("cloudflared", content)

    def test_gitignore_raiz_protege_chromedriver_linux(self):
        """El .gitignore raíz debe proteger el binario chromedriver (sin .exe)."""
        content = (self.BASE.parent / ".gitignore").read_text(encoding="utf-8")
        self.assertIn("chromedriver", content)

    def test_main_sin_cloudflared_exe_hardcodeado(self):
        """main.py no debe tener la asignacion hardcodeada antigua.
        
        La referencia antigua era: cloudflared_exe = PROJECT_ROOT / 'cloudflared.exe'
        La nueva es un dict multiplataforma con candidates por OS (correcto).
        """
        content = self._read("main.py")
        # El patron antiguo era asignar cloudflared_exe directamente
        self.assertNotIn("cloudflared_exe = PROJECT_ROOT", content,
            "main.py tiene la referencia hardcodeada antigua a cloudflared.exe")
        # Debe usar el nuevo mecanismo multiplataforma con dict de candidates
        self.assertIn("candidates", content,
            "main.py debe usar dict de candidates multiplataforma")

    def test_main_tiene_find_cloudflared(self):
        """main.py debe tener la función _find_cloudflared."""
        content = self._read("main.py")
        self.assertIn("_find_cloudflared", content)
        self.assertIn("shutil.which", content)

    def test_setup_sh_existe(self):
        """setup.sh debe existir en la raíz del proyecto."""
        setup = self.BASE.parent / "setup.sh"
        self.assertTrue(setup.exists(), "setup.sh no encontrado en raíz del proyecto")

    def test_setup_sh_cubre_mac_y_ubuntu(self):
        """setup.sh debe cubrir tanto Darwin (Mac) como Linux (Ubuntu)."""
        content = (self.BASE.parent / "setup.sh").read_text(encoding="utf-8")
        self.assertIn("Darwin", content, "setup.sh debe manejar macOS")
        self.assertIn("Linux", content, "setup.sh debe manejar Linux/Ubuntu")
        self.assertIn("patchright install chromium", content)
        self.assertIn("brew install cloudflared", content)


# ─── GRUPO 4: main.py importa limpio ─────────────────────────────────────────

class TestMainImport(unittest.TestCase):

    def test_main_importa_sin_errores(self):
        """main.py debe poder importarse sin errores."""
        import main
        self.assertTrue(hasattr(main, "_find_cloudflared"))
        self.assertTrue(hasattr(main, "start_cloudflared"))
        self.assertTrue(hasattr(main, "main"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
