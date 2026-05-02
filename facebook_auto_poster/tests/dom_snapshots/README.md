# DOM Snapshots — Facebook UI reference (Fase 3.4)

HTMLs sanitizados de páginas clave de Facebook para validar selectores sin requerir sesión real.

## Cómo capturar un snapshot

1. Ejecuta `facebook_poster_async.py` con una cuenta real (modo debug o test_run).
2. En el punto de interés, añade temporalmente:
   ```python
   html = await self.page.content()
   Path("/tmp/snapshot_feed.html").write_text(html, encoding="utf-8")
   ```
3. Copia el archivo a esta carpeta.
4. Sanitízalo antes de commitear:
   ```bash
   python scripts/scrub_snapshot.py snapshot_feed.html > tests/dom_snapshots/feed.html
   ```

## Snapshots disponibles

| Archivo | Qué representa | Fecha |
|---------|----------------|-------|
| *(ninguno aún)* | — | — |

## Qué eliminan los snapshots sanitizados

- IDs de usuario Facebook (`/100012345678/`)
- Tokens de sesión (`__user`, `__dyn`, `__req`, `__a`)
- `src` de imágenes (reemplazado por `data-src-removed`)
- Scripts inline completos
- Estilos inline completos

## Tests de selectores

`tests/integration/test_selectors.py` valida que los XPaths críticos aún encuentran
elementos en los HTMLs de esta carpeta. Si un selector falla contra un snapshot real,
es candidato a reparación.

## Selectores críticos a validar

| Clave | Selector |
|-------|---------|
| `login_email` | `//input[@name='email']` |
| `login_password` | `//input[@name='pass']` |
| `composer_open` | `//div[@aria-label='Crear publicación']` |
| `composer_editor` | `//div[@role='dialog']//div[@contenteditable='true']` |
| `publish_button` | `//div[@role='dialog']//div[@aria-label='Publicar']` |
| `group_loaded` | `//div[@role='main']` |
