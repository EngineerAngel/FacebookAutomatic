# Plan: Wizard de publicación con Alpine.js

## Contexto

`produccion_temp` implementa un wizard de 5 pasos en `publish.html` (1367 líneas), pero
el estado está disperso en variables globales y el DOM se manipula de forma imperativa.
Este plan describe cómo implementar el mismo wizard con Alpine.js para hacerlo más
mantenible y fácil de extender.

## Por qué Alpine.js

- Sin build step — un `<script src="cdn">` en el HTML
- Estado centralizado y reactivo en un objeto JS (`x-data`)
- Agregar/reordenar un paso = una línea de template + una entrada en el objeto de estado
- Compatible con los templates Jinja2 actuales (Flask)
- ~15 KB, sin node_modules, sin bundler

## Archivos afectados

| Archivo | Cambio | Riesgo |
|---|---|---|
| `templates/publish.html` | Reescritura completa con Alpine | BAJO — solo UI |
| `config.py` | Agregar `apply_group_filter()` (ya en `produccion_temp`) | BAJO |
| `job_store.py` | Tabla `templates`, columna `group_ids`, helpers multi-foto (ya en `produccion_temp`) | MEDIO |
| `api_server.py` | Endpoints de plantillas, pipeline `group_ids`, `skip_hour_check` (ya en `produccion_temp`) | MEDIO |
| `account_manager.py` | Soporte `skip_hour_check` e `image_paths` multi-foto (ya en `produccion_temp`) | BAJO |

Los cambios de backend (últimas 4 filas) se portan directamente desde `produccion_temp`
sin modificación. Solo `publish.html` se implementa desde cero con Alpine.

## Arquitectura del wizard

### Estado global (`x-data="publishWizard()"`)

```js
function publishWizard() {
  return {
    // Navegación
    step: 'accounts',           // 'accounts' | 'template' | 'groups' | 'schedule' | 'confirm'
    steps: ['accounts', 'template', 'groups', 'schedule', 'confirm'],

    // Datos por paso
    accounts: [],               // lista cargada del servidor
    selectedAccounts: [],       // nombres seleccionados
    templates: [],              // lista cargada del servidor
    selectedTemplate: null,     // objeto plantilla o null
    groupsByAccount: {},        // { account_name: [{id, tag}, ...] }
    selectedGroups: {},         // { account_name: ['gid1', 'gid2'] }
    scheduleMode: 'now',        // 'now' | 'scheduled'
    scheduledFor: '',           // datetime-local string
    imageFiles: [],             // File[] subidos por el usuario
    templateImagePaths: [],     // paths del servidor (plantilla)

    // Estado UI
    publishing: false,
    statusMsg: '',
    statusType: '',             // 'ok' | 'error' | 'info'
    jobId: null,
  }
}
```

### Pasos y navegación

```html
<!-- Tab indicadores -->
<div class="wizard-steps">
  <template x-for="(s, i) in steps" :key="s">
    <div :class="{ active: step === s, done: stepIndex() > i }"
         x-text="stepLabel(s)"></div>
  </template>
</div>

<!-- Contenido de cada paso -->
<div x-show="step === 'accounts'" x-transition>...</div>
<div x-show="step === 'template'" x-transition>...</div>
<div x-show="step === 'groups'"   x-transition>...</div>
<div x-show="step === 'schedule'" x-transition>...</div>
<div x-show="step === 'confirm'"  x-transition>...</div>
```

Navegación:
```js
next() {
  const i = this.steps.indexOf(this.step);
  if (this.validate()) this.step = this.steps[i + 1];
},
prev() {
  const i = this.steps.indexOf(this.step);
  if (i > 0) this.step = this.steps[i - 1];
},
validate() {
  // Por paso: retorna true/false y setea statusMsg si falla
  if (this.step === 'accounts') return this.selectedAccounts.length > 0;
  if (this.step === 'groups')   return Object.values(this.selectedGroups).some(g => g.length > 0);
  return true;
},
```

### Paso 1 — Cuentas

- `init()` llama `fetch('/admin/api/accounts')` y puebla `this.accounts`
- Chips con `:class="{ checked: selectedAccounts.includes(a.name) }"`
- Click en chip: toggle en `selectedAccounts`
- Al hacer `next()`: carga grupos de cada cuenta seleccionada en `groupsByAccount`

### Paso 2 — Plantilla (opcional)

- Galería de tarjetas (`templates-gallery`) con `x-for="t in templates"`
- Seleccionar plantilla: `selectedTemplate = t`, pre-rellena texto e imágenes
- Botón "Sin plantilla" → `selectedTemplate = null`, continua
- Preview en modal: `x-show="previewModal"` con bind al template activo

### Paso 3 — Grupos

- Renderizado dinámico desde `groupsByAccount` (poblado al salir del paso 1)
- Por cada cuenta: sección con sus grupos como checkboxes
- `selectedGroups[account]` sincronizado con Alpine reactivity
- "Seleccionar todos" / "Ninguno" por cuenta

### Paso 4 — Cuándo

- Radio: "Ahora" / "Programar"
- `x-show="scheduleMode === 'scheduled'"` para el datetime input
- Validación: si programado, fecha debe ser futura

### Paso 5 — Confirmar y publicar

- Resumen reactivo (leído directo del estado):
  - Cuentas seleccionadas
  - Plantilla elegida (o "Sin plantilla")
  - Grupos por cuenta
  - Hora de publicación
- Botón "Publicar" → llama `publish()`
- `publish()` arma el `FormData` con `group_ids` como JSON y dispara el endpoint
- Poll de estado con `setInterval` hasta `done/failed`

## Agregar un paso futuro

1. Añadir el ID al array `steps`
2. Añadir `<div x-show="step === 'nuevo-paso'">` en el HTML
3. Añadir label en `stepLabel(s)`
4. Añadir validación en `validate()` si aplica

Sin tocar lógica de navegación ni CSS de tabs.

## Orden de implementación

```
Bloque A — Backend (portar desde produccion_temp, sin modificar)
  A1. config.py         — apply_group_filter()            BAJO
  A2. job_store.py      — templates table + group_ids     MEDIO
  A3. account_manager   — skip_hour_check + image_paths   BAJO
  A4. api_server.py     — endpoints plantillas + group_ids MEDIO

Bloque B — Frontend
  B1. publish.html      — reescritura con Alpine.js       BAJO
```

Cada bloque termina con su propio commit antes de continuar al siguiente.

## Notas

- Alpine.js se carga desde CDN: `https://cdn.jsdelivr.net/npm/alpinejs@3.x.x/dist/cdn.min.js`
  con `defer`. Sin instalación.
- El contrato de API no cambia — `publish()` envía los mismos campos que hoy
  (`text`, `accounts`, `image`, `image_paths`, `scheduled_for`, `group_ids`).
- `group_ids` se envía como JSON string en `FormData`: `fd.append('group_ids', JSON.stringify(selectedGroups))`
- La tabla `templates` y la columna `group_ids` en `jobs` requieren migración de DB
  (los `ALTER TABLE` en `job_store.py` ya los incluyen con `IF NOT EXISTS`/try-except).
