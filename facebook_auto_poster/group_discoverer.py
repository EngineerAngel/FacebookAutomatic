"""
group_discoverer.py — Descubrimiento autónomo de grupos de Facebook.

Ejecuta el script de plan/grupos.md (DOM scraping seguro, sin API calls).
Navega a facebook.com/groups/joined, hace scroll y extrae IDs.
"""

import asyncio
import logging
from config import AccountConfig, CONFIG
from facebook_poster_async import FacebookPosterAsync

logger = logging.getLogger("group_discoverer")

# Script de plan/grupos.md — extrae grupos sin hacer API calls
DISCOVERY_JS = """
(function() {
  var links = document.querySelectorAll('a[href*="/groups/"]');
  var results = [];
  var seen = {};
  var excluded = {'feed': 1, 'discover': 1, 'joins': 1, 'notifications': 1,
                  'pending': 1, 'joined': 1, 'category': 1, 'create': 1};
  for (var i = 0; i < links.length; i++) {
    var href = links[i].href;
    var match = href.match(/groups\\/([^/?#]+)/);
    if (!match) continue;
    var id = match[1];
    if (excluded[id] || seen[id]) continue;
    seen[id] = 1;
    var container = links[i].closest('div');
    var name = container ? container.innerText.split('\\n')[0].trim() : '(sin nombre)';
    results.push({id: id, name: name});
  }
  return results;
})();
"""


async def discover_groups_for_account(account: AccountConfig, config: dict) -> list[dict]:
    """
    Descubre grupos de una cuenta navegando a facebook.com/groups/joined,
    haciendo scroll y ejecutando el script de grupos.md.

    Retorna: [{id: "123456", name: "Grupo X"}, ...]
    Lanza excepción si falla (el caller guarda el error en DB).
    """
    async with FacebookPosterAsync(account, config) as poster:
        try:
            logger.info(f"[{account.name}] Navegando a groups feed...")
            await poster.page.goto("https://www.facebook.com/groups/joins/", timeout=30000)

            logger.info(f"[{account.name}] Haciendo scroll para cargar grupos...")
            for i in range(10):
                await poster.page.evaluate("window.scrollBy(0, window.innerHeight)")
                await poster.page.wait_for_timeout(1000)
                if i % 3 == 0:
                    logger.debug(f"[{account.name}] Scroll {i+1}/10...")

            logger.info(f"[{account.name}] Ejecutando script de extracción...")
            results = await poster.page.evaluate(DISCOVERY_JS)

            logger.info(f"[{account.name}] {len(results) if results else 0} grupos encontrados")
            return results if results else []

        except Exception as e:
            logger.error(f"[{account.name}] Error descubriendo grupos: {e}")
            raise
