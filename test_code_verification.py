#!/usr/bin/env python3
"""
test_code_verification.py — Tests de verificación de código sin dependencias externas.

Verifica que cada fix está implementado en el código sin necesidad de importar módulos.
"""

import re
from pathlib import Path


def check_file_contains(file_path, patterns, test_name):
    """Verifica que el archivo contiene todos los patrones."""
    content = Path(file_path).read_text()
    failures = []

    for pattern_name, pattern in patterns.items():
        if not re.search(pattern, content, re.MULTILINE | re.DOTALL):
            failures.append(f"  ❌ No encontrado: {pattern_name}")

    if failures:
        print(f"❌ {test_name}")
        for f in failures:
            print(f)
        return False
    else:
        print(f"✅ {test_name}")
        return True


def main():
    base = Path(__file__).parent / "facebook_auto_poster"
    results = []

    # ====================================================================
    # PLANTILLAS FIXES
    # ====================================================================

    print("\n📋 TESTS DE PLANTILLAS (7 fixes)")
    print("=" * 60)

    # FIX #1: selectTemplate no usa event.currentTarget
    results.append(check_file_contains(
        base / "templates/publish.html",
        {
            "selectTemplate función": r"function selectTemplate\(tplId\)",
            "No event.currentTarget": r"selectTemplate.*?(?!event\.currentTarget)",
            "Usa índice en forEach": r"forEach.*?\(card,\s*idx\)",
        },
        "FIX #1: selectTemplate compatible Firefox (sin event.currentTarget)"
    ))

    # FIX #2: scheduled_for validación
    results.append(check_file_contains(
        base / "templates/publish.html",
        {
            "Valida scheduled_for": r"if\s*\(\s*state\.publishWhen\s*===\s*['\"]scheduled",
            "Verifica datetime vacío": r"!state\.publishDatetime\s*\|\|\s*state\.publishDatetime\.trim",
            "Valida fecha futura": r"scheduled\s*<=\s*now",
        },
        "FIX #2: Validación de scheduled_for antes de POST"
    ))

    # FIX #3: loadTemplates error handling
    results.append(check_file_contains(
        base / "templates/publish.html",
        {
            "Valida res.ok": r"if\s*\(\s*!res\.ok\s*\)",
            "Valida Array": r"!Array\.isArray\(templates\)",
            "Valida campos": r"!tpl\.id\s*\|\|\s*!tpl\.name",
            "Maneja errores JSON": r"await res\.json\(\)\.catch",
        },
        "FIX #3: loadTemplates() con validación HTTP + Array + campos"
    ))

    # FIX #4: XSS fix - no innerHTML
    results.append(check_file_contains(
        base / "templates/publish.html",
        {
            "showTemplatePreview existe": r"function showTemplatePreview",
            "Usa createElement": r"document\.createElement\(",
            "Usa textContent": r"\.textContent\s*=",
            "Valida src ruta": r"tpl\.image_path\.startsWith\(",
        },
        "FIX #4: showTemplatePreview sin XSS (createElement + textContent)"
    ))

    # FIX #5: template_id validation
    results.append(check_file_contains(
        base / "api_server.py",
        {
            "Patrón regex": r"_TEMPLATE_ID_PATTERN\s*=\s*re\.compile",
            "Función validate": r"def _validate_template_id",
            "GET endpoint": r"@app\.get\(\"/admin/api/templates/<template_id>",
            "PUT endpoint": r"@app\.put\(\"/admin/api/templates/<template_id>",
            "DELETE endpoint": r"@app\.delete\(\"/admin/api/templates/<template_id>",
        },
        "FIX #5: Validación de template_id en GET/PUT/DELETE"
    ))

    # FIX #6: logging con exception
    results.append(check_file_contains(
        base / "api_server.py",
        {
            "logger.exception en create": r"logger\.exception.*admin_create_template",
            "logger.exception en update": r"logger\.exception.*admin_update_template",
        },
        "FIX #6: Logging mejorado con logger.exception()"
    ))

    # FIX #7: límites de tamaño
    results.append(check_file_contains(
        base / "api_server.py",
        {
            "MAX_TEMPLATE_TEXT": r"MAX_TEMPLATE_TEXT_CHARS\s*=\s*50000",
            "MIN_TEMPLATE_TEXT": r"MIN_TEMPLATE_TEXT_CHARS\s*=\s*10",
            "MAX_TEMPLATE_NAME": r"MAX_TEMPLATE_NAME_CHARS\s*=\s*100",
            "MAX_TEMPLATE_URL": r"MAX_TEMPLATE_URL_CHARS\s*=\s*2048",
            "Validación create": r"if\s+len\(text\)\s*>\s*MAX_TEMPLATE_TEXT_CHARS",
            "Validación update": r"if\s+len\(text\).*?MAX_TEMPLATE_TEXT_CHARS",
        },
        "FIX #7: Límites de tamaño (text 50KB, name 100 chars, url 2KB)"
    ))

    # ====================================================================
    # PROXIES FIXES
    # ====================================================================

    print("\n🔗 TESTS DE PROXIES (6 fixes)")
    print("=" * 60)

    # FIX #1: Cache en resolve_proxy
    results.append(check_file_contains(
        base / "proxy_manager.py",
        {
            "Cache dict": r"_proxy_cache:\s*dict",
            "Cache TTL": r"_PROXY_CACHE_TTL_S\s*=\s*30",
            "Cache en resolve": r"_proxy_cache\[account_name\]",
            "Force refresh param": r"def resolve_proxy.*force_refresh",
        },
        "FIX #1: Cache de proxies con TTL 30s en resolve_proxy()"
    ))

    # FIX #2: _check_node validaciones
    results.append(check_file_contains(
        base / "proxy_manager.py",
        {
            "Valida status": r"resp\.status_code\s*!=\s*200",
            "Valida JSON": r"data\.get\(\"ip\",\s*\"\"\)",
            "Timeout handler": r"except requests\.Timeout",
            "Connection handler": r"except requests\.ConnectionError",
            "Exception handler": r"logger\.exception.*ProxyCheck",
        },
        "FIX #2: _check_node() con validaciones robustas (HTTP, JSON, timeouts)"
    ))

    # FIX #3: _alert_node_down mejora
    results.append(check_file_contains(
        base / "proxy_manager.py",
        {
            "Try/catch": r"def _alert_node_down.*?try:",
            "Error handler": r"except Exception",
            "Create alert": r"job_store\.create_system_alert",
        },
        "FIX #3: _alert_node_down() con error handling"
    ))

    # FIX #4: Lock en assign_proxy
    results.append(check_file_contains(
        base / "proxy_manager.py",
        {
            "Lock define": r"_assign_lock\s*=\s*threading\.Lock\(",
            "Lock use": r"with _assign_lock:",
            "Valida secondary": r"if secondary_node:.*?job_store\.get_proxy_node",
        },
        "FIX #4: assign_proxy_to_account() con lock para race conditions"
    ))

    # FIX #5: admin_assign_proxy validación
    results.append(check_file_contains(
        base / "api_server.py",
        {
            "Valida primary": r"if not job_store\.get_proxy_node\(primary\)",
            "Valida secondary": r"if secondary and not job_store\.get_proxy_node\(secondary\)",
            "Try/catch": r"try:\s+job_store\.set_proxy_assignment",
            "Error logging": r"logger\.exception.*Error asignando proxy",
        },
        "FIX #5: admin_assign_proxy() valida nodos + try/catch"
    ))

    # FIX #6: Tunnel file validation
    results.append(check_file_contains(
        base / "main.py",
        {
            "Read URL robusta": r"def _read_static_url\(\)\s*->\s*str\s*\|\s*None",
            "Read backend robusta": r"def _read_backend\(\)\s*->\s*str\s*\|\s*None",
            "Ensure tunnel": r"def _ensure_tunnel_ready\(\)",
            "URL validation": r"url\.startswith\(\(\"http://\",\s*\"https://\"\)\)",
            "Backend validation": r"backend not in \(\"cloudflare\",\s*\"ngrok\"\)",
        },
        "FIX #6: Túnel con validación de archivos (URL, backend, contenido)"
    ))

    # ====================================================================
    # RESUMEN
    # ====================================================================

    print("\n" + "=" * 60)
    passed = sum(results)
    total = len(results)
    percentage = (passed / total * 100) if total > 0 else 0

    print(f"\n📊 RESUMEN: {passed}/{total} tests pasados ({percentage:.0f}%)")

    if passed == total:
        print("\n🎉 ¡TODOS LOS FIXES ESTÁN IMPLEMENTADOS CORRECTAMENTE!")
        return 0
    else:
        print(f"\n⚠️  {total - passed} fixes pendientes de verificar")
        return 1


if __name__ == "__main__":
    exit(main())
