# Verificacion de Bugs — Sistema de Plantillas

> Rama: `produccion_temp`
> Proposito: Checklist para verificar si los bugs corregidos aqui tambien estan presentes en la otra rama.

Cada bug incluye: sintoma observable, como verificar si existe en la otra rama, y referencia del fix aplicado.

---

## T1 — `selectTemplate()` falla en Firefox (usa `event.currentTarget`)

- **Archivo:** `templates/publish.html`
- **Sintoma:** Al hacer clic en una plantilla, `selectTemplate()` no resalta la tarjeta seleccionada en Firefox o Safari. Solo funciona en Chrome.
- **Verificacion:** Abrir `/admin/publish` en Firefox. Clic en cualquier plantilla. Si no se resalta con clase `.selected`, el bug esta presente.
- **Fix aplicado:** Reemplazar `event.currentTarget` (global implicito) por busqueda por indice en `state.allTemplates` usando el `tplId`.
- **Archivos modificados:** `templates/publish.html`

---

## T2 — `publish()` envia `scheduled_for=null` sin validar

- **Archivo:** `templates/publish.html`
- **Sintoma:** Si el usuario selecciona "Programada" pero no pone fecha, el `FormData` incluye `scheduled_for=null`. El servidor falla con `ValueError`.
- **Verificacion:** Ir a `/admin/publish`, seleccionar "Programada" sin elegir fecha, hacer clic en Publicar. Si el POST se envia sin validacion previa, el bug esta presente.
- **Fix aplicado:** Validacion antes de deshabilitar el boton: verificar que `state.publishDatetime` no este vacio y que la fecha sea futura (`new Date(scheduled) > new Date()`).
- **Archivos modificados:** `templates/publish.html`

---

## T3 — `loadTemplates()` no detecta errores HTTP

- **Archivo:** `templates/publish.html`
- **Sintoma:** Si el servidor devuelve 401/500, el codigo hace `res.json()` directamente. Como la respuesta es `{error: "..."}` (objeto, no array), el `forEach` posterior falla silenciosamente.
- **Verificacion:** Simular un error de servidor (o inspeccionar el codigo). Buscar `const templates = await res.json()` sin validacion previa de `res.ok`.
- **Fix aplicado:** Validar `res.ok` antes de parsear JSON; verificar `Array.isArray(templates)` antes del `forEach`; validar que cada plantilla tenga `id` y `name`.
- **Archivos modificados:** `templates/publish.html`

---

## T4 — XSS en `showTemplatePreview()` via `innerHTML`

- **Archivo:** `templates/publish.html`
- **Sintoma:** La funcion construye un string HTML con datos del servidor y lo asigna a `body.innerHTML`. Si `image_path` contiene `javascript:alert('xss')`, `escapeHtml()` no lo bloquea como atributo `src`.
- **Verificacion:** Buscar `innerHTML` en `showTemplatePreview()`. Si hay asignacion directa de HTML con datos externos, el bug esta presente.
- **Fix aplicado:** Reconstruir el DOM con `createElement` + `textContent`. Validar que `image_path` empiece con `/` (solo rutas locales).
- **Archivos modificados:** `templates/publish.html`

---

## T5 — Endpoints no validan `template_id` con regex

- **Archivo:** `api_server.py` — endpoints GET/PUT/DELETE `/admin/api/templates/<template_id>`
- **Sintoma:** Un `template_id` con caracteres arbitrarios llega directo a `job_store` sin validacion.
- **Verificacion:** Buscar si existe `_validate_template_id()` con patron `^[a-f0-9]{12}$` y si se llama en los 3 endpoints.
- **Fix aplicado:** Definir `_TEMPLATE_ID_PATTERN = re.compile(r'^[a-f0-9]{12}$')` y validar al inicio de cada endpoint.
- **Archivos modificados:** `api_server.py`

---

## T6 — Sin limites de tamano en campos de plantilla

- **Archivo:** `api_server.py` — `admin_create_template()`, `admin_update_template()`
- **Sintoma:** Solo hay validacion de minimo (10 chars en texto), pero no de maximo. Un texto de 10 MB se guardaria en BD.
- **Verificacion:** Buscar constantes `MAX_TEMPLATE_TEXT_CHARS`, `MAX_TEMPLATE_NAME_CHARS`, `MAX_TEMPLATE_URL_CHARS`, `MIN_TEMPLATE_TEXT_CHARS`.
- **Fix aplicado:** Definir constantes y validar en create y update:
  - `MAX_TEMPLATE_TEXT_CHARS = 50000` (50 KB)
  - `MAX_TEMPLATE_NAME_CHARS = 100`
  - `MAX_TEMPLATE_URL_CHARS = 2048`
  - `MIN_TEMPLATE_TEXT_CHARS = 10`
- **Archivos modificados:** `api_server.py`

---

## T7 — Logging mejorado con `logger.exception()`

- **Archivo:** `api_server.py` — `admin_create_template()`, `admin_update_template()`
- **Sintoma:** Los bloques `except` usan `logger.error()` sin stack trace. Dificil debuggear errores en produccion.
- **Verificacion:** Buscar `logger.error("Error creando plantilla"` o `logger.error("Error actualizando plantilla"` sin `exc_info=True`.
- **Fix aplicado:** Reemplazar `logger.error()` por `logger.exception()` que incluye stack trace automaticamente. Agregar contexto (nombre, url, id) al mensaje.
- **Archivos modificados:** `api_server.py`

---

## T8 — `confirmDeleteTemplate()` no espera a `loadTemplates()`

- **Archivo:** `templates/publish.html`
- **Sintoma:** Tras eliminar una plantilla (204), se llama `loadTemplates()` sin `await` ni manejo de error. Si `loadTemplates()` falla, el usuario ve "Plantilla eliminada" pero la galeria no se actualiza.
- **Verificacion:** Buscar si `confirmDeleteTemplate()` usa `await loadTemplates()` con try/catch alrededor, o si es una llamada fire-and-forget.
- **Fix aplicado:** `await loadTemplates()` dentro de try/catch; mostrar error especifico si falla la recarga; deshabilitar boton durante la operacion.
- **Archivos modificados:** `templates/publish.html`

---

## T9 — Frontend sin `maxlength` ni validacion de tamano de archivo

- **Archivo:** `templates/publish.html`
- **Sintoma:** Los campos `<textarea>` y `<input type="file">` no tienen atributos `maxlength` ni validacion JS de tamano. Un archivo de 100 MB se enviaria al servidor causando timeout.
- **Verificacion:** Inspeccionar los campos de entrada en el formulario de plantillas. Si no tienen `maxlength="50000"` ni validacion JS de tipo/tamano de archivo, el bug esta presente.
- **Fix aplicado:**
  - `<textarea>` con `maxlength="50000"` + hint visual
  - `<input type="file">` con listener `change` que valida tamano max 10 MB y tipos MIME permitidos
- **Archivos modificados:** `templates/publish.html`

---

## T10 — `layout_messaging_attachment_header` overflow en movil

- **Archivo:** `templates/publish.html`
- **Sintoma:** El breadcrumb/header de la vista de publicacion desborda horizontalmente en pantallas moviles.
- **Verificacion:** Abrir `/admin/publish` en viewport < 480px. Si el texto del header se sale del contenedor, el bug esta presente.
- **Fix aplicado:** Estilos CSS responsivos para overflow y word-break en el header del workflow de publicacion.
- **Archivos modificados:** `templates/publish.html`

---

## Regla general: `loadAccounts()` en publish.html

- **Patron a verificar:** `loadAccounts()` (en `publish.html`) debe validar `res.ok` y `Array.isArray()` igual que `loadTemplates()` (ver T3). Si una tiene la validacion y la otra no, aplicar el mismo patron.
- **Verificacion:** Comparar `loadAccounts()` con `loadTemplates()`. Ambas deben seguir la misma estructura de validacion HTTP.
- **Fix aplicado:** Mismo patron de validacion aplicado a ambas funciones.
- **Archivos modificados:** `templates/publish.html`
