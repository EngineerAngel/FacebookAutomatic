# 🔍 AUDITORÍA EXHAUSTIVA: Conexiones, Lógica y Bugs
**Sistema de Plantillas de Publicación — Rama `produccion_temp`**  
**Fecha:** 26 de abril de 2026

---

## 📋 Tabla de Contenidos
1. [Bugs Críticos Encontrados](#bugs-críticos)
2. [Problemas de Conexión HTTP](#problemas-conexión-http)
3. [Fallos en Manejo de Errores](#fallos-manejo-errores)
4. [Validaciones Insuficientes](#validaciones-insuficientes)
5. [Race Conditions y Concurrencia](#race-conditions)
6. [Recomendaciones de Mejora](#recomendaciones)

---

## 🔴 BUGS CRÍTICOS ENCONTRADOS

### **BUG #1: `selectTemplate()` usa `event.currentTarget` — Falla en Firefox**
**Ubicación:** `publish.html` (línea ~485)

```javascript
// ❌ PROBLEMA
function selectTemplate(tplId) {
  state.selectedTemplate = state.allTemplates.find(t => t.id === tplId);
  document.querySelectorAll('.template-card').forEach(c => c.classList.remove('selected'));
  event.currentTarget.classList.add('selected');  // ← FALLA EN FIREFOX
  showStatus('templates-status', 'Plantilla seleccionada: ' + state.selectedTemplate.name, 'info');
}
```

**Impacto:** En Firefox y navegadores antiguos, `event.currentTarget` puede ser `null` o apuntar a elemento incorrecto.

**Solución:**
```javascript
// ✅ CORRECCIÓN
function selectTemplate(tplId) {
  state.selectedTemplate = state.allTemplates.find(t => t.id === tplId);
  document.querySelectorAll('.template-card').forEach((c, i) => {
    if (state.allTemplates[i] && state.allTemplates[i].id === tplId) {
      c.classList.add('selected');
    } else {
      c.classList.remove('selected');
    }
  });
  if (state.selectedTemplate) {
    showStatus('templates-status', 'Plantilla seleccionada: ' + state.selectedTemplate.name, 'info');
  }
}
```

---

### **BUG #2: Sin Validación de `scheduled_for` Antes de POST**
**Ubicación:** `publish.html` (línea ~745)

```javascript
// ❌ PROBLEMA
async function publish() {
  // ... otras validaciones ...
  if (state.publishWhen === 'scheduled') {
    fd.append('scheduled_for', state.publishDatetime);  // ← Puede ser null/vacío
  }
  const res = await fetch(endpoint, { method: 'POST', body: fd });
  // Sin validar que scheduled_for > now()
}
```

**Impacto:** Envía `scheduled_for=null` al servidor, luego falla con `ValueError` en api_server.py.

**Solución:**
```javascript
// ✅ CORRECCIÓN
async function publish() {
  if (!state.selectedTemplate) {
    showStatus('confirm-status', 'Selecciona una plantilla', 'error');
    return;
  }
  if (state.selectedAccounts.length === 0) {
    showStatus('confirm-status', 'Selecciona al menos una cuenta', 'error');
    return;
  }
  
  // NUEVA VALIDACIÓN: scheduled_for obligatorio si es scheduled
  if (state.publishWhen === 'scheduled') {
    if (!state.publishDatetime) {
      showStatus('confirm-status', 'Selecciona fecha y hora para publicación programada', 'error');
      return;
    }
    // Validar que sea fecha futura
    const scheduled = new Date(state.publishDatetime);
    if (scheduled <= new Date()) {
      showStatus('confirm-status', 'La fecha debe ser en el futuro', 'error');
      return;
    }
  }
  // ... resto del código ...
}
```

---

### **BUG #3: `loadTemplates()` No Gestiona Errores HTTP Correctamente**
**Ubicación:** `publish.html` (línea ~440)

```javascript
// ❌ PROBLEMA
async function loadTemplates() {
  const gallery = document.getElementById('templates-gallery');
  try {
    const res = await fetch('/admin/api/templates');
    const templates = await res.json();  // ← Si res es 401/403/500, falla aquí
    state.allTemplates = templates;
    // ...
  } catch (err) {
    gallery.innerHTML = '<div style="grid-column:1/-1;color:#fca5a5">Error al cargar plantillas</div>';
  }
}
```

**Impacto:** Si servidor retorna 401 (no autorizado), el JSON tiene `{error: "..."}` pero el código asume que es array.

**Solución:**
```javascript
// ✅ CORRECCIÓN
async function loadTemplates() {
  const gallery = document.getElementById('templates-gallery');
  try {
    const res = await fetch('/admin/api/templates');
    
    // VALIDAR HTTP STATUS
    if (!res.ok) {
      const err = await res.json().catch(() => ({ error: 'Error desconocido' }));
      throw new Error(err.error || `HTTP ${res.status}`);
    }
    
    const templates = await res.json();
    
    // VALIDAR TIPO DE RESPUESTA
    if (!Array.isArray(templates)) {
      throw new Error('Respuesta inválida del servidor');
    }
    
    state.allTemplates = templates;
    if (templates.length === 0) {
      gallery.innerHTML = '<div style="grid-column:1/-1;color:#475569;text-align:center;padding:2rem">No hay plantillas guardadas</div>';
      return;
    }

    gallery.innerHTML = '';
    templates.forEach(tpl => {
      // Validar que tpl.id y tpl.name existan
      if (!tpl.id || !tpl.name) {
        console.error('Plantilla con datos incompletos:', tpl);
        return;
      }
      const card = document.createElement('div');
      card.className = 'template-card';
      card.innerHTML = `
        <div class="template-name">${escapeHtml(tpl.name)}</div>
        <div class="template-preview">${escapeHtml((tpl.text || '').substring(0, 100))}</div>
        <div class="template-actions">
          <button class="btn btn-secondary btn-sm" onclick="showTemplatePreview('${escapeHtml(tpl.id)}')">Vista previa</button>
          <button class="btn btn-secondary btn-sm" onclick="prepareDeleteTemplate('${escapeHtml(tpl.id)}','${escapeHtml(tpl.name)}')">Eliminar</button>
        </div>
      `;
      card.onclick = () => selectTemplate(tpl.id);
      gallery.appendChild(card);
    });
  } catch (err) {
    console.error('Error cargando plantillas:', err);
    gallery.innerHTML = `<div style="grid-column:1/-1;color:#fca5a5">Error: ${escapeHtml(err.message)}</div>`;
  }
}
```

---

### **BUG #4: XSS en `showTemplatePreview()` — Imagen sin Validar**
**Ubicación:** `publish.html` (línea ~545)

```javascript
// ❌ PROBLEMA
function showTemplatePreview(tplId) {
  const tpl = state.allTemplates.find(t => t.id === tplId);
  if (!tpl) return;
  
  const body = document.getElementById('preview-modal-body');
  let html = `<div>...`;
  if (tpl.image_path) {
    html += `<img src="${escapeHtml(tpl.image_path)}" ...>`;  // ← escapeHtml NO previene XSS en atributo src
  }
  body.innerHTML = html;  // ← innerHTML es PELIGROSO
}
```

**Impacto:** Si `tpl.image_path` contiene `javascript:alert('xss')`, `escapeHtml()` la pasa igual. Además, `innerHTML` ejecuta scripts.

**Solución:**
```javascript
// ✅ CORRECCIÓN
function showTemplatePreview(tplId) {
  const tpl = state.allTemplates.find(t => t.id === tplId);
  if (!tpl) return;

  const modal = document.getElementById('template-preview-modal');
  document.getElementById('preview-modal-title').textContent = tpl.name;  // textContent, no HTML
  const body = document.getElementById('preview-modal-body');
  
  // Crear elementos DOM en lugar de innerHTML
  body.innerHTML = '';
  
  // Texto
  const textDiv = document.createElement('div');
  textDiv.style.marginBottom = '1rem';
  const textStrong = document.createElement('strong');
  textStrong.textContent = 'Texto:';
  textDiv.appendChild(textStrong);
  textDiv.appendChild(document.createElement('br'));
  const textPre = document.createElement('pre');
  textPre.style.whiteSpace = 'pre-wrap';
  textPre.style.color = '#94a3b8';
  textPre.textContent = tpl.text;  // textContent, no HTML
  textDiv.appendChild(textPre);
  body.appendChild(textDiv);
  
  // URL
  if (tpl.url && typeof tpl.url === 'string') {
    const urlDiv = document.createElement('div');
    urlDiv.style.marginBottom = '1rem';
    const urlStrong = document.createElement('strong');
    urlStrong.textContent = 'URL:';
    urlDiv.appendChild(urlStrong);
    urlDiv.appendChild(document.createElement('br'));
    const urlCode = document.createElement('code');
    urlCode.style.color = '#94a3b8';
    urlCode.textContent = tpl.url;
    urlDiv.appendChild(urlCode);
    body.appendChild(urlDiv);
  }
  
  // Imagen — VALIDAR URL
  if (tpl.image_path && typeof tpl.image_path === 'string' && tpl.image_path.startsWith('/')) {
    const imgDiv = document.createElement('div');
    imgDiv.style.marginBottom = '1rem';
    const imgStrong = document.createElement('strong');
    imgStrong.textContent = 'Imagen:';
    imgDiv.appendChild(imgStrong);
    imgDiv.appendChild(document.createElement('br'));
    const img = document.createElement('img');
    img.src = tpl.image_path;  // Solo rutas locales (comienzan con /)
    img.style.maxWidth = '100%';
    img.style.borderRadius = '6px';
    img.onerror = () => {
      img.style.display = 'none';
      imgDiv.appendChild(document.createTextNode('(imagen no disponible)'));
    };
    imgDiv.appendChild(img);
    body.appendChild(imgDiv);
  }
  
  modal.classList.add('active');
  state._previewTemplateId = tplId;
}
```

---

### **BUG #5: `confirmDeleteTemplate()` No Valida Response**
**Ubicación:** `publish.html` (línea ~569)

```javascript
// ❌ PROBLEMA
async function confirmDeleteTemplate() {
  if (!state.templateToDelete) return;
  const tplId = state.templateToDelete.id;
  try {
    const res = await fetch('/admin/api/templates/' + tplId, { method: 'DELETE' });
    if (res.status === 204) {  // Solo valida status, no content
      showStatus('templates-status', 'Plantilla eliminada', 'ok');
      loadTemplates();  // Pero loadTemplates() puede fallar y no se detecta
      closeModal('delete-template-modal');
    } else {
      showStatus('templates-status', 'Error al eliminar', 'error');
    }
  } catch {
    showStatus('templates-status', 'Error de conexión', 'error');
  }
}
```

**Impacto:** Si `loadTemplates()` falla, el usuario ve "Plantilla eliminada" pero la galería no se actualiza.

**Solución:**
```javascript
// ✅ CORRECCIÓN
async function confirmDeleteTemplate() {
  if (!state.templateToDelete) return;
  const tplId = state.templateToDelete.id;
  const deleteBtn = document.querySelector('#delete-template-modal .btn-danger');
  deleteBtn.disabled = true;
  
  try {
    const res = await fetch('/admin/api/templates/' + tplId, { method: 'DELETE' });
    
    if (res.status === 204) {
      showStatus('templates-status', 'Eliminando…', 'info');
      
      try {
        await loadTemplates();  // Esperar a que se recargue
        showStatus('templates-status', 'Plantilla eliminada', 'ok');
        closeModal('delete-template-modal');
      } catch (loadErr) {
        console.error('Error recargando plantillas:', loadErr);
        showStatus('templates-status', 'Plantilla eliminada pero error al recargar lista', 'error');
      }
    } else if (res.status === 404) {
      showStatus('templates-status', 'Plantilla no encontrada (ya fue eliminada)', 'error');
      closeModal('delete-template-modal');
      loadTemplates();
    } else {
      const data = await res.json().catch(() => ({ error: 'Error desconocido' }));
      showStatus('templates-status', 'Error: ' + (data.error || 'Desconocido'), 'error');
    }
  } catch (err) {
    console.error('Error eliminando:', err);
    showStatus('templates-status', 'Error de conexión: ' + err.message, 'error');
  } finally {
    deleteBtn.disabled = false;
  }
}
```

---

### **BUG #6: Race Condition en `publish()` — Estado Inconsistente**
**Ubicación:** `publish.html` (línea ~715)

```javascript
// ❌ PROBLEMA
async function publish() {
  // ... validaciones ...
  
  const btn = document.getElementById('publish-btn');
  btn.disabled = true;
  
  const fd = new FormData();
  fd.append('text', state.selectedTemplate.text);
  // ... construir formdata ...
  
  try {
    const endpoint = state.publishWhen === 'scheduled' ? '/admin/api/schedule' : '/admin/api/post';
    const res = await fetch(endpoint, { method: 'POST', body: fd });
    const data = await res.json();

    if (res.ok) {
      showStatus('confirm-status', 'Publicado exitosamente. Job ID: ' + data.job_id, 'ok');
      loadScheduled();
      resetForm();  // ← resetForm() puede fallar y estado queda inconsistente
    } else {
      showStatus('confirm-status', 'Error: ' + (data.error || 'Desconocido'), 'error');
    }
  } catch {
    showStatus('confirm-status', 'Error de conexión', 'error');
  } finally {
    btn.disabled = false;
  }
}
```

**Impacto:** Si `resetForm()` o `loadScheduled()` lanzan excepción, el botón vuelve a estar habilitado pero el estado es inconsistente.

**Solución:**
```javascript
// ✅ CORRECCIÓN
async function publish() {
  if (!state.selectedTemplate) {
    showStatus('confirm-status', 'Selecciona una plantilla', 'error');
    return;
  }
  if (state.selectedAccounts.length === 0) {
    showStatus('confirm-status', 'Selecciona al menos una cuenta', 'error');
    return;
  }
  if (state.publishWhen === 'scheduled') {
    if (!state.publishDatetime) {
      showStatus('confirm-status', 'Selecciona fecha y hora', 'error');
      return;
    }
    const scheduled = new Date(state.publishDatetime);
    if (scheduled <= new Date()) {
      showStatus('confirm-status', 'La fecha debe ser en el futuro', 'error');
      return;
    }
  }

  const btn = document.getElementById('publish-btn');
  btn.disabled = true;
  showStatus('confirm-status', 'Publicando…', 'info');

  const fd = new FormData();
  fd.append('text', state.selectedTemplate.text);
  if (state.selectedTemplate.url) fd.append('url', state.selectedTemplate.url);
  fd.append('accounts', state.selectedAccounts.join(','));
  if (state.publishWhen === 'scheduled') {
    fd.append('scheduled_for', state.publishDatetime);
  }
  if (state.selectedTemplate.image_file) {
    fd.append('image', state.selectedTemplate.image_file);
  }

  try {
    const endpoint = state.publishWhen === 'scheduled' ? '/admin/api/schedule' : '/admin/api/post';
    const res = await fetch(endpoint, { method: 'POST', body: fd });
    
    if (!res.ok) {
      const data = await res.json().catch(() => ({ error: 'Error desconocido' }));
      showStatus('confirm-status', 'Error: ' + (data.error || `HTTP ${res.status}`), 'error');
      return;  // NO resetear si hay error
    }
    
    const data = await res.json();
    if (!data.job_id) {
      showStatus('confirm-status', 'Respuesta inválida del servidor', 'error');
      return;
    }
    
    showStatus('confirm-status', 'Publicado exitosamente. Job ID: ' + data.job_id, 'ok');
    
    // Recargar listas
    try {
      await loadScheduled();
    } catch (e) {
      console.error('Error recargando jobs:', e);
    }
    
    // Solo resetear si todo fue bien
    resetForm();
    switchWorkflow('templates');  // Volver al inicio
    
  } catch (err) {
    console.error('Error publicando:', err);
    showStatus('confirm-status', 'Error de conexión: ' + err.message, 'error');
  } finally {
    btn.disabled = false;
  }
}
```

---

## 🔗 PROBLEMAS DE CONEXIÓN HTTP

### **PROBLEMA #1: Sin Timeout en Fetch Calls**
**Ubicación:** Todos los `fetch()` en `publish.html`

**Impacto:** Si servidor cuelga, usuario espera indefinidamente sin feedback.

**Solución:**
```javascript
// ✅ Helper con timeout
async function fetchWithTimeout(url, options = {}, timeoutMs = 10000) {
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), timeoutMs);
  
  try {
    return await fetch(url, { ...options, signal: controller.signal });
  } catch (err) {
    if (err.name === 'AbortError') {
      throw new Error(`Timeout después de ${timeoutMs}ms`);
    }
    throw err;
  } finally {
    clearTimeout(timeout);
  }
}

// Usar así:
async function loadTemplates() {
  try {
    const res = await fetchWithTimeout('/admin/api/templates', {}, 5000);
    // ... resto del código ...
  } catch (err) {
    // "Timeout después de 5000ms" o error de red
  }
}
```

---

### **PROBLEMA #2: Sin Reintentos en Fallos Transientes**
**Ubicación:** `loadAccounts()`, `loadScheduled()`, etc.

**Impacto:** Un error de red temporal causa fallo permanente en la UI.

**Solución:**
```javascript
// ✅ Helper con reintentos
async function fetchWithRetry(url, options = {}, maxRetries = 3, delayMs = 500) {
  let lastError;
  for (let i = 0; i < maxRetries; i++) {
    try {
      const res = await fetchWithTimeout(url, options, 10000);
      return res;
    } catch (err) {
      lastError = err;
      console.warn(`Intento ${i + 1}/${maxRetries} falló para ${url}:`, err.message);
      if (i < maxRetries - 1) {
        await new Promise(r => setTimeout(r, delayMs * Math.pow(2, i)));  // Backoff exponencial
      }
    }
  }
  throw lastError;
}

// Usar así:
async function loadTemplates() {
  try {
    const res = await fetchWithRetry('/admin/api/templates', {}, 3);
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const templates = await res.json();
    // ...
  } catch (err) {
    console.error('Error final:', err);
  }
}
```

---

### **PROBLEMA #3: Sin Validación de Content-Type en Respuestas**
**Ubicación:** Todos los `res.json()` en `publish.html`

**Impacto:** Si servidor retorna HTML (error 500), `res.json()` falla silenciosamente.

**Solución:**
```javascript
// ✅ Helper seguro
async function getJsonResponse(res) {
  const contentType = res.headers.get('content-type') || '';
  
  if (!contentType.includes('application/json')) {
    const text = await res.text();
    console.error('Content-Type no es JSON:', contentType);
    console.error('Respuesta:', text.substring(0, 500));
    throw new Error(`Respuesta inválida (${contentType}): esperaba JSON`);
  }
  
  return await res.json();
}

// Usar así:
try {
  const res = await fetch('/admin/api/templates');
  if (!res.ok) {
    const data = await getJsonResponse(res);
    throw new Error(data.error || 'Unknown error');
  }
  const templates = await getJsonResponse(res);
} catch (err) {
  // Error bien manejado
}
```

---

## ⚠️ FALLOS EN MANEJO DE ERRORES

### **FALLO #1: `admin_create_template()` — Sin Logging de Errors Detallado**
**Ubicación:** `api_server.py` (línea ~1246)

```python
# ❌ PROBLEMA
try:
    template_id = job_store.create_template(name, text, url, image_path)
    logger.info("Plantilla '%s' creada (id=%s) via admin", name, template_id)
    return jsonify({"status": "created", "id": template_id, "name": name}), 201
except sqlite3.IntegrityError:
    return jsonify({"error": f"Ya existe una plantilla con el nombre '{name}'"}), 409
except Exception as exc:
    logger.error("Error creando plantilla: %s", exc)  # ← No captura stack trace
    return jsonify({"error": "Error interno al crear plantilla"}), 500
```

**Impacto:** En log solo aparece `Error creando plantilla: (error message)`, sin traceback para debuggear.

**Solución:**
```python
# ✅ CORRECCIÓN
except Exception as exc:
    logger.exception("Error creando plantilla (nombre='%s', url='%s'): %s", 
                    name, url, exc)  # exception() incluye stack trace
    return jsonify({"error": "Error interno al crear plantilla"}), 500
```

---

### **FALLO #2: `admin_list_templates()` — Sin Manejo de Error en job_store**
**Ubicación:** `api_server.py` (línea ~1225)

```python
# ❌ PROBLEMA
@app.get("/admin/api/templates")
@admin_required
def admin_list_templates():
    """Lista todas las plantillas de publicación."""
    templates = job_store.list_templates()  # ← Puede fallar por error BD
    return jsonify(templates), 200
```

**Impacto:** Si BD falla (e.g., corrupta, sin permisos), Flask retorna 500 con stack trace expuesto.

**Solución:**
```python
# ✅ CORRECCIÓN
@app.get("/admin/api/templates")
@admin_required
def admin_list_templates():
    """Lista todas las plantillas de publicación."""
    try:
        templates = job_store.list_templates()
        return jsonify(templates), 200
    except Exception as exc:
        logger.exception("Error listando plantillas:")
        return jsonify({"error": "Error al acceder a la base de datos"}), 500
```

---

### **FALLO #3: `admin_update_template()` — IntegrityError sin Información de Cuál Campo Falló**
**Ubicación:** `api_server.py` (línea ~1310)

```python
# ❌ PROBLEMA
try:
    success = job_store.update_template(template_id, name, text, url, image_path)
    if success:
        logger.info("Plantilla '%s' actualizada via admin", template_id)
        return jsonify({"status": "updated", "id": template_id}), 200
    else:
        return jsonify({"error": "No hay campos para actualizar"}), 400
except sqlite3.IntegrityError:
    if name:
        return jsonify({"error": f"Ya existe una plantilla con el nombre '{name}'"}), 409
    raise  # ← Si no es por nombre, re-levanta sin información
except Exception as exc:
    logger.error("Error actualizando plantilla: %s", exc)
    return jsonify({"error": "Error interno al actualizar plantilla"}), 500
```

**Impacto:** Si IntegrityError es por otra razón, usuario ve error genérico sin explicación.

**Solución:**
```python
# ✅ CORRECCIÓN
try:
    success = job_store.update_template(template_id, name, text, url, image_path)
    if success:
        logger.info("Plantilla '%s' actualizada via admin", template_id)
        return jsonify({"status": "updated", "id": template_id}), 200
    else:
        return jsonify({"error": "Plantilla no encontrada o sin campos para actualizar"}), 404
except sqlite3.IntegrityError as e:
    # Analizar el error para dar mensaje específico
    error_str = str(e).lower()
    if 'unique constraint failed: templates.name' in error_str or 'name' in error_str:
        return jsonify({"error": f"Ya existe una plantilla con el nombre '{name}'"}), 409
    else:
        logger.exception("IntegrityError desconocido en update_template:")
        return jsonify({"error": "Violación de restricción de datos"}), 409
except Exception as exc:
    logger.exception("Error actualizando plantilla (id='%s'):", template_id)
    return jsonify({"error": "Error interno al actualizar plantilla"}), 500
```

---

## 🔓 VALIDACIONES INSUFICIENTES

### **VALIDACIÓN #1: Falta Sanitización de `template_id` en URLs**
**Ubicación:** `api_server.py` (línea ~1273, 1283, 1328)

```python
# ❌ PROBLEMA
@app.get("/admin/api/templates/<template_id>")
@admin_required
def admin_get_template(template_id: str):
    """Obtiene los detalles de una plantilla específica."""
    template = job_store.get_template(template_id)  # ← template_id sin validar
    if not template:
        return jsonify({"error": f"Plantilla '{template_id}' no encontrada"}), 404
    return jsonify(template), 200
```

**Impacto:** Si `template_id` contiene caracteres especiales SQL (`' OR 1=1`), aunque usa parametrized queries, es mejor validar.

**Solución:**
```python
# ✅ CORRECCIÓN
import re

TEMPLATE_ID_PATTERN = re.compile(r'^[a-f0-9]{12}$')

def _validate_template_id(template_id: str) -> bool:
    return bool(TEMPLATE_ID_PATTERN.match(template_id))

@app.get("/admin/api/templates/<template_id>")
@admin_required
def admin_get_template(template_id: str):
    """Obtiene los detalles de una plantilla específica."""
    if not _validate_template_id(template_id):
        return jsonify({"error": "ID de plantilla inválido"}), 400
    
    template = job_store.get_template(template_id)
    if not template:
        return jsonify({"error": f"Plantilla no encontrada"}), 404
    return jsonify(template), 200
```

---

### **VALIDACIÓN #2: Falta Límite de Tamaño en `text`**
**Ubicación:** `api_server.py` (línea ~1241, 1308)

```python
# ❌ PROBLEMA
if not text or len(text) < 10:
    return jsonify({"error": "El texto debe tener al menos 10 caracteres"}), 400
# ← Pero NO hay límite máximo
```

**Impacto:** Usuario podría cargar 10MB de texto, causando BD grande y lentitud.

**Solución:**
```python
# ✅ CORRECCIÓN
MAX_TEMPLATE_TEXT = 50000  # 50KB máximo

if not text or len(text) < 10:
    return jsonify({"error": "El texto debe tener entre 10 y 50000 caracteres"}), 400
if len(text) > MAX_TEMPLATE_TEXT:
    return jsonify({"error": f"El texto no puede exceder {MAX_TEMPLATE_TEXT} caracteres"}), 400
```

---

### **VALIDACIÓN #3: Falta Validación de `url` — Podría ser Cualquier String**
**Ubicación:** `api_server.py` (línea ~1243, 1309)

```python
# ❌ PROBLEMA
if not url:
    return jsonify({"error": "La URL es obligatoria"}), 400
# ← Pero NO valida que sea URL válida
```

**Impacto:** `url` podría ser "abc123", causando error en frontend al usarla.

**Solución:**
```python
# ✅ CORRECCIÓN
import re
from urllib.parse import urlparse

def _validate_url(url: str) -> bool:
    """Valida que sea URL http/https válida."""
    if not url or len(url) > 2048:
        return False
    try:
        result = urlparse(url)
        return result.scheme in ('http', 'https') and result.netloc
    except Exception:
        return False

if not url or not _validate_url(url):
    return jsonify({"error": "La URL debe ser http:// o https:// válida"}), 400
```

---

### **VALIDACIÓN #4: Frontend — Sin Validación de Longitud en Campos de Entrada**
**Ubicación:** `publish.html`

```html
<!-- ❌ PROBLEMA -->
<textarea id="custom-text" placeholder="Escribe el texto aquí…"></textarea>
<!-- Sin maxlength, sin validación de tamaño -->

<input type="file" id="custom-image" accept=".jpg,.jpeg,.png,.gif,.webp">
<!-- Sin validación de tamaño máximo -->
```

**Impacto:** Usuario intenta subir archivo 100MB, `publish()` lo envía, timeout.

**Solución:**
```html
<!-- ✅ CORRECCIÓN -->
<textarea id="custom-text" placeholder="Escribe el texto aquí…" maxlength="50000"></textarea>
<p class="hint">Máximo 50,000 caracteres</p>

<input type="file" id="custom-image" accept=".jpg,.jpeg,.png,.gif,.webp">
<p class="hint">Máximo 10MB, formatos: jpg, jpeg, png, gif, webp</p>
```

```javascript
// ✅ VALIDACIÓN EN JAVASCRIPT
document.getElementById('custom-image').addEventListener('change', function() {
  const maxSizeMB = 10;
  const maxSizeBytes = maxSizeMB * 1024 * 1024;
  
  if (this.files.length > 0) {
    const file = this.files[0];
    if (file.size > maxSizeBytes) {
      showStatus('templates-status', `Archivo demasiado grande (máx ${maxSizeMB}MB)`, 'error');
      this.value = '';
      return;
    }
    
    const allowedTypes = ['image/jpeg', 'image/png', 'image/gif', 'image/webp'];
    if (!allowedTypes.includes(file.type)) {
      showStatus('templates-status', 'Tipo de archivo no permitido', 'error');
      this.value = '';
      return;
    }
  }
});
```

---

## ⚡ RACE CONDITIONS Y CONCURRENCIA

### **RACE #1: `selectedTemplate` Puede Estar Null Cuando Se Publica**
**Ubicación:** `publish.html` (línea ~715)

**Escenario:**
1. Usuario selecciona plantilla
2. Otra pestaña elimina esa plantilla
3. Primera pestaña intenta publicar
4. `state.selectedTemplate` ya no existe en `state.allTemplates`

**Impacto:** La publicación se envía con datos obsoletos.

**Solución:**
```javascript
// ✅ CORRECCIÓN — Validar plantilla en momento de publish
async function publish() {
  if (!state.selectedTemplate) {
    showStatus('confirm-status', 'Selecciona una plantilla', 'error');
    return;
  }
  
  // VALIDAR que la plantilla aún existe en servidor
  try {
    const res = await fetch(`/admin/api/templates/${state.selectedTemplate.id}`);
    if (!res.ok) {
      showStatus('confirm-status', 'Plantilla fue eliminada', 'error');
      loadTemplates();  // Recargar galería
      switchWorkflow('templates');
      return;
    }
  } catch (err) {
    showStatus('confirm-status', 'Error validando plantilla', 'error');
    return;
  }
  
  // ... continuar con publish ...
}
```

---

### **RACE #2: `loadTemplates()` Mientras Se Está Mostrando Preview**
**Ubicación:** `publish.html` (línea ~440, 545)

**Escenario:**
1. Usuario abre preview de plantilla
2. Se ejecuta `loadTemplates()` (auto-refresh)
3. `state.allTemplates` se reemplaza
4. `state._previewTemplateId` apunta a plantilla que ya no existe

**Impacto:** Modal de preview muestra datos incorrectos o vacíos.

**Solución:**
```javascript
// ✅ CORRECCIÓN — Usar ID en lugar de guardar referencia
async function loadTemplates() {
  const gallery = document.getElementById('templates-gallery');
  const currentPreviewId = state._previewTemplateId;  // Guardar ID actual
  
  try {
    const res = await fetchWithRetry('/admin/api/templates');
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    
    const templates = await getJsonResponse(res);
    if (!Array.isArray(templates)) throw new Error('Respuesta inválida');
    
    state.allTemplates = templates;
    
    // Si había un preview abierto, actualizarlo con datos nuevos
    if (currentPreviewId && document.getElementById('template-preview-modal').classList.contains('active')) {
      const updatedTemplate = templates.find(t => t.id === currentPreviewId);
      if (updatedTemplate) {
        // Actualizar el contenido del modal
        document.getElementById('preview-modal-title').textContent = updatedTemplate.name;
        // ... actualizar body ...
      } else {
        // Plantilla fue eliminada
        closeModal('template-preview-modal');
        showStatus('templates-status', 'Plantilla fue eliminada', 'error');
      }
    }
    
    // Renderizar galería...
    renderTemplateGallery();
  } catch (err) {
    console.error('Error:', err);
  }
}
```

---

## 📊 RECOMENDACIONES DE MEJORA

### **Priority 1: CRÍTICO (Aplicar ya)**
| Número | Bug | Solución | Tiempo |
|--------|-----|----------|--------|
| #1 | `selectTemplate()` con `event.currentTarget` | Refactorizar con IDs | 30 min |
| #2 | Sin validación `scheduled_for` | Validar antes de POST | 20 min |
| #3 | `loadTemplates()` error handling | Mejorar respuesta HTTP | 30 min |
| #4 | XSS en preview de imagen | Usar textContent + createElement | 40 min |
| Validación #1 | Falta sanitización `template_id` | Validar con regex | 20 min |

**Total:** ~2.5 horas

---

### **Priority 2: ALTO (Próxima sprint)**
| Número | Bug | Solución | Tiempo |
|--------|-----|----------|--------|
| #5 | Race condition en `publish()` | Mejor manejo de estado | 45 min |
| #6 | Sin timeout en fetch | Implementar helper timeout | 30 min |
| Fallo #1 | Sin logging detallado | Usar `logger.exception()` | 20 min |
| Validación #2 | Falta límite tamaño `text` | Limitar a 50KB | 15 min |
| Validación #3 | Validar `url` | Usar `urlparse` | 20 min |

**Total:** ~2 horas

---

### **Priority 3: MEDIO (Backlog)**
| Número | Bug | Solución | Tiempo |
|--------|-----|----------|--------|
| #7 | Sin reintentos en fallos | Implementar helper retry | 45 min |
| #8 | Sin validación Content-Type | Helper `getJsonResponse()` | 30 min |
| Race #1 | Plantilla eliminada durante publish | Validar en servidor | 25 min |
| Validación #4 | Sin validación en frontend | Añadir maxlength + verificar archivo | 30 min |

**Total:** ~2 horas

---

## 🔧 Plan de Implementación

### **Fase 1: Hotfixes (Hoy)**
```bash
# Crear rama de fixes
git checkout -b produccion_temp_hotfixes

# Arreglar bugs críticos (#1, #2, #3, #4)
# Validación #1
# Fallo #1

# Commit
git commit -m "fix: resolver bugs críticos de conexión y validación

- Bug #1: selectTemplate() con event.currentTarget → usar IDs
- Bug #2: validar scheduled_for antes de POST
- Bug #3: mejorar error handling en loadTemplates()
- Bug #4: XSS en preview → usar createElement
- Val #1: sanitizar template_id con regex
- Fallo #1: usar logger.exception() con stack trace
"

# Merge a produccion_temp
git checkout produccion_temp
git merge produccion_temp_hotfixes
```

### **Fase 2: Mejoras (Próxima semana)**
```bash
# Crear rama
git checkout -b produccion_temp_improvements

# Implementar:
# - Helpers timeout, retry, getJsonResponse
# - Limitar tamaño campos
# - Validar URLs
# - Better race condition handling
```

---

## ✅ Checklist de Testing

Después de aplicar fixes:

- [ ] Test en Firefox (event.currentTarget issue)
- [ ] Test con timeout de red (helper timeout)
- [ ] Test XSS: intentar inyectar `javascript:alert()` en preview
- [ ] Test form con archivo 100MB
- [ ] Test publicar y eliminar plantilla simultáneamente
- [ ] Test cargar anuncio.txt si no existe
- [ ] Test rate limiting en 10 requeset/ 60s
- [ ] Test con BD corrupta (error handling)

---

**Documento Generado:** 26 de abril de 2026  
**Estado:** LISTO PARA APLICAR  
**Ramas:** produccion_temp (cambios implementados) → hotfixes (fixes aplicar)
