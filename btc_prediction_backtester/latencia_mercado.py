"""¿El mercado de prediccion se retrasa respecto al precio de BTC?

Ahi esta la ventaja que usan los que ganan, y no es prediccion. Los precios de
Kalshi se reajustan con 3-7 segundos de retraso respecto al spot, y los
mercados de 5 minutos de Polymarket llegan a retrasarse 30-90 segundos. El bot
no adivina: ve que BTC ya se movio, sabe que la probabilidad real ya es 85%, y
el mercado sigue cotizando 50/50.

Este proyecto entero fue en la direccion equivocada. Predecir el futuro desde
velas es justo lo que el mercado tiene bien cotizado. Reaccionar al presente
mas rapido que el mercado es otra cosa, y es medible con lo ya recolectado:
`live_odds_log.csv` tiene una foto del libro cada 5 segundos junto con el
precio de BTC del mismo instante.

Que hace:

  1. Para cada foto calcula la probabilidad REAL de cierre en Up, usando el
     movimiento hasta ese segundo y la tabla empirica de 4 anios.
  2. La compara con lo que el mercado cotizaba en ese mismo momento.
  3. Busca el retraso: si el mercado va atras, su precio de ahora deberia
     parecerse mas a la probabilidad real de hace N segundos que a la de ahora.
  4. Calcula el EV de comprar el lado favorecido cuando la brecha es grande.

Necesita tabla_prob.json (de exportar_tabla_prob.py).

    python3 latencia_mercado.py

Solo lectura sobre los CSV que ya tenes. No apuesta nada.
"""
import bisect
import csv
import json
import math
import os
import statistics
from collections import defaultdict
from datetime import datetime, timezone

import requests

HERE = os.path.dirname(os.path.abspath(__file__))
ODDS = os.path.join(HERE, "data", "live_odds_log.csv")
TABLA = os.path.join(HERE, "tabla_prob.json")
KLINES = "https://data-api.binance.vision/api/v3/klines"
ROUND_MS = 5 * 60 * 1000
FEE = 0.02

ODDS_SCHEMA = [
    "polled_at_ms", "market_id", "http_status", "api_timestamp",
    "best_bid", "best_ask", "mid_price", "btc_price",
    "round_start_price", "price_gap_usd", "raw_body",
]


def cargar_odds():
    if not os.path.exists(ODDS):
        raise SystemExit(f"No encuentro {ODDS}")
    with open(ODDS, newline="") as f:
        filas = list(csv.reader(f))
    cuerpo = filas[1:] if filas and filas[0] and filas[0][0] == ODDS_SCHEMA[0] else filas
    out = []
    for r in cuerpo:
        if not r:
            continue
        d = dict.fromkeys(ODDS_SCHEMA, "")
        for i, v in enumerate(r):
            if i < len(ODDS_SCHEMA):
                d[ODDS_SCHEMA[i]] = v
        out.append(d)
    return out


def num(s):
    try:
        return float(str(s).strip())
    except (TypeError, ValueError):
        return None


def sigma_por_ronda(inicio_ms, fin_ms):
    """sigma de 1 minuto para cada ronda, de la hora previa a su apertura."""
    opens = {}
    cursor = inicio_ms - 70 * 60_000
    while cursor <= fin_ms:
        r = requests.get(KLINES, params={
            "symbol": "BTCUSDT", "interval": "1m",
            "startTime": cursor, "endTime": fin_ms, "limit": 1000}, timeout=30)
        r.raise_for_status()
        ks = r.json()
        if not ks:
            break
        for k in ks:
            opens[int(k[0])] = float(k[1])
        cursor = int(ks[-1][0]) + 60_000

    sig = {}
    for s in range(inicio_ms - inicio_ms % ROUND_MS, fin_ms + ROUND_MS, ROUND_MS):
        prev = [opens.get(s - k * 60_000) for k in range(1, 61)]
        prev = [p for p in prev if p]
        if len(prev) < 30:
            continue
        rets = [(prev[i] - prev[i - 1]) / prev[i - 1] for i in range(1, len(prev)) if prev[i - 1]]
        if len(rets) < 2:
            continue
        base = opens.get(s) or prev[-1]
        sd = statistics.pstdev(rets) * base
        if sd > 0:
            sig[s] = sd
    return sig


def main():
    if not os.path.exists(TABLA):
        raise SystemExit(f"Falta {TABLA}. Bajalo del repo o corre exportar_tabla_prob.py")
    with open(TABLA) as f:
        tp = json.load(f)
    bordes, celdas = tp["bordes_z"], tp["tabla"]

    def prob_real(minuto, z):
        i = bisect.bisect_left(bordes, z)
        c = celdas.get(f"{minuto}|{i}")
        return c[0] if c else None

    filas = cargar_odds()
    print(f"Fotos del libro en el log: {len(filas):,}")

    puntos = []
    for r in filas:
        t = num(r["polled_at_ms"])
        mid = num(r["mid_price"])
        btc = num(r["btc_price"])
        p0 = num(r["round_start_price"])
        if None in (t, mid, btc, p0) or not 0 < mid < 1 or p0 <= 0:
            continue
        puntos.append((int(t), mid, btc, p0))
    if len(puntos) < 200:
        raise SystemExit(f"Solo {len(puntos)} fotos utilizables. Segui recolectando.")
    puntos.sort()
    print(f"Utilizables (con precio de mercado y de BTC): {len(puntos):,}")
    d0 = datetime.fromtimestamp(puntos[0][0] / 1000, tz=timezone.utc)
    d1 = datetime.fromtimestamp(puntos[-1][0] / 1000, tz=timezone.utc)
    print(f"Periodo: {d0:%d-%b %H:%M} a {d1:%d-%b %H:%M} UTC\n")

    print("Bajando velas para estimar la volatilidad de cada ronda...")
    sig = sigma_por_ronda(puntos[0][0], puntos[-1][0])
    print(f"Rondas con sigma: {len(sig)}\n")

    # Serie por ronda: (segundos dentro, prob real, precio de mercado)
    series = defaultdict(list)
    for t, mid, btc, p0 in puntos:
        s = t - t % ROUND_MS
        sd = sig.get(s)
        if not sd:
            continue
        seg = (t - s) / 1000
        minuto = min(4, max(1, int(seg // 60) + 1))
        restan = 5 - minuto
        if restan <= 0:
            continue
        z = (btc - p0) / (sd * math.sqrt(restan))
        pr = prob_real(minuto, z)
        if pr is None:
            continue
        series[s].append((seg, pr, mid))

    total = sum(len(v) for v in series.values())
    print(f"Fotos con probabilidad real calculable: {total:,}  "
          f"en {len(series)} rondas\n")
    if total < 100:
        raise SystemExit("Muy pocas para concluir. Segui recolectando.")

    print("=" * 74)
    print("1. ¿CUANTO SE SEPARA EL MERCADO DE LA PROBABILIDAD REAL?")
    print("=" * 74)
    por_min = defaultdict(list)
    for s, serie in series.items():
        for seg, pr, mid in serie:
            por_min[min(4, int(seg // 60) + 1)].append(pr - mid)
    print(f"  {'minuto':<10}{'brecha media':>16}{'|brecha| media':>18}{'fotos':>10}")
    print("  " + "-" * 54)
    for m in sorted(por_min):
        v = por_min[m]
        print(f"  {m:<10}{statistics.fmean(v)*100:>+15.1f}pp"
              f"{statistics.fmean(abs(x) for x in v)*100:>17.1f}pp{len(v):>10,}")
    print()
    print("  brecha = probabilidad real - precio del mercado.")
    print("  Positiva quiere decir que el mercado cotiza Up MAS BARATO de lo")
    print("  que vale, o sea que todavia no incorporo el movimiento.")

    print("\n" + "=" * 74)
    print("2. ¿VA EL MERCADO CON RETRASO?")
    print("=" * 74)
    print("  Si va atras, su precio de ahora deberia parecerse mas a la")
    print("  probabilidad real de hace N segundos que a la de ahora.\n")
    print(f"  {'retraso':<12}{'error medio |mercado - real|':>32}")
    print("  " + "-" * 44)
    mejor = None
    for lag in (0, 5, 10, 15, 30, 45, 60, 90):
        errores = []
        for s, serie in series.items():
            serie = sorted(serie)
            segs = [x[0] for x in serie]
            for seg, pr, mid in serie:
                j = bisect.bisect_left(segs, seg - lag)
                if j >= len(serie):
                    continue
                errores.append(abs(mid - serie[j][1]))
        if len(errores) < 50:
            continue
        e = statistics.fmean(errores)
        marca = ""
        if mejor is None or e < mejor[1]:
            mejor = (lag, e)
            marca = ""
        print(f"  {lag:>3} s{'':<7}{e*100:>28.2f}pp{marca}")
    if mejor:
        print(f"\n  El error mas chico se da con un retraso de {mejor[0]} segundos.")
        if mejor[0] == 0:
            print("  El mercado NO va atras: cotiza el presente. No hay ventaja")
            print("  de latencia que explotar con datos cada 5 segundos.")
        else:
            print(f"  El mercado parece ir ~{mejor[0]}s atras del precio real de BTC.")
            print("  Esa es exactamente la ventana que explotan los bots.")

    print("\n" + "=" * 74)
    print("3. ¿SE PODRIA GANAR COMPRANDO EL LADO FAVORECIDO?")
    print("=" * 74)
    print("  Cuando la probabilidad real supera al precio del mercado por mucho,")
    print("  comprar ese lado tiene EV positivo. Aca esta medido de verdad.\n")
    print(f"  {'brecha minima':<16}{'oportunidades':>15}{'EV por apuesta':>18}")
    print("  " + "-" * 49)
    for umbral in (0.03, 0.05, 0.10, 0.15):
        evs = []
        for s, serie in series.items():
            for seg, pr, mid in serie:
                # Comprar Up cuando el mercado lo subvalua.
                if pr - mid >= umbral and 0.02 < mid < 0.98:
                    evs.append(pr * (1 - FEE) / mid - 1)
                # Comprar Down cuando el mercado subvalua a Down.
                pdown, mdown = 1 - pr, 1 - mid
                if pdown - mdown >= umbral and 0.02 < mdown < 0.98:
                    evs.append(pdown * (1 - FEE) / mdown - 1)
        if len(evs) < 20:
            print(f"  {umbral*100:>5.0f} pp{'':<10}{len(evs):>15}{'sin muestra':>18}")
            continue
        print(f"  {umbral*100:>5.0f} pp{'':<10}{len(evs):>15}{statistics.fmean(evs)*100:>+17.1f}%")

    print("\n" + "=" * 74)
    print("AVISO")
    print("=" * 74)
    print("  Esto mide el EV TEORICO usando el precio medio del libro. No")
    print("  incluye si habia tamaño suficiente a ese precio, ni cuanto tardaria")
    print("  una orden en llegar. Con ventanas de segundos, esas dos cosas")
    print("  deciden si la ventaja existe de verdad o solo en la planilla.")
    print()
    print("  Y una nota de escala: los bots que hacen esto corren en servidores")
    print("  a milisegundos del exchange. Un telefono pollea cada 5 segundos.")


if __name__ == "__main__":
    main()
