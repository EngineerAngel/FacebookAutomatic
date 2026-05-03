# Scrapling — Referencia técnica e implementación en 3.4

> **Creado:** 2026-05-01  
> **Estado:** ✅ Implementado — Ítem 3.4 completado (2026-05-01)  
> **Propósito:** Referencia técnica de cómo funciona el sistema de DOM repair en producción. Útil para debugging, extensión del sistema, o entender decisiones de diseño.

---

## 1. Qué es Scrapling (solo lo relevante)

Scrapling es un **parser HTML con memoria adaptativa**. No es un scraper completo ni un framework de automatización. Su valor para este proyecto es una sola capacidad: **encontrar un elemento en el DOM aunque su selector CSS/XPath haya cambiado**, usando un fingerprint guardado de la primera vez que lo vio.

**Lo que NO hace:**
- No reemplaza Playwright — no puede hacer clic, escribir, ni manejar sesiones de navegador.
- No entiende JavaScript en tiempo real — trabaja sobre HTML estático (snapshot del DOM).
- No monitorea cambios — es pasivo, se activa cuando tú lo llamas.

**Repositorio:** github.com/D4Vinci/Scrapling  
**Licencia:** MIT

---

## 2. Cómo funciona el Adaptive Parsing internamente

### 2.1 Fase de entrenamiento (`auto_save=True`)

La primera ejecución "enseña" a Scrapling cómo es el elemento:

```python
from scrapling import Adaptor

html = await page.content()  # snapshot del DOM en este momento
doc = Adaptor(html, url=page.url)

# auto_save=True → guarda el fingerprint del elemento encontrado
boton = doc.css('button.xrcdw81', auto_save=True)
```

Scrapling toma el **primer elemento** que matchea y guarda en SQLite:

```
{
  "tag":        "button",
  "text":       "Publicar",
  "attributes": {"data-testid": "post_button", "class": "xrcdw81 x2y6brl"},
  "parent_tag": "div",
  "siblings":   ["span", "svg"],
  "path":       ["div", "div", "div", "button"]
}
```

**Dónde se guarda:** archivo SQLite local, por defecto `.scrapling.db` en el directorio de trabajo. La ruta es configurable al construir el `Adaptor`. Se indexa por `dominio` (extraído de la URL) + `identificador` (el selector CSS/XPath usado).

### 2.2 Fase de recuperación (`adaptive=True`)

En ejecuciones posteriores, si el selector falla:

```python
html_nuevo = await page.content()
doc2 = Adaptor(html_nuevo, url=page.url)

boton = doc2.css('button.xrcdw81', adaptive=True)
# → si falla el selector: activa recuperación por fingerprint
```

**Flujo interno:**

```
css('button.xrcdw81') → 0 resultados
  │
  ├─ Recupera fingerprint de la DB (domain=facebook.com, id='button.xrcdw81')
  │
  ├─ Itera TODOS los elementos del DOM actual
  │     Para cada elemento:
  │       score = similarity(elemento_actual, fingerprint_guardado)
  │       if score > threshold: candidatos.append(elemento)
  │
  └─ Devuelve candidatos ordenados por score (mayor primero)
```

**Algoritmo de similarity** — compara estas 5 dimensiones:

| Dimensión | Peso relativo | Qué compara |
|-----------|--------------|-------------|
| Tag name | Alto | `button` vs `button` |
| Texto visible | Alto | "Publicar" vs "Publicar" |
| Atributos | Medio | `data-testid`, `aria-label`, etc. |
| Hermanos (siblings) | Medio | Tags adyacentes en el DOM |
| Path (ancestros) | Bajo | Secuencia de tags hasta la raíz |

El threshold por defecto es ~0.75 (75% de similitud). Es configurable.

### 2.3 Limitaciones concretas para Facebook

| Limitación | Impacto real |
|-----------|-------------|
| Solo guarda el **primer elemento** matcheado | Si el selector devolvía varios elements, solo aprende el primero |
| Fingerprint basado en **texto visible** | Botones con solo iconos SVG (sin texto) tienen fingerprint débil |
| **No entiende React state** | Si el elemento existe pero está oculto (`display:none`), lo incluye como candidato |
| **No opera sobre shadow DOM** | Facebook usa algunos shadow DOM — esos elementos son invisibles para Scrapling |
| El fingerprint es **estático** | Si cambias qué elemento esperas encontrar, hay que borrar la DB y reentrenar |

---

## 3. El puente con Playwright (`AdaptivePlaywrightBridge`)

### 3.1 Por qué Playwright sigue siendo necesario

Scrapling opera sobre HTML estático. Playwright es el que ejecuta JavaScript, mantiene sesiones de cookie, simula mouse/teclado, y hace posible interactuar con Facebook. No son competidores — son capas complementarias:

```
Playwright   → abre el navegador, hace login, navega, hace clic
Scrapling    → cuando un selector rompe, encuentra dónde está el elemento ahora
```

### 3.2 Estrategia de extracción de identificadores

Scrapling devuelve un objeto `lxml.etree.Element`. Playwright necesita un selector o locator. El puente extrae un identificador del elemento encontrado:

| Nivel | Estrategia | Cuándo usar | Robustez |
|-------|-----------|-------------|----------|
| 1 | `element.attrib['data-testid']` | Si el elemento tiene atributo único | ✅ Alta |
| 2 | `element.text_content().strip()` | Texto visible distintivo | ✅ Media |
| 3 | XPath reconstruido (`lxml.etree`) | Último recurso | ⚠️ Frágil |

### 3.3 Patrón `AdaptivePlaywrightBridge`

```python
# facebook_auto_poster/adaptive_selector.py
#
# Módulo diseñado genérico (no acoplado a Facebook) para permitir extracción
# futura como librería pública (py-adaptive-playwright).

from scrapling import Adaptor
from lxml import etree
from playwright.async_api import Page


class AdaptivePlaywrightBridge:
    """
    Puente entre Scrapling (parsing adaptativo) y Playwright (interacción).

    Uso:
        html = await page.content()
        doc = Adaptor(html, url=page.url)
        bridge = AdaptivePlaywrightBridge(page, doc)

        await bridge.click('button.xrcdw81')
        await bridge.fill('textarea[aria-label*="Escribe"]', texto)
    """

    def __init__(self, page: Page, doc: Adaptor):
        self.page = page
        self.doc = doc

    async def click(self, selector: str, adaptive: bool = True) -> None:
        element = self.doc.css(selector, adaptive=adaptive)
        if not element:
            raise LookupError(f"Elemento no encontrado: {selector}")
        locator = self._to_playwright_locator(element)
        await locator.click()

    async def fill(self, selector: str, text: str, adaptive: bool = True) -> None:
        element = self.doc.css(selector, adaptive=adaptive)
        if not element:
            raise LookupError(f"Campo no encontrado: {selector}")
        locator = self._to_playwright_locator(element)
        await locator.fill(text)

    def _to_playwright_locator(self, element):
        # Nivel 1: atributo único
        testid = element.attrib.get('data-testid')
        if testid:
            return self.page.get_by_test_id(testid)

        # Nivel 2: texto visible
        text = (element.text_content() or '').strip()
        tag = element.tag
        if text:
            return self.page.get_by_role(tag, name=text)

        # Nivel 3: XPath (frágil, último recurso)
        xpath = etree.ElementTree(element).getroottree().getpath(element)
        return self.page.locator(f'xpath={xpath}')
```

### 3.4 Integración en `facebook_poster_async.py` — puntos concretos

Los selectores más propensos a romperse cuando Facebook actualiza su interfaz:

| Selector actual | Elemento | Prioridad para adaptive |
|----------------|----------|------------------------|
| `[aria-label="Crear una publicación"]` | Botón abrir composer | 🔴 Alta |
| `div[contenteditable="true"]` | Campo de texto del post | 🔴 Alta |
| Botón "Publicar" | Enviar el post | 🔴 Alta |
| `input[type="file"]` | Upload de imagen | 🟡 Media |
| Confirmación de post publicado | Verificación de éxito | 🟡 Media |

Patrón de uso en `facebook_poster_async.py`:

```python
async def _open_composer(self):
    html = await self.page.content()
    doc = Adaptor(html, url=self.page.url, auto_save=True)  # entrenamiento
    bridge = AdaptivePlaywrightBridge(self.page, doc)
    await bridge.click('[aria-label="Crear una publicación"]')

async def _write_post(self, text: str):
    html = await self.page.content()
    doc = Adaptor(html, url=self.page.url, adaptive=True)   # recuperación
    bridge = AdaptivePlaywrightBridge(self.page, doc)
    await bridge.fill('div[contenteditable="true"]', text)
    await bridge.click('button:has-text("Publicar")')
```

---

## 4. Archivos implementados (Ítem 3.4 — completado 2026-05-01)

### 4.1 Archivos creados

| Archivo | Descripción |
|---------|-------------|
| `facebook_auto_poster/adaptive_selector.py` | Módulo `AdaptivePlaywrightBridge` (genérico) |
| `facebook_auto_poster/selector_repair.py` | `SelectorRepairService` — llama Gemini, parsea candidatos JSON, escribe a DB |
| `tests/unit/test_adaptive_selector.py` | 11 tests unitarios del bridge — todos pasan |
| `tests/dom_snapshots/README.md` | Instrucciones para capturar y sanitizar snapshots |
| `scripts/scrub_snapshot.py` | Sanitizador de snapshots (elimina tokens/IDs personales) |

### 4.2 Archivos modificados

| Archivo | Cambio |
|---------|--------|
| `facebook_auto_poster/job_store.py` | Tabla `selector_repairs` + 5 funciones CRUD |
| `facebook_auto_poster/facebook_poster_async.py` | 6 selectores críticos envueltos con `AdaptivePlaywrightBridge` |
| `facebook_auto_poster/api_server.py` | Endpoints `/admin/api/selector-repairs` (list/approve/reject) |
| `facebook_auto_poster/templates/admin.html` | Tab "Mantenimiento" con tabla de repairs pendientes |
| `facebook_auto_poster/config.py` | Flag `adaptive_selectors_enabled` |
| `facebook_auto_poster/requirements.txt` | `scrapling>=0.3,<1.0` (sin `[fetchers]`) |

**Activación:** `ADAPTIVE_SELECTORS=1` en `.env` (default OFF — sin la flag, zero overhead).

---

## 5. Estrategia de entrenamiento y fallback

### 5.1 Ciclo de vida de un selector

```
1ª ejecución (auto_save=True):
  Selector encuentra elemento → Scrapling guarda fingerprint → Playwright actúa

Ejecuciones siguientes (adaptive=True):
  Selector encuentra elemento → Playwright actúa  (camino feliz)

Selector falla (Facebook cambió):
  Selector → 0 resultados → Scrapling busca por fingerprint
    │
    ├─ score > 0.75 → devuelve candidato → Playwright actúa ✅
    │
    └─ score < 0.75 (o 0 candidatos) → TimeoutError capturado
         │
         └─ ESCALA A GEMINI (ver 5.2)
```

### 5.2 Escalado a Gemini (cuando Scrapling falla)

Cuando `AdaptivePlaywrightBridge` no puede recuperar el elemento:

```
TimeoutError / LookupError en selector conocido
  → Capturar HTML actual de la página
  → Enviar a Gemini: "el selector X ya no funciona, ¿dónde está este elemento?"
  → Gemini devuelve candidatos con nivel de confianza
  → Guardar en DB como 'selector_repair' con estado 'pendiente_aprobacion'
  → Notificar al admin (log WARNING + alerta en panel)
  → Admin aprueba desde /admin → se activa en la siguiente ejecución
```

Este flujo (Gemini + aprobación humana) ya estaba planificado en 3.4. Scrapling es la capa previa que evita que el 90% de los casos lleguen a Gemini.

### 5.3 Tabla de escenarios

| Escenario | Scrapling | Gemini | Acción manual |
|-----------|----------|--------|--------------|
| Clase CSS cambiada | ✅ Resuelve solo | — | — |
| Elemento movido (misma estructura) | ✅ Resuelve solo | — | — |
| Atributo `data-testid` renombrado | ✅ Si el texto no cambió | — | — |
| Rediseño parcial (estructura diferente) | ⚠️ Puede fallar | ✅ Sugiere candidato | Admin aprueba |
| Rediseño total (nueva pantalla) | ❌ Falla | ✅ Sugiere candidato | Admin aprueba |
| Facebook bloquea el scraping | ❌ No aplica | ❌ No aplica | Debug manual |

---

## 6. Extracción futura como librería pública (post-3.4)

### 6.1 Propuesta

**Nombre:** `py-adaptive-playwright`  
**PyPI:** `pip install py-adaptive-playwright`  
**GitHub:** repo independiente, extraído de este proyecto

### 6.2 Qué contiene

```
py-adaptive-playwright/
├── src/
│   └── adaptive_playwright/
│       ├── __init__.py
│       └── bridge.py          # = adaptive_selector.py de este proyecto
├── tests/
│   └── test_bridge.py
├── pyproject.toml
└── README.md
```

**El módulo `adaptive_selector.py` ya es genérico por diseño** — la extracción es `cp` + boilerplate de packaging.

### 6.3 Audiencia objetivo

| Audiencia | Problema que resuelve |
|-----------|----------------------|
| Web scrapers | Selectores que rompen con cada update del sitio |
| RPA developers | Automatizaciones frágiles ante cambios de UI |
| Test automation | UI tests que fallan tras cada deploy |
| Bot developers | Bots que dejan de funcionar sin aviso |

### 6.4 Timeline de extracción

```
Implementar 3.4 (adaptive_selector.py en producción)
  ↓
2-3 semanas corriendo con Facebook real (validación)
  ↓
Sin regresiones → extraer a py-adaptive-playwright
  ↓
Publicar en PyPI + GitHub
```

**No antes.** El valor del portfolio es "probado en producción contra Facebook", no "experimental".

---

## 7. Compatibilidad y dependencias

### 7.1 Patchright

**Compatible.** La condición es instalar `scrapling` sin el extra `[fetchers]`:

| Instalación | Instala playwright/patchright | Conflicto |
|-------------|------------------------------|-----------|
| `pip install scrapling` | ❌ No | ✅ Sin conflicto |
| `pip install scrapling[fetchers]` | ✅ Sí (versiones pinadas) | ⚠️ Posible conflicto de versión |

Usar siempre el **base package**.

### 7.2 Dependencias que añade (base package)

Ninguna de estas está en `requirements.txt` actualmente, y ninguna conflictúa:

| Paquete | Versión mínima | Propósito |
|---------|---------------|-----------|
| `lxml` | 6.0.3 | Parser HTML subyacente |
| `cssselect` | 1.4.0 | Soporte de selectores CSS en lxml |
| `orjson` | 3.11.8 | Serialización de fingerprints en la DB |
| `tld` | 0.13.2 | Extrae el dominio de la URL para indexar en DB |
| `w3lib` | 2.4.1 | Utilidades de URLs y HTML |

### 7.3 Línea a añadir en `requirements.txt`

```
# Adaptive DOM parsing — puente Scrapling↔Playwright (Fase 3.4)
# Instalar solo el base package (sin [fetchers]) para evitar conflicto con patchright.
scrapling>=0.3,<1.0
```

Verificar versión estable disponible al momento de implementar con `pip index versions scrapling`.

### 7.4 Verificación rápida de compatibilidad

```bash
pip install scrapling
python -c "from scrapling import Adaptor; print('OK')"
python -c "from patchright.async_api import async_playwright; print('OK')"
```

Ambos deben imprimir `OK` sin errores de importación.
