# Decisión pendiente — Ítem 3.2 (FastAPI)

> Generado para discusión antes de implementar. Responder al final del documento.

---

## ¿Qué es FastAPI y por qué aparece en el plan?

Actualmente el sistema tiene una puerta de entrada (Flask) que recibe las órdenes de OpenClaw y las procesa. Flask fue la elección inicial — funciona, pero tiene limitaciones cuando el sistema crece.

FastAPI es simplemente una puerta de entrada más moderna que hace lo mismo pero mejor en tres aspectos concretos:

### 1. Validación automática de datos
Con Flask, si OpenClaw envía una orden malformada (sin texto, con un número donde debe ir texto), el sistema puede crashear silenciosamente o dar errores confusos. Con FastAPI, cualquier orden incorrecta recibe una respuesta clara inmediatamente: *"falta el campo 'text'"*, *"'accounts' debe ser una lista"*. Menos debugging, menos sorpresas.

### 2. Documentación automática
FastAPI genera automáticamente una página en `/docs` que muestra todos los endpoints disponibles, qué datos aceptan y qué responden. Útil cuando OpenClaw o tú mismo quieran ver exactamente qué acepta el sistema sin leer código.

### 3. Base para crecer
Cuando llegue el momento de separar la API de los workers (3.7) o agregar métricas (3.3b), FastAPI encaja mejor con esa arquitectura.

---

## ¿Es necesario ahora?

**No.** El sistema funciona perfectamente con Flask. OpenClaw ya funciona, los jobs se procesan, los webhooks llegan.

3.2 es una inversión a futuro: si el volumen sube (más cuentas, más peticiones, más integraciones), tener FastAPI ya montado en `/v2` permite migrar gradualmente sin apagar nada.

---

## Alternativas si se pospone 3.2

Hay dos ítems con impacto operacional más inmediato:

| Opción | Qué resuelve | Cuándo importa |
|--------|-------------|----------------|
| **3.4 — Auto-reparación DOM** | Cuando Facebook cambie algo, el sistema lo detecta y Gemini sugiere la corrección. Un humano aprueba antes de aplicar. | Cada vez que FB actualice su interfaz (ocurre cada pocas semanas) |
| **3.3b — Métricas** | Ver en tiempo real: tasa de éxito por cuenta, latencia, cuentas activas, alertas de ban. | Cuando se escale a más cuentas o se quiera operar sin revisar logs |

---

## Respuesta

> ¿Implementar 3.2 ahora, o pasar a 3.4 / 3.3b?

**[✅]** Implementar en orden: 3.2 → 3.3b → 3.4  
**[ ]** Saltar a 3.4 directamente

---

## Justificación de la decisión

**Fecha de decisión:** 2026-05-01

### Por qué 3.4 antes que 3.2

| Criterio | 3.2 FastAPI | 3.4 Auto-reparación DOM |
|----------|------------|------------------------|
| ¿Resuelve un problema recurrente hoy? | No — Flask funciona | ✅ Sí — Facebook cambia cada pocas semanas |
| ¿Bloquea algo urgente? | No | No |
| ¿ROI inmediato? | No (arquitectura futura) | ✅ Sí (menos debugging manual) |
| ¿Tiene dependencias previas? | Requiere async (✅ ya completo) | Independiente — se puede empezar ya |

### El factor Scrapling

Durante la investigación de la sesión 2026-05-01 se evaluó la librería **Scrapling** (adaptive DOM parsing). El análisis determinó que encaja exactamente en 3.4, no antes.

Scrapling aporta una capa que faltaba en el plan original de 3.4:

```
Plan original:
  3.4 = DOM snapshots + tests + Gemini (semi-automático)

Plan actualizado (con Scrapling):
  3.4 = DOM snapshots + tests + Scrapling (primera línea, automático) + Gemini (fallback)
```

Esto convierte 3.4 en un sistema de dos capas:
- **Capa 1 — Scrapling adaptive parsing:** detecta y repara selectores rotos automáticamente cuando el cambio es estructural menor (clase CSS cambiada, elemento movido).
- **Capa 2 — Gemini:** cuando Scrapling no puede recuperar el elemento (cambio radical de diseño), Gemini analiza el HTML y sugiere candidatos. Un humano aprueba antes de aplicar en producción.

Adicionalmente, el módulo `adaptive_selector.py` que se implementará en 3.4 se diseñará genérico (no acoplado a Facebook) para permitir su extracción posterior como librería pública independiente (`py-adaptive-playwright`).

### Por qué Flask/3.2 puede esperar

- Flask funciona. OpenClaw no tiene problemas con la interfaz actual.
- FastAPI es una inversión arquitectónica válida, pero no urgente.
- 3.2 se implementará después de 3.4 (y después de que Scrapling esté validado en producción).

### Referencia técnica

Ver [SCRAPLING_REFERENCE.md](SCRAPLING_REFERENCE.md) para el detalle completo de:
- Cómo funciona el adaptive parsing internamente
- El patrón `AdaptivePlaywrightBridge` (puente Scrapling↔Playwright)
- Plan de implementación para 3.4
- Estrategia de extracción como librería pública
