"""Segunda vuelta: `market/detail` existe. ¿Sirve para rondas pasadas?

El primer sondeo dejo una sola puerta abierta. De 21 nombres, 20 dieron 404 y
uno dio 400 con este mensaje:

    Required request parameter 'marketTopicId' ... is not present

Existe. Solo faltaba el parametro.

Y `market/list` mostro dos cosas que lo hacen util:

    "marketTopicId": 4465182
    "slug": "btc-updown-5m-1786250700"

El slug lleva el timestamp de inicio de ronda, y el id es un entero. Si los
ids corren de a uno por ronda, los de las rondas pasadas se calculan restando
-- y si `market/detail` contesta para esos, hay historial y no hacen falta los
dias de recoleccion.

Este script:

  1. saca el mercado vivo y su id
  2. pide `market/detail` de ESE id y muestra la respuesta entera, para ver
     que campos de precio trae
  3. pide ids hacia atras (1, 2, 12, 288 = un dia, 2016 = una semana) y ve
     hasta donde llega
  4. prueba pedir por slug construido desde un timestamp, que es lo que
     permitiria pedir una ronda puntual
  5. prueba parametros de paginacion en `market/list`, porque los de estado
     (CLOSED/RESOLVED) se ignoran -- devolvieron el mismo mercado vivo

Solo GET, solo lectura. No coloca ordenes ni mueve fondos.

    source ~/.btc_env
    python3 probe_market_detail.py
"""
import hashlib
import hmac
import json
import os
import time
import urllib.parse
from datetime import datetime, timezone

import requests

# El chequeo de llaves va en main(), no aca: si aborta al importar, el modulo
# no se puede cargar para probar sus funciones puras sin credenciales.
API_KEY = os.environ.get("BINANCE_API_KEY")
API_SECRET = os.environ.get("BINANCE_API_SECRET")

REST_BASE = "https://api.binance.com"
NS = "/sapi/v1/w3w/wallet/prediction"


def get(path, params=None):
    p = dict(params or {})
    p["timestamp"] = str(int(time.time() * 1000))
    p["recvWindow"] = "5000"
    qs = urllib.parse.urlencode(sorted(p.items()))
    sig = hmac.new(API_SECRET.encode(), qs.encode(), hashlib.sha256).hexdigest()
    url = f"{REST_BASE}{path}?{qs}&signature={sig}"
    try:
        r = requests.get(url, headers={"X-MBX-APIKEY": API_KEY}, timeout=15)
    except Exception as exc:
        return None, str(exc)
    try:
        return r.status_code, r.json()
    except ValueError:
        return r.status_code, r.text[:300]


def buscar_precios(obj, camino=""):
    """Cualquier campo que parezca un precio o una probabilidad, a cualquier
    profundidad. No se cual es el nombre, asi que se buscan todos."""
    hallados = []
    interesantes = ("price", "chance", "prob", "odds", "bid", "ask", "last",
                    "close", "settle", "outcome", "result", "status")
    if isinstance(obj, dict):
        for k, v in obj.items():
            nuevo = f"{camino}.{k}" if camino else k
            if isinstance(v, (dict, list)):
                hallados += buscar_precios(v, nuevo)
            elif any(t in k.lower() for t in interesantes):
                hallados.append((nuevo, v))
    elif isinstance(obj, list):
        for i, v in enumerate(obj[:3]):
            hallados += buscar_precios(v, f"{camino}[{i}]")
    return hallados


def main():
    if not API_KEY or not API_SECRET:
        raise SystemExit("Faltan BINANCE_API_KEY / BINANCE_API_SECRET (source ~/.btc_env)")

    print("=" * 72)
    print("1. MERCADO VIVO")
    print("=" * 72)
    code, body = get(f"{NS}/market/list", {"limit": 5})
    if code != 200 or not isinstance(body, dict):
        print(f"  market/list fallo ({code}): {body}")
        return
    topics = body.get("marketTopics") or []
    if not topics:
        print("  Sin marketTopics en la respuesta.")
        return

    t0 = topics[0]
    mid = t0.get("marketTopicId")
    slug = t0.get("slug", "")
    print(f"  marketTopicId: {mid}")
    print(f"  slug:          {slug}")
    print(f"  question:      {str(t0.get('question'))[:70]}")
    print(f"  mercados devueltos: {len(topics)}")
    for t in topics[:5]:
        print(f"    id={t.get('marketTopicId')}  slug={t.get('slug')}")

    print("\n" + "=" * 72)
    print("2. DETALLE DEL MERCADO VIVO -- ¿que campos de precio trae?")
    print("=" * 72)
    code, det = get(f"{NS}/market/detail", {"marketTopicId": mid})
    print(f"  HTTP {code}")
    if code == 200:
        precios = buscar_precios(det)
        if precios:
            print("  Campos que parecen precio/estado:")
            for k, v in precios[:30]:
                print(f"    {k:<46} = {v}")
        print("\n  Respuesta completa (primeros 1500 caracteres):")
        print("  " + json.dumps(det)[:1500])
    else:
        print(f"  {str(det)[:300]}")

    print("\n" + "=" * 72)
    print("3. ¿CONTESTA PARA RONDAS PASADAS? (ids hacia atras)")
    print("=" * 72)
    print("  Si los ids corren de a uno por ronda: -12 = 1 hora, -288 = 1 dia")
    encontrados = 0
    for atras in (1, 2, 12, 288, 2016):
        if not isinstance(mid, int):
            break
        code, d = get(f"{NS}/market/detail", {"marketTopicId": mid - atras})
        if code == 200 and isinstance(d, dict) and d:
            encontrados += 1
            s = None
            for clave in ("slug", "marketTopic", "data"):
                v = d.get(clave)
                if isinstance(v, str):
                    s = v
                    break
                if isinstance(v, dict) and v.get("slug"):
                    s = v["slug"]
                    break
            cuando = ""
            if s and "-" in s:
                try:
                    ts = int(s.rsplit("-", 1)[1])
                    cuando = f"  ({datetime.fromtimestamp(ts, tz=timezone.utc):%d-%b %H:%M} UTC)"
                except (ValueError, IndexError):
                    pass
            precios = buscar_precios(d)
            print(f"  -{atras:<5} id={mid-atras}  HTTP 200  slug={s}{cuando}")
            if precios:
                for k, v in precios[:8]:
                    print(f"           {k:<40} = {v}")
        else:
            print(f"  -{atras:<5} id={mid-atras}  HTTP {code}  {str(d)[:90]}")
        time.sleep(0.4)

    print("\n" + "=" * 72)
    print("4. PEDIR POR SLUG (una ronda puntual por su hora)")
    print("=" * 72)
    ahora = int(time.time())
    hace_una_hora = (ahora // 300 - 12) * 300
    slug_viejo = f"btc-updown-5m-{hace_una_hora}"
    print(f"  probando slug de hace 1 hora: {slug_viejo}")
    for nombre in ("slug", "marketSlug", "topicSlug"):
        code, d = get(f"{NS}/market/detail", {nombre: slug_viejo})
        print(f"    {nombre:<12} HTTP {code}  {str(d)[:110]}")
        time.sleep(0.4)

    print("\n" + "=" * 72)
    print("5. PAGINACION EN market/list (el filtro de estado se ignora)")
    print("=" * 72)
    for params in ({"limit": 5, "page": 2}, {"limit": 5, "offset": 20},
                   {"limit": 5, "cursor": "2"},
                   {"limit": 5, "startTime": (ahora - 86400) * 1000},
                   {"limit": 50}):
        code, d = get(f"{NS}/market/list", params)
        ids = []
        if code == 200 and isinstance(d, dict):
            ids = [t.get("marketTopicId") for t in (d.get("marketTopics") or [])][:4]
        extra = "".join(f" {k}={v}" for k, v in params.items())
        print(f"  {extra:<34} HTTP {code}  primeros ids: {ids}")
        time.sleep(0.4)

    print("\n" + "=" * 72)
    print("LECTURA")
    print("=" * 72)
    if encontrados:
        print(f"  {encontrados} de 5 ids pasados contestaron. Si alguno trae el")
        print("  precio al que cotizaba esa ronda, el historial existe y la")
        print("  espera de dias se cae.")
    else:
        print("  Ningun id pasado contesto: market/detail parece servir solo")
        print("  el mercado vivo. Ahi si queda confirmado que el precio hay")
        print("  que juntarlo en vivo.")
    print("\n  Pegame todo. Nada de esto coloco ordenes: son GET.")


if __name__ == "__main__":
    main()
