"""
tests/unit/test_adaptive_selector.py — Unit tests for AdaptivePlaywrightBridge (Fase 3.4).

These tests use MagicMock — no real browser required.
"""
from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, MagicMock, patch, call
from patchright.async_api import TimeoutError as PatchrightTimeout


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_page(content: str = "<html><body></body></html>", url: str = "https://www.facebook.com/"):
    page = MagicMock()
    page.content = AsyncMock(return_value=content)
    page.url = url
    return page


def _make_locator(visible: bool = True):
    loc = MagicMock()
    if visible:
        loc.wait_for = AsyncMock()
    else:
        loc.wait_for = AsyncMock(side_effect=PatchrightTimeout("timeout"))
    return loc


# ---------------------------------------------------------------------------
# Tests — feature flag OFF (default)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_get_locator_disabled_returns_raw_locator():
    """When ADAPTIVE_SELECTORS=0 (default), get_locator returns page.locator() directly."""
    import adaptive_selector as mod
    original_enabled = mod._ENABLED
    mod._ENABLED = False
    try:
        page = _make_page()
        expected = MagicMock()
        page.locator = MagicMock(return_value=expected)

        from adaptive_selector import AdaptivePlaywrightBridge
        bridge = AdaptivePlaywrightBridge(page)
        result = await bridge.get_locator("login_email", "//input[@name='email']")

        page.locator.assert_called_once_with("//input[@name='email']")
        assert result is expected
    finally:
        mod._ENABLED = original_enabled


# ---------------------------------------------------------------------------
# Tests — feature flag ON
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_get_locator_uses_approved_selector_from_db():
    """When DB has an approved selector, it is used instead of the hardcoded one."""
    import adaptive_selector as mod
    original_enabled = mod._ENABLED
    mod._ENABLED = True
    try:
        page = _make_page()
        approved_locator = _make_locator(visible=True)
        page.locator = MagicMock(return_value=approved_locator)

        with patch("job_store.get_approved_selector", return_value="//input[@id='email_new']"):
            from adaptive_selector import AdaptivePlaywrightBridge
            bridge = AdaptivePlaywrightBridge(page)
            result = await bridge.get_locator("login_email", "//input[@name='email']")

        page.locator.assert_called_with("//input[@id='email_new']")
        assert result is approved_locator
    finally:
        mod._ENABLED = original_enabled


@pytest.mark.asyncio
async def test_get_locator_active_selector_succeeds():
    """When the active (hardcoded) selector works, return it without Scrapling."""
    import adaptive_selector as mod
    original_enabled = mod._ENABLED
    mod._ENABLED = True
    try:
        page = _make_page()
        good_locator = _make_locator(visible=True)
        page.locator = MagicMock(return_value=good_locator)

        with patch("job_store.get_approved_selector", return_value=None):
            from adaptive_selector import AdaptivePlaywrightBridge
            bridge = AdaptivePlaywrightBridge(page)
            result = await bridge.get_locator("login_email", "//input[@name='email']")

        assert result is good_locator
    finally:
        mod._ENABLED = original_enabled


@pytest.mark.asyncio
async def test_get_locator_falls_back_to_scrapling_on_timeout(tmp_path):
    """When Playwright times out, Scrapling is tried."""
    import sys
    import types
    import adaptive_selector as mod
    original_enabled = mod._ENABLED
    mod._ENABLED = True

    # Stub scrapling.defaults so the test doesn't need scrapling installed
    mock_element = MagicMock()
    mock_element.attrib = {"aria-label": "Tu correo", "name": "email"}
    mock_element.tag = "input"

    mock_doc = MagicMock()
    mock_doc.xpath = MagicMock(return_value=[mock_element])
    mock_doc.css = MagicMock(return_value=[mock_element])

    mock_adaptor_cls = MagicMock(return_value=mock_doc)

    scrapling_pkg = types.ModuleType("scrapling")
    scrapling_defaults = types.ModuleType("scrapling.defaults")
    scrapling_defaults.Adaptor = mock_adaptor_cls
    scrapling_pkg.defaults = scrapling_defaults

    try:
        page = _make_page("<html><body><input name='email' aria-label='Tu correo'/></body></html>")
        failing_locator = _make_locator(visible=False)
        aria_locator = MagicMock()
        page.locator = MagicMock(side_effect=lambda s: aria_locator if 'aria-label' in s else failing_locator)
        failing_locator.wait_for = AsyncMock(side_effect=PatchrightTimeout("t"))

        sys.modules.setdefault("scrapling", scrapling_pkg)
        sys.modules["scrapling.defaults"] = scrapling_defaults

        with patch("job_store.get_approved_selector", return_value=None), \
             patch("job_store.create_selector_repair") as mock_create:
            from adaptive_selector import AdaptivePlaywrightBridge
            bridge = AdaptivePlaywrightBridge(page)
            result = await bridge.get_locator("login_email", "//input[@name='email']")

        mock_create.assert_called_once()
        assert result is aria_locator
    finally:
        mod._ENABLED = original_enabled
        sys.modules.pop("scrapling.defaults", None)


@pytest.mark.asyncio
async def test_get_locator_triggers_gemini_when_scrapling_fails():
    """When both Playwright and Scrapling fail, SelectorRepairService is dispatched."""
    import sys
    import types
    import asyncio
    import adaptive_selector as mod
    original_enabled = mod._ENABLED
    mod._ENABLED = True

    mock_doc = MagicMock()
    mock_doc.xpath = MagicMock(return_value=[])
    mock_doc.css = MagicMock(return_value=[])
    scrapling_pkg = types.ModuleType("scrapling")
    scrapling_defaults = types.ModuleType("scrapling.defaults")
    scrapling_defaults.Adaptor = MagicMock(return_value=mock_doc)
    scrapling_pkg.defaults = scrapling_defaults

    try:
        page = _make_page()
        fallback_locator = MagicMock()
        page.locator = MagicMock(return_value=fallback_locator)
        fallback_locator.wait_for = AsyncMock(side_effect=PatchrightTimeout("t"))

        dispatched = []

        async def fake_dispatch(self, key, sel):
            dispatched.append((key, sel))

        sys.modules.setdefault("scrapling", scrapling_pkg)
        sys.modules["scrapling.defaults"] = scrapling_defaults

        with patch("job_store.get_approved_selector", return_value=None), \
             patch.object(mod.AdaptivePlaywrightBridge, "_dispatch_repair", fake_dispatch):
            from adaptive_selector import AdaptivePlaywrightBridge
            bridge = AdaptivePlaywrightBridge(page)
            result = await bridge.get_locator("login_email", "//input[@name='email']")

        await asyncio.sleep(0)
        assert len(dispatched) == 1
        assert dispatched[0][0] == "login_email"
    finally:
        mod._ENABLED = original_enabled
        sys.modules.pop("scrapling.defaults", None)


# ---------------------------------------------------------------------------
# Tests — helpers
# ---------------------------------------------------------------------------

def test_element_to_selector_uses_aria_label():
    from adaptive_selector import _element_to_selector
    elem = MagicMock()
    elem.attrib = {"aria-label": "Publicar", "role": "button"}
    elem.tag = "div"
    assert _element_to_selector(elem) == '//div[@aria-label="Publicar"]'


def test_element_to_selector_uses_name_fallback():
    from adaptive_selector import _element_to_selector
    elem = MagicMock()
    elem.attrib = {"name": "pass"}
    elem.tag = "input"
    assert _element_to_selector(elem) == '//input[@name="pass"]'


def test_element_to_selector_uses_role_fallback():
    from adaptive_selector import _element_to_selector
    elem = MagicMock()
    elem.attrib = {"role": "dialog"}
    elem.tag = "div"
    assert _element_to_selector(elem) == '//div[@role="dialog"]'


def test_element_to_selector_tag_only_fallback():
    from adaptive_selector import _element_to_selector
    elem = MagicMock()
    elem.attrib = {}
    elem.tag = "span"
    assert _element_to_selector(elem) == "//span"


def test_element_to_locator_uses_testid():
    from adaptive_selector import _element_to_locator
    page = MagicMock()
    testid_loc = MagicMock()
    page.get_by_test_id = MagicMock(return_value=testid_loc)

    elem = MagicMock()
    elem.attrib = {"data-testid": "post-button"}
    elem.tag = "div"

    result = _element_to_locator(page, elem)
    page.get_by_test_id.assert_called_once_with("post-button")
    assert result is testid_loc


def test_element_to_locator_uses_aria_label():
    from adaptive_selector import _element_to_locator
    page = MagicMock()
    aria_loc = MagicMock()
    page.locator = MagicMock(return_value=aria_loc)

    elem = MagicMock()
    elem.attrib = {"aria-label": "Publicar"}
    elem.tag = "div"

    result = _element_to_locator(page, elem)
    page.locator.assert_called_once_with('[aria-label="Publicar"]')
    assert result is aria_loc
