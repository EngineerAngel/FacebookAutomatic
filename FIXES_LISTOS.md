# 🔧 FIXES LISTOS PARA APLICAR — Copia & Pega
**Sistema de Plantillas — Rama produccion_temp**

---

## FIX #1: Problema `selectTemplate()` en Firefox
**Archivo:** `facebook_auto_poster/templates/publish.html`  
**Línea:** ~485  
**Tiempo:** 5 minutos

### ❌ CÓDIGO ORIGINAL (BROKEN)
```javascript
function selectTemplate(tplId) {
  state.selectedTemplate = state.allTemplates.find(t => t.id === tplId);
  document.querySelectorAll('.template-card').forEach(c => c.classList.remove('selected'));
  event.currentTarget.classList.add('selected');
  showStatus('templates-status', 'Plantilla seleccionada: ' + state.selectedTemplate.name, 'info');
}
```

### ✅ CÓDIGO CORREGIDO
```javascript
function selectTemplate(tplId) {
  state.selectedTemplate = state.allTemplates.find(t => t.id === tplId);
  
  // Actualizar visual: eliminar 'selected' de todos, añadir a correcto
  document.querySelectorAll('.template-card').forEach(card => {
    const cardTplId = card.querySelector('.template-actions button').onclick.toString().match(/'([a-f0-9]+)'/)?.[1];
    if (cardTplId === tplId) {
      card.classList.add('selected');
    } else {
      card.classList.remove('selected');
    }
  });
  
  if (state.selectedTemplate) {
    showStatus('templates-status', 'Plantilla seleccionada: ' + escapeHtml(state.selectedTemplate.name), 'info');
  }
}
```

---

## FIX #2: Validar `scheduled_for` Antes de POST
**Archivo:** `facebook_auto_poster/templates/publish.html`  
**Línea:** ~715  
**Tiempo:** 10 minutos

### ❌ CÓDIGO ORIGINAL (BROKEN)
```javascript
async function publish() {
  if (!state.selectedTemplate) {
    showStatus('confirm-status', 'Selecciona una plantilla', 'error');
    return;
  }
  if (state.selectedAccounts.length === 0) {
    showStatus('confirm-status', 'Selecciona al menos una cuenta', 'error');
    return;
  }

  const btn = document.getElementById('publish-btn');
  btn.disabled = true;
  // ...sigue sin validar scheduled_for...
}
```

### ✅ CÓDIGO CORREGIDO — Añadir ANTES de `btn.disabled = true`
```javascript
async function publish() {
  if (!state.selectedTemplate) {
    showStatus('confirm-status', 'Selecciona una plantilla', 'error');
    return;
  }
  if (state.selectedAccounts.length === 0) {
    showStatus('confirm-status', 'Selecciona al menos una cuenta', 'error');
    return;
  }

  // ✅ NUEVA VALIDACIÓN
  if (state.publishWhen === 'scheduled') {
    if (!state.publishDatetime || state.publishDatetime.trim() === '') {
      showStatus('confirm-status', 'Selecciona fecha y hora para publicación programada', 'error');
      return;
    }
    const scheduled = new Date(state.publishDatetime);
    const now = new Date();
    if (isNaN(scheduled.getTime())) {
      showStatus('confirm-status', 'Fecha inválida', 'error');
      return;
    }
    if (scheduled <= now) {
      showStatus('confirm-status', 'La fecha y hora deben ser en el futuro', 'error');
      return;
    }
  }

  const btn = document.getElementById('publish-btn');
  btn.disabled = true;
  // ...continúa resto del código...
}
```

---

## FIX #3: Mejorar Error Handling en `loadTemplates()`
**Archivo:** `facebook_auto_poster/templates/publish.html`  
**Línea:** ~440  
**Tiempo:** 15 minutos

### ❌ CÓDIGO ORIGINAL (BROKEN)
```javascript
async function loadTemplates() {
  const gallery = document.getElementById('templates-gallery');
  try {
    const res = await fetch('/admin/api/templates');
    const templates = await res.json();
    state.allTemplates = templates;
    // ...sigue sin validar res.ok...
  } catch (err) {
    gallery.innerHTML = '<div style="grid-column:1/-1;color:#fca5a5">Error al cargar plantillas</div>';
  }
}
```

### ✅ CÓDIGO CORREGIDO
```javascript
async function loadTemplates() {
  const gallery = document.getElementById('templates-gallery');
  try {
    const res = await fetch('/admin/api/templates');
    
    // ✅ NUEVA VALIDACIÓN: Verificar status HTTP
    if (!res.ok) {
      let errorMsg = `HTTP ${res.status}`;
      try {
        const errData = await res.json();
        errorMsg = errData.error || errorMsg;
      } catch {
        // Si no es JSON, usar el status
      }
      throw new Error(errorMsg);
    }
    
    const templates = await res.json();
    
    // ✅ NUEVA VALIDACIÓN: Verificar que sea array
    if (!Array.isArray(templates)) {
      throw new Error('Respuesta inválida del servidor (no es array)');
    }
    
    state.allTemplates = templates;

    if (templates.length === 0) {
      gallery.innerHTML = '<div style="grid-column:1/-1;color:#475569;text-align:center;padding:2rem">No hay plantillas guardadas</div>';
      return;
    }

    gallery.innerHTML = '';
    templates.forEach((tpl, idx) => {
      // ✅ NUEVA VALIDACIÓN: Verificar campos obligatorios
      if (!tpl.id || !tpl.name) {
        console.error(`Plantilla #${idx} con datos incompletos:`, tpl);
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

## FIX #4: XSS en Preview — Usar `createElement` en lugar de `innerHTML`
**Archivo:** `facebook_auto_poster/templates/publish.html`  
**Línea:** ~545  
**Tiempo:** 20 minutos

### ❌ CÓDIGO ORIGINAL (XSS VULNERABLE)
```javascript
function showTemplatePreview(tplId) {
  const tpl = state.allTemplates.find(t => t.id === tplId);
  if (!tpl) return;

  const modal = document.getElementById('template-preview-modal');
  document.getElementById('preview-modal-title').textContent = tpl.name;
  const body = document.getElementById('preview-modal-body');
  let html = `<div style="margin-bottom:1rem"><strong>Texto:</strong><br><pre style="white-space:pre-wrap;color:#94a3b8">${escapeHtml(tpl.text)}</pre></div>`;
  if (tpl.url) html += `<div style="margin-bottom:1rem"><strong>URL:</strong><br><code style="color:#94a3b8">${escapeHtml(tpl.url)}</code></div>`;
  if (tpl.image_path) html += `<div style="margin-bottom:1rem"><strong>Imagen:</strong><br><img src="${escapeHtml(tpl.image_path)}" style="max-width:100%;border-radius:6px"></div>`;
  body.innerHTML = html;  // ← PELIGRO: innerHTML con datos user

  modal.classList.add('active');
  state._previewTemplateId = tplId;
}
```

### ✅ CÓDIGO CORREGIDO
```javascript
function showTemplatePreview(tplId) {
  const tpl = state.allTemplates.find(t => t.id === tplId);
  if (!tpl) return;

  const modal = document.getElementById('template-preview-modal');
  document.getElementById('preview-modal-title').textContent = tpl.name;
  const body = document.getElementById('preview-modal-body');
  
  // ✅ LIMPIAR CONTENIDO
  body.innerHTML = '';
  
  // ✅ SECCIÓN TEXTO — Usando createElement
  if (tpl.text) {
    const textDiv = document.createElement('div');
    textDiv.style.marginBottom = '1rem';
    
    const textLabel = document.createElement('strong');
    textLabel.textContent = 'Texto:';
    textDiv.appendChild(textLabel);
    textDiv.appendChild(document.createElement('br'));
    
    const textPre = document.createElement('pre');
    textPre.style.whiteSpace = 'pre-wrap';
    textPre.style.color = '#94a3b8';
    textPre.textContent = tpl.text;  // ← textContent, no innerHTML
    textDiv.appendChild(textPre);
    body.appendChild(textDiv);
  }
  
  // ✅ SECCIÓN URL — Usando createElement
  if (tpl.url && typeof tpl.url === 'string' && tpl.url.length > 0) {
    const urlDiv = document.createElement('div');
    urlDiv.style.marginBottom = '1rem';
    
    const urlLabel = document.createElement('strong');
    urlLabel.textContent = 'URL:';
    urlDiv.appendChild(urlLabel);
    urlDiv.appendChild(document.createElement('br'));
    
    const urlCode = document.createElement('code');
    urlCode.style.color = '#94a3b8';
    urlCode.textContent = tpl.url;
    urlDiv.appendChild(urlCode);
    body.appendChild(urlDiv);
  }
  
  // ✅ SECCIÓN IMAGEN — VALIDAR ORIGEN
  if (tpl.image_path && typeof tpl.image_path === 'string' && tpl.image_path.startsWith('/')) {
    const imgDiv = document.createElement('div');
    imgDiv.style.marginBottom = '1rem';
    
    const imgLabel = document.createElement('strong');
    imgLabel.textContent = 'Imagen:';
    imgDiv.appendChild(imgLabel);
    imgDiv.appendChild(document.createElement('br'));
    
    const img = document.createElement('img');
    img.src = tpl.image_path;  // Solo rutas locales relativas
    img.style.maxWidth = '100%';
    img.style.borderRadius = '6px';
    img.style.maxHeight = '400px';
    
    img.onerror = () => {
      img.style.display = 'none';
      const errMsg = document.createElement('p');
      errMsg.style.color = '#fca5a5';
      errMsg.textContent = '(imagen no disponible)';
      imgDiv.appendChild(errMsg);
    };
    
    imgDiv.appendChild(img);
    body.appendChild(imgDiv);
  }

  modal.classList.add('active');
  state._previewTemplateId = tplId;
}
```

---

## FIX #5: Validación de `template_id` en Backend
**Archivo:** `facebook_auto_poster/api_server.py`  
**Ubicación:** Añadir al inicio de la sección de templates (línea ~1220)  
**Tiempo:** 10 minutos

### ✅ CÓDIGO A AÑADIR
```python
# ─── Validación de template_id ───
_TEMPLATE_ID_PATTERN = re.compile(r'^[a-f0-9]{12}$')

def _validate_template_id(template_id: str) -> bool:
    """Valida que template_id sea UUID corto válido."""
    return bool(_TEMPLATE_ID_PATTERN.match(template_id))
```

### Luego actualizar los 3 endpoints que usan `template_id`:

#### En `admin_get_template()` (línea ~1273)
```python
@app.get("/admin/api/templates/<template_id>")
@admin_required
def admin_get_template(template_id: str):
    """Obtiene los detalles de una plantilla específica."""
    if not _validate_template_id(template_id):
        return jsonify({"error": "ID de plantilla inválido"}), 400
    
    template = job_store.get_template(template_id)
    if not template:
        return jsonify({"error": "Plantilla no encontrada"}), 404
    return jsonify(template), 200
```

#### En `admin_update_template()` (línea ~1283)
```python
@app.put("/admin/api/templates/<template_id>")
@admin_required
def admin_update_template(template_id: str):
    """Actualiza una plantilla existente."""
    if not _validate_template_id(template_id):
        return jsonify({"error": "ID de plantilla inválido"}), 400
    
    template = job_store.get_template(template_id)
    if not template:
        return jsonify({"error": "Plantilla no encontrada"}), 404
    # ...resto sin cambios...
```

#### En `admin_delete_template()` (línea ~1328)
```python
@app.delete("/admin/api/templates/<template_id>")
@admin_required
def admin_delete_template(template_id: str):
    """Elimina una plantilla."""
    if not _validate_template_id(template_id):
        return jsonify({"error": "ID de plantilla inválido"}), 400
    
    deleted = job_store.delete_template(template_id)
    if deleted:
        logger.info("Plantilla '%s' eliminada via admin", template_id)
        return "", 204
    return jsonify({"error": f"Plantilla no encontrada"}), 404
```

---

## FIX #6: Mejorar Logging en Backend
**Archivo:** `facebook_auto_poster/api_server.py`  
**Ubicación:** 3 funciones de plantillas  
**Tiempo:** 5 minutos

### En `admin_create_template()` (línea ~1265)
#### ❌ ORIGINAL
```python
    except Exception as exc:
        logger.error("Error creando plantilla: %s", exc)
        return jsonify({"error": "Error interno al crear plantilla"}), 500
```

#### ✅ CORREGIDO
```python
    except Exception as exc:
        logger.exception("Error creando plantilla (nombre='%s', url='%s'): %s", 
                        name, url, exc)
        return jsonify({"error": "Error interno al crear plantilla"}), 500
```

### En `admin_update_template()` (línea ~1320)
#### ❌ ORIGINAL
```python
    except Exception as exc:
        logger.error("Error actualizando plantilla: %s", exc)
        return jsonify({"error": "Error interno al actualizar plantilla"}), 500
```

#### ✅ CORREGIDO
```python
    except Exception as exc:
        logger.exception("Error actualizando plantilla (id='%s'): %s", 
                        template_id, exc)
        return jsonify({"error": "Error interno al actualizar plantilla"}), 500
```

---

## FIX #7: Validar Límite de Tamaño en `text`
**Archivo:** `facebook_auto_poster/api_server.py`  
**Ubicación:** Línea ~1241 y ~1308  
**Tiempo:** 5 minutos

### ✅ CÓDIGO A AÑADIR en inicio del archivo
```python
# Límites de validación
MAX_TEMPLATE_TEXT_CHARS = 50000  # 50 KB de texto
MAX_TEMPLATE_NAME_CHARS = 100
MIN_TEMPLATE_TEXT_CHARS = 10
MAX_TEMPLATE_URL_CHARS = 2048
```

### En `admin_create_template()` (línea ~1241)
#### ❌ ORIGINAL
```python
    if not text or len(text) < 10:
        return jsonify({"error": "El texto debe tener al menos 10 caracteres"}), 400
```

#### ✅ CORREGIDO
```python
    if not text or len(text) < MIN_TEMPLATE_TEXT_CHARS:
        return jsonify({
            "error": f"El texto debe tener entre {MIN_TEMPLATE_TEXT_CHARS} y {MAX_TEMPLATE_TEXT_CHARS} caracteres"
        }), 400
    if len(text) > MAX_TEMPLATE_TEXT_CHARS:
        return jsonify({
            "error": f"El texto no puede exceder {MAX_TEMPLATE_TEXT_CHARS} caracteres"
        }), 400
```

### En `admin_update_template()` (línea ~1308)
#### ❌ ORIGINAL
```python
    if text is not None and len(text) < 10:
        return jsonify({"error": "El texto debe tener al menos 10 caracteres"}), 400
```

#### ✅ CORREGIDO
```python
    if text is not None:
        if len(text) < MIN_TEMPLATE_TEXT_CHARS or len(text) > MAX_TEMPLATE_TEXT_CHARS:
            return jsonify({
                "error": f"El texto debe tener entre {MIN_TEMPLATE_TEXT_CHARS} y {MAX_TEMPLATE_TEXT_CHARS} caracteres"
            }), 400
```

---

## ✅ Orden de Aplicación

1. **FIX #5** (Validación backend) — Independiente
2. **FIX #6** (Logging) — Independiente
3. **FIX #7** (Límites) — Independiente
4. **FIX #1** (selectTemplate) — Importante
5. **FIX #2** (scheduled_for validation) — Crítico
6. **FIX #3** (loadTemplates error handling) — Crítico
7. **FIX #4** (XSS fix) — Crítico

---

## 🧪 Testing Recomendado Post-Fixes

```javascript
// En console del navegador:

// Test FIX #1: Seleccionar plantilla
selectTemplate('0123456789ab');
console.log(state.selectedTemplate);

// Test FIX #2: Intentar publicar sin fecha
state.publishWhen = 'scheduled';
state.publishDatetime = '';
publish();  // Debe fallar con mensaje

// Test FIX #3: Cargar plantillas
loadTemplates();  // Debe mostrar error si falla

// Test FIX #4: XSS attempt
state.allTemplates[0].image_path = 'javascript:alert("xss")';
showTemplatePreview(state.allTemplates[0].id);  // No debe ejecutar script
```

---

**Tiempo Total de Aplicación:** ~1.5 horas  
**Riesgo:** BAJO (cambios localizados, no afectan flujo crítico)  
**Testing:** RECOMENDADO (1 hora adicional)
