#!/usr/bin/env python3
"""
scripts/scrub_snapshot.py — Sanitiza HTMLs de Facebook antes de commitear como snapshots.

Elimina:
  - IDs de usuario y tokens personales de URLs
  - Atributos src de imágenes y videos
  - Scripts inline completos
  - Estilos inline completos

Uso:
    python scripts/scrub_snapshot.py input.html > tests/dom_snapshots/output.html
    python scripts/scrub_snapshot.py input.html tests/dom_snapshots/login.html
"""
from __future__ import annotations

import re
import sys
from pathlib import Path


_TRANSFORMS: list[tuple[str, str]] = [
    # Scripts inline
    (r"<script[^>]*>.*?</script>", "<script>/* removed */</script>"),
    # Styles inline
    (r"<style[^>]*>.*?</style>", "<style>/* removed */</style>"),
    # src/srcset de imágenes y videos (no URLs de navegación)
    (r'(src|srcset)="https?://[^"]*"', r'\1="data-src-removed"'),
    (r"(src|srcset)='https?://[^']*'", r"\1='data-src-removed'"),
    # IDs de usuario Facebook en URLs (/profile.php?id=NNNN, /100012345678/)
    (r"(profile\.php\?id=)\d{5,20}", r"\g<1>USERID"),
    (r"(/)\d{10,20}(/|$)", r"\g<1>USERID\g<2>"),
    # Tokens de sesión en query strings
    (r"(__user|__dyn|__req|__a|__spin_r|__spin_b|__spin_t)=[^&\"'&;]+", r"\1=REMOVED"),
    # Cookies en atributos data-*
    (r'(data-[a-z-]*token[a-z-]*=")[^"]*"', r'\1REMOVED"'),
    # Nonces
    (r'(nonce=")[^"]*"', r'\1REMOVED"'),
]

_FLAGS = re.DOTALL | re.IGNORECASE


def scrub(html: str) -> str:
    for pattern, replacement in _TRANSFORMS:
        html = re.sub(pattern, replacement, html, flags=_FLAGS)
    return html


def main() -> None:
    if len(sys.argv) < 2:
        print("Uso: python scripts/scrub_snapshot.py input.html [output.html]", file=sys.stderr)
        sys.exit(1)

    src = Path(sys.argv[1])
    if not src.exists():
        print(f"Error: '{src}' no encontrado", file=sys.stderr)
        sys.exit(1)

    html = src.read_text(encoding="utf-8", errors="replace")
    cleaned = scrub(html)

    if len(sys.argv) >= 3:
        out = Path(sys.argv[2])
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(cleaned, encoding="utf-8")
        print(f"Sanitizado: {src} -> {out} ({len(cleaned):,} chars)", file=sys.stderr)
    else:
        print(cleaned)


if __name__ == "__main__":
    main()
