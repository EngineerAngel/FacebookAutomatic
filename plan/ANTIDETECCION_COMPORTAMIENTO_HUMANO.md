# Simulación de Comportamiento Humano — Análisis de Efectividad por Tipo de Penalización

> Fecha: 2026-04-26
> Contexto: Evaluación del stack antidetección actual y su relevancia por vector de riesgo.

---

## 1. Capas de detección de Facebook

Facebook opera con 5 capas independientes. La simulación humana cubre **L3 completo** y toca L2 marginalmente.

```
L1 — Red           IP, ASN, /24 subnet, TLS fingerprint (JA3)
L2 — Dispositivo   Browser fingerprint, Canvas, WebGL, fonts, cookies
L3 — Comportamiento  Mouse, scroll, timing, navegación, session patterns
L4 — Contenido     Texto duplicado, hash de imagen, URLs, spam lexicon
L5 — Social/Trust  Engagement rate, reportes, edad cuenta, historial
```

---

## 2. Análisis por tipo de penalización

---

### 2.1 Restricciones temporales de funciones (comentar / compartir)

**Simulación humana: ESENCIAL**

Facebook mide:
- Intervalo entre acciones del mismo tipo (comentar N veces en M minutos)
- Patrón de navegación: ¿llegó al grupo directo o venía del feed?
- Tiempo entre llegar a la página y ejecutar la acción
- Si la acción siempre ocurre en la misma posición del DOM

Las restricciones temporales son casi siempre **velocity + behavioral (L3)**. Un bot que comenta 3 publicaciones en 90 segundos con timing uniforme activa esto aunque tenga proxy y fingerprint perfectos.

**Cobertura actual del stack:**

| Mecanismo | Archivo | Estado |
|---|---|---|
| `idle_probability` entre grupos | `config.py` | ✅ |
| `warmup_probability` antes de publicar | `config.py` | ✅ |
| `wait_between_groups` 30–60 s | `config.py` | ✅ |
| Tiempo de lectura proporcional al contenido | — | ❌ Falta |

**Gap principal:** el warmup usa duración fija (8–25 s), no proporcional al texto visible. Un humano tarda más en leer un post largo que uno corto.

---

### 2.2 Inhabilitación de cuentas personales

**Simulación humana: NECESARIA pero NO suficiente**

La inhabilitación es la penalización más severa. Facebook la aplica cuando se acumulan señales de **múltiples capas simultáneamente**. Una sola capa perfecta no la previene.

| Señal que más pesa | Capa | Cobertura actual |
|---|---|---|
| IP compartida con cuentas ya baneadas | L1 | ✅ (proxy pool) |
| Fingerprint idéntico a otra cuenta activa | L2 | Parcial — 15 perfiles, se reutilizan si hay más cuentas |
| Login desde IP/device nunca visto | L2+L3 | ✅ (profile persistente por cuenta) |
| Acción automatizada detectada en sesión | L3 | ✅ (simulación humana) |
| Contenido repetido en múltiples grupos | L4 | ✅ (text variation Gemini) |
| Mini-bans sin cooldown respetado | L5 | ✅ (`ban_cooldown_until` en DB) |
| Cuenta nueva con actividad alta desde día 1 | L5 | ❌ No hay período de maduración |

**Gap crítico:** no existe un período de "maduración" para cuentas nuevas. Una cuenta que el día 1 publica en 5 grupos es sospechosa sin importar cuánto imite el comportamiento humano. Este gap no lo resuelve la simulación sino el **tiempo y volumen gradual**.

**Recomendación:** implementar `account_age_days` en DB y escalar `max_groups_per_session` según antigüedad:
- Días 1–7: máx 1 grupo/día
- Días 8–30: máx 2–3 grupos/día
- Días 30+: máx 5 grupos/día (actual)

---

### 2.3 Bloqueos de páginas o perfiles comerciales

**Simulación humana: OPCIONAL**

Las páginas tienen un modelo de detección distinto. Facebook tolera más la automatización de páginas (Meta Business Suite la hace legítimamente), pero es más estricto con:

- **CIB (Comportamiento Inauténtico Coordinado):** múltiples páginas publicando el mismo contenido con la misma redacción en el mismo horario desde IPs relacionadas.
- **Violaciones de política de contenido:** el texto activa moderación automática independientemente de cómo se posteó.
- **Engagement artificialmente bajo:** publicar mucho sin interacciones escala a revisión manual.

La simulación humana de L3 casi no importa para este tipo de bloqueo.

| Lo que más pesa | Capa | Cobertura actual |
|---|---|---|
| Texto único por grupo | L4 | ✅ `TextVariator` (Gemini) |
| IP diferente por cuenta | L1 | ✅ proxy pool |
| Espaciado temporal entre publicaciones | L3 | ✅ parcial |
| Variación en **hora** de publicación entre cuentas | L3 | ❌ Todas corren en el mismo job |

**Gap:** cuando hay múltiples cuentas en un job, publican con segundos de diferencia entre ellas. Un coordinador humano real las separaría por minutos u horas. Añadir un offset random de 10–30 min por cuenta antes de iniciar su sesión reduciría la correlación temporal.

---

### 2.4 Shadow ban (reducción de visibilidad sin notificación)

**Simulación humana: BAJA incidencia directa**

El shadow ban es un **algoritmo de scoring de contenido y cuenta (L4+L5)**, no de detección de automatización. Ninguna mejora en mouse o timing lo resuelve directamente.

| Factor | Capa | Peso | Cobertura actual |
|---|---|---|---|
| Texto idéntico o parafraseo obvio cross-grupos | L4 | 🔴 Muy alto | ✅ Gemini paraphrase |
| Ratio engagement/alcance históricamente bajo | L5 | 🔴 Muy alto | Parcial — Gemini comments generan algo |
| Palabras del spam lexicon (gratis, oferta, precio, etc.) | L4 | 🟡 Alto | ❌ Sin filtro de lexicon |
| Publicación excesiva en grupos grandes (>50k miembros) | L3+L5 | 🟡 Medio | ❌ Sin clasificación por tamaño de grupo |
| Cuenta sin historial de engagement orgánico | L5 | 🟡 Medio | Parcial — Gemini comments ayudan |
| Automatización de sesión detectada | L3 | 🟢 Bajo | ✅ simulación humana |

**El único componente que ataca el shadow ban directamente:**
`GeminiCommenter._post_gemini_comment_on_random_article` — genera engagement social real bidireccional que sube el trust score L5 de la cuenta.

**Gaps accionables:**
1. Filtrar palabras del spam lexicon antes de enviar texto a Gemini para parafraseo.
2. Clasificar grupos por tamaño y reducir frecuencia en los >50k miembros.
3. Incrementar `gemini_comment_probability` gradualmente para cuentas nuevas (warmup social).

---

## 3. Tabla resumen ejecutivo

| Penalización | L3 Simulación humana | Qué la previene más | Urgencia de mejora |
|---|---|---|---|
| Restricción temporal de funciones | **Esencial** | L3 + rate limits | Media — ya cubierto en >70% |
| Inhabilitación de cuenta | **Necesaria, no suficiente** | L1 + L2 + L3 + L5 juntos | Alta — falta maduración de cuentas |
| Bloqueo de página / comercial | **Opcional** | L4 (text variation) + L1 (proxy) | Media — falta offset temporal entre cuentas |
| Shadow ban | **Irrelevante en gran parte** | L4 (contenido único) + L5 (Gemini comments) | Alta — falta filtro spam lexicon |

---

## 4. Mejoras de simulación humana priorizadas por ROI

### Impacto × facilidad de implementación

| # | Mejora | Penalización que mitiga | Archivo objetivo | Estimado |
|---|---|---|---|---|
| 1 | Tiempo de lectura proporcional al contenido | Restricción temporal | `human_browsing.py` | 30 min |
| 2 | Movimiento idle del mouse entre acciones | Restricción temporal + inhabilitación | `facebook_poster_async.py` | 1 h |
| 3 | Scroll variable (wheel events vs smooth) | Restricción temporal | `human_browsing.py` | 45 min |
| 4 | Comportamiento post-publicación ("ver el post") | Inhabilitación | `facebook_poster_async.py` | 30 min |
| 5 | Think time antes de tipear (log-normal) | Restricción temporal | `facebook_poster_async.py` | 10 min |
| 6 | Decaimiento de velocidad de escritura al final | Inhabilitación | `facebook_poster_async.py` | 20 min |
| 7 | Offset temporal random entre cuentas en mismo job | Bloqueo comercial | `account_manager_async.py` | 20 min |
| 8 | Maduración gradual de cuentas nuevas | Inhabilitación | `config.py` + `job_store.py` | 2–3 h |
| 9 | Filtro de spam lexicon en texto antes de publicar | Shadow ban | `text_variation.py` | 1 h |
| 10 | Clasificación de grupos por tamaño y límite diferenciado | Shadow ban | `config.py` + `facebook_poster_async.py` | 2 h |

---

## 5. Lo que NO resuelve la simulación humana

Documentado para evitar trabajo con retorno cero:

- **Shadow ban por contenido repetitivo:** ningún nivel de realismo en el comportamiento compensa que el hash del texto o la imagen sea idéntico. L4 es independiente de L3.
- **Inhabilitación por IP compartida:** antes del proxy pool, mejorar el mouse no servía de nada. La capa de red siempre manda primero.
- **Canvas / WebGL fingerprint:** no se puede falsificar de forma confiable desde `add_init_script`; requiere modificación del binario (fuera del alcance del stack actual).
- **Engagement orgánico:** los Gemini comments son un sustituto imperfecto. Facebook sabe la diferencia entre comentarios de cuentas con trust score bajo y comentarios de cuentas reales con historial.

---

## 6. Configuración actual relevante (referencia rápida)

```python
# config.py — parámetros que afectan directamente L3
"warmup_probability": 0.60,          # 60% de grupos tienen warmup
"gemini_comment_probability": 0.20,  # 20% de warmups postean comentario
"gemini_comment_max_per_session": 2, # máximo 2 comentarios por run
"idle_probability": 0.20,            # 20% de pausa aleatoria entre grupos
"wait_between_groups_min": 30,       # segundos
"wait_between_groups_max": 60,
"wait_between_accounts_min": 60,
"wait_between_accounts_max": 120,
"max_posts_per_account_per_hour": 3,
"max_posts_per_account_per_day": 15,
"max_groups_per_session": 5,
"text_variation_mode": "gemini",     # "gemini" | "zero_width" | "off"
```

> **Nota:** `zero_width` no funciona contra el tokenizador de Facebook — Facebook los elimina antes de hashear. Usar siempre `"gemini"` si hay API key disponible.
