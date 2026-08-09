"""¿Cuantas rondas etiqueto MAL el backtest entero?

El sondeo de `market/detail` dejo ver como resuelve el mercado de verdad:

    variantData.priceFeedProvider = CHAINLINK
    variantData.startPrice        = 64817.245
    variantData.endPrice          = 64813.495

Chainlink, no Binance. Y todo lo medido en este repo -- los 4 anios, el
54.25%, los controles -- usa el precio de APERTURA DE LA VELA DE BINANCE.

Son dos fuentes distintas. Chainlink toma el mid-price entre bid y ask del top
of book (por eso los 3 decimales); la vela de Binance registra el ultimo trade
del minuto. En una ronda que se define por centavos pueden dar resultados
OPUESTOS, y una ronda mal etiquetada es ruido inyectado directamente en el
entrenamiento y en la medicion.

Si la discrepancia es del 2%, se come la mitad de la ventaja del modelo. Nunca
se pudo verificar porque no habia forma de conseguir los precios oficiales.
Ahora `market/detail` los da para rondas pasadas, asi que se puede medir.

Como funciona: los ids son casi lineales en el tiempo (~6.7 ids por ronda de
5 min, contando los mercados de ETH, BNB y los de 15m intercalados), asi que
se estima el id de cada ronda y se corrige buscando alrededor, en vez de
recorrer miles de ids de a uno.

    source ~/.btc_env
    python3 chainlink_vs_klines.py --rondas 150

Solo GET. No coloca ordenes ni mueve fondos.
"""
import argparse
import hashlib
import hmac
import os
import sys
import time
import urllib.parse
from datetime import datetime, timezone

import requests

API_KEY = os.environ.get("BINANCE_API_KEY")
API_SECRET = os.environ.get("BINANCE_API_SECRET")

REST_BASE = "https://api.binance.com"
NS = "/sapi/v1/w3w/wallet/prediction"
KLINES = "https://data-api.binance.vision/api/v3/klines"
PREFIJO = "btc-updown-5m-"
ROUND = 300  # segundos


def get(path, params=None):
    p = dict(params or {})
    p["timestamp"] = str(int(time.time() * 1000))
    p["recvWindow"] = "5000"
    qs = urllib.parse.urlencode(sorted(p.items()))
    sig = hmac.new(API_SECRET.encode(), qs.encode(), hashlib.sha256).hexdigest()
    url = f"{REST_BASE}{path}?{qs}&signature={sig}"
    try:
        r = requests.get(url, headers={"X-MBX-APIKEY": API_KEY}, timeout=15)
    except Exception:
        return None, None
    try:
        return r.status_code, r.json()
    except ValueError:
        return r.status_code, None


def slug_ts(slug):
    if not isinstance(slug, str) or not slug.startswith(PREFIJO):
        return None
    try:
        return int(slug[len(PREFIJO):])
    except ValueError:
        return None


def detalle(mid):
    code, d = get(f"{NS}/market/detail", {"marketTopicId": mid})
    if code != 200 or not isinstance(d, dict):
        return None
    return d


def extraer(d):
    """(timestamp de ronda, startPrice, endPrice) si es un btc-updown-5m resuelto."""
    ts = slug_ts(d.get("slug"))
    if ts is None:
        return None
    vd = d.get("variantData") or {}
    sp, ep = vd.get("startPrice"), vd.get("endPrice")
    if sp is None or ep is None:
        return None
    try:
        return ts, float(sp), float(ep)
    except (TypeError, ValueError):
        return None


def klines_opens(desde_ms, hasta_ms):
    """Aperturas de velas de 1m de Binance, que es lo que uso el backtest."""
    opens = {}
    cursor = desde_ms
    while cursor <= hasta_ms:
        r = requests.get(KLINES, params={
            "symbol": "BTCUSDT", "interval": "1m",
            "startTime": cursor, "endTime": hasta_ms, "limit": 1000}, timeout=30)
        r.raise_for_status()
        ks = r.json()
        if not ks:
            break
        for k in ks:
            opens[int(k[0])] = float(k[1])
        cursor = int(ks[-1][0]) + 60_000
    return opens


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--rondas", type=int, default=150)
    args = ap.parse_args()

    if not API_KEY or not API_SECRET:
        raise SystemExit("Faltan BINANCE_API_KEY / BINANCE_API_SECRET (source ~/.btc_env)")

    code, body = get(f"{NS}/market/list", {"limit": 10})
    if code != 200 or not isinstance(body, dict):
        raise SystemExit(f"market/list fallo: {code}")
    vivos = [t for t in (body.get("marketTopics") or []) if slug_ts(t.get("slug")) is not None]
    if not vivos:
        raise SystemExit("No hay un btc-updown-5m vivo en market/list")
    id0 = vivos[0]["marketTopicId"]
    ts0 = slug_ts(vivos[0]["slug"])
    print(f"Ronda viva: id={id0}  {datetime.fromtimestamp(ts0, tz=timezone.utc):%d-%b %H:%M} UTC")
    print(f"Buscando hacia atras {args.rondas} rondas...\n")

    # Densidad de ids: se calibra con una ronda lejana en vez de asumirla.
    sonda = detalle(id0 - 672)
    dens = 6.72
    if sonda:
        e = extraer(sonda)
        if e and e[0] != ts0:
            dens = 672 / ((ts0 - e[0]) / ROUND)
    print(f"Densidad estimada: {dens:.2f} ids por ronda\n")

    encontradas = {}
    peticiones = 0
    for n in range(1, args.rondas + 1):
        objetivo = ts0 - n * ROUND
        if objetivo in encontradas:
            continue
        estimado = int(id0 - n * dens)
        # Buscar alrededor del estimado hasta dar con el slug exacto.
        for delta in (0, -1, 1, -2, 2, -3, 3, -4, 4, -5, 5, -6, 6, -7, 7):
            d = detalle(estimado + delta)
            peticiones += 1
            time.sleep(0.25)
            if not d:
                continue
            e = extraer(d)
            if not e:
                continue
            encontradas[e[0]] = (e[1], e[2])
            if e[0] == objetivo:
                # Recalibrar sobre la marcha para que el estimado no derive.
                dens = (id0 - (estimado + delta)) / n
                break
        if n % 25 == 0:
            sys.stdout.write(f"\r  {n}/{args.rondas} rondas, {len(encontradas)} halladas, "
                             f"{peticiones} peticiones")
            sys.stdout.flush()
    print()

    if len(encontradas) < 20:
        print(f"Solo se recuperaron {len(encontradas)} rondas. Muy pocas para concluir.")
        return

    ts_min, ts_max = min(encontradas), max(encontradas)
    print(f"\nRondas con precios oficiales: {len(encontradas)}")
    print(f"Periodo: {datetime.fromtimestamp(ts_min, tz=timezone.utc):%d-%b %H:%M}"
          f" a {datetime.fromtimestamp(ts_max, tz=timezone.utc):%d-%b %H:%M} UTC")

    print("\nDescargando las velas de Binance de ese periodo...")
    opens = klines_opens(ts_min * 1000, (ts_max + ROUND) * 1000)

    comparadas = iguales = distintas = empates_cl = 0
    ejemplos = []
    difs_start = []
    for ts, (sp, ep) in sorted(encontradas.items()):
        a = opens.get(ts * 1000)
        b = opens.get((ts + ROUND) * 1000)
        if a is None or b is None:
            continue
        comparadas += 1
        difs_start.append(abs(sp - a))
        if ep == sp:
            empates_cl += 1
            continue
        cl = "Up" if ep > sp else "Down"
        bi = "Up" if b > a else "Down"
        if cl == bi:
            iguales += 1
        else:
            distintas += 1
            if len(ejemplos) < 10:
                ejemplos.append((ts, sp, ep, a, b, cl, bi))

    print("\n" + "=" * 74)
    print("CHAINLINK (como resuelve el mercado) vs BINANCE (como mide el backtest)")
    print("=" * 74)
    print(f"  Rondas comparadas: {comparadas}")
    print(f"  Mismo resultado:   {iguales}  ({iguales/comparadas*100:.2f}%)")
    print(f"  DISTINTO:          {distintas}  ({distintas/comparadas*100:.2f}%)")
    if empates_cl:
        print(f"  Empate exacto en Chainlink (resuelve 50-50): {empates_cl}")

    if difs_start:
        difs_start.sort()
        med = difs_start[len(difs_start) // 2]
        p95 = difs_start[int(len(difs_start) * 0.95)]
        print(f"\n  Diferencia |Chainlink - Binance| en el precio de apertura:")
        print(f"    mediana ${med:.2f}   p95 ${p95:.2f}   maxima ${max(difs_start):.2f}")

    if ejemplos:
        print("\n  Rondas donde discrepan:")
        print(f"    {'hora UTC':<14}{'CL inicio':>11}{'CL fin':>11}{'BI inicio':>11}{'BI fin':>11}   CL/BI")
        for ts, sp, ep, a, b, cl, bi in ejemplos:
            d = datetime.fromtimestamp(ts, tz=timezone.utc)
            print(f"    {d:%d-%b %H:%M}  {sp:>11.3f}{ep:>11.3f}{a:>11.2f}{b:>11.2f}   {cl}/{bi}")

    print("\n" + "=" * 74)
    print("LECTURA")
    print("=" * 74)
    tasa = distintas / comparadas if comparadas else 0
    print(f"  Tasa de etiquetado incorrecto en el backtest: {tasa*100:.2f}%")
    print()
    print("  Que significa: el modelo se entrena y se mide contra la etiqueta de")
    print("  Binance, pero cobra segun la de Chainlink. Cada ronda discrepante es")
    print("  una que el backtest cuenta como acierto y el mercado paga como fallo,")
    print("  o al reves.")
    print()
    ventaja = 0.5425 - 0.5  # el edge medido sobre el breakeven de una moneda
    print(f"  El acierto medido es 54.25%, o sea {ventaja*100:.2f} pp sobre el azar.")
    if tasa > ventaja / 2:
        print(f"  Con {tasa*100:.2f}% de etiquetas malas, una parte grande de esa ventaja")
        print("  puede ser artefacto. Hay que rehacer el backtest con precios de")
        print("  Chainlink antes de creerle al numero.")
    elif tasa > 0.005:
        print(f"  Con {tasa*100:.2f}% de etiquetas malas el efecto existe pero es chico")
        print("  frente a la ventaja medida. Conviene tenerlo en cuenta, no invalida.")
    else:
        print(f"  Con {tasa*100:.2f}% las dos fuentes coinciden casi siempre: usar velas")
        print("  de Binance no estaba introduciendo un sesgo relevante.")


if __name__ == "__main__":
    main()
