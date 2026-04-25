# 📖 Guía: Extraer tus grupos de Facebook con JavaScript

---

## ✅ ¿Es seguro? ¿Te pueden banear?

### 🟢 Riesgo: MUY BAJO — casi nulo por estas razones:

| Factor | Detalle |
|--------|---------|
| **Es tu propia cuenta** | Solo lees datos que ya ves en pantalla |
| **No hay peticiones externas** | El script no hace llamadas a la API de Facebook |
| **No automatiza acciones** | No publica, no da like, no sigue a nadie |
| **Es ejecución manual** | Tú pegas el código manualmente en tu consola |
| **No hay bots** | No hay Selenium, no hay automatización de clics |

> ⚠️ Lo que **SÍ** puede causar baneo: usar bots, hacer scraping masivo, automatizar publicaciones, o usar herramientas de terceros que pidan tu contraseña. **Este método no hace nada de eso.**

---

## 🗺️ Proceso completo paso a paso

```
1. Abre Facebook en tu navegador
2. Ve a tu lista de grupos
3. Haz scroll para cargar todos
4. Abre la consola del navegador (F12)
5. Pega el script
6. Obtén tu lista
```

---

## 📂 Guía detallada

### PASO 1 — Ir a tus grupos
```
URL: https://www.facebook.com/groups/?category=joined
```

### PASO 2 — Cargar todos los grupos
- Haz **scroll hacia abajo** lentamente hasta ver todos tus grupos
- Espera a que cada sección cargue antes de seguir
- Si tienes +50 grupos, puede tomar un poco más

### PASO 3 — Abrir la consola del navegador

| Navegador | Atajo |
|-----------|-------|
| Chrome / Edge | `F12` → pestaña **Console** |
| Firefox | `F12` → pestaña **Consola** |
| Safari | `Cmd+Option+C` |

### PASO 4 — Ejecutar el script

---

## 🧩 El Script

```javascript
// ================================================
// EXTRACTOR DE GRUPOS DE FACEBOOK
// Uso: Pegar en consola del navegador (F12)
// Página: facebook.com/groups/?category=joined
// ================================================

var links = document.querySelectorAll('a[href*="/groups/"]');
var results = [];
var seen = {};

// Palabras clave a ignorar (no son grupos reales)
var excluded = {
  'feed': 1, 'discover': 1, 'joins': 1,
  'notifications': 1, 'pending': 1,
  'joined': 1, 'category': 1, 'create': 1
};

// Recorrer todos los enlaces encontrados
for (var i = 0; i < links.length; i++) {
  var href = links[i].href;

  // Extraer el ID de la URL
  var match = href.match(/groups\/([^/?#]+)/);
  if (!match) continue;

  var id = match[1];

  // Saltar si ya fue procesado o es una ruta del sistema
  if (excluded[id] || seen[id]) continue;
  seen[id] = 1;

  // Obtener el nombre del grupo desde el contenedor
  var container = links[i].closest('div');
  var name = container
    ? container.innerText.split('\n')[0].trim()
    : '(sin nombre)';

  results.push({
    nombre: name,
    id: id,
    url: 'https://facebook.com/groups/' + id
  });
}

// Mostrar como tabla en consola
console.table(results);
console.log('Total encontrados:', results.length);

// Exportar como CSV y descargar automáticamente
var csv = 'Nombre,ID,URL\n';
results.forEach(function(g) {
  csv += '"' + g.nombre + '","' + g.id + '","' + g.url + '"\n';
});

var blob = new Blob([csv], { type: 'text/csv;charset=utf-8;' });
var a = document.createElement('a');
a.href = URL.createObjectURL(blob);
a.download = 'mis_grupos_facebook.csv';
document.body.appendChild(a);
a.click();
document.body.removeChild(a);
```

---

## 📤 ¿Qué produce el script?

### En consola (`console.table`):
```
┌─────┬──────────────────────┬──────────────────┬─────────────────────────────┐
│ idx │ nombre               │ id               │ url                         │
├─────┼──────────────────────┼──────────────────┼─────────────────────────────┤
│  0  │ Claude Code México   │ 1383546083816551 │ https://facebook.com/groups/│
│  1  │ Roomies Queretaro    │ 1570349816330640 │ https://facebook.com/groups/│
└─────┴──────────────────────┴──────────────────┴─────────────────────────────┘
```

### Archivo descargado:
```
mis_grupos_facebook.csv
```

---

## 🔍 ¿Cómo funciona el script? (explicación técnica)

```
querySelectorAll('a[href*="/groups/"]')
  └─ Busca todos los <a> cuya href contenga "/groups/"

href.match(/groups\/([^/?#]+)/)
  └─ Extrae el ID de la URL usando expresión regular
     Ejemplo: /groups/123456  →  ID = "123456"

closest('div')
  └─ Sube al contenedor padre más cercano
     para leer el nombre del grupo

Blob + a.click()
  └─ Crea un archivo CSV en memoria
     y simula un clic para descargarlo
```

---

## ⚠️ Limitaciones conocidas

| Problema | Causa | Solución |
|----------|-------|----------|
| Grupos sin nombre | Facebook carga nombres de forma dinámica | Visitar el grupo individualmente |
| IDs como texto | Algunos grupos usan alias en lugar de números | También son válidos para URLs |
| Lista incompleta | No hiciste scroll suficiente | Bajar hasta el final antes de ejecutar |
| Grupos duplicados | El script ya los filtra con `seen` | — |

---

## 💡 Tip extra: Guardar como `.md`

Si quieres el resultado en Markdown en vez de CSV, reemplaza la parte del CSV por:

```javascript
var md = '# Mis Grupos de Facebook\n\n';
md += '| # | Nombre | ID | URL |\n';
md += '|---|--------|----|-----|\n';
results.forEach(function(g, i) {
  md += '| ' + (i+1) + ' | ' + g.nombre + ' | `' + g.id + '` | [Ver]('+g.url+') |\n';
});

var blob = new Blob([md], { type: 'text/markdown' });
var a = document.createElement('a');
a.href = URL.createObjectURL(blob);
a.download = 'mis_grupos_facebook.md';
document.body.appendChild(a);
a.click();
document.body.removeChild(a);
```