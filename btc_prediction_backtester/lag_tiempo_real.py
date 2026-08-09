"""Mide el retraso del mercado en tiempo real, con resolucion de decimas.

Por que hacia falta esto. El intento anterior uso `live_odds_log.csv` y no
podia funcionar, por dos razones:

  1. RESOLUCION. El logger pollea cada 5 segundos, y el lag que se busca es de
     3 a 7. Por eso 0s y 5s dieron el MISMO error a dos decimales (8.77pp) --
     no es que gane 0s, es que la regla no tiene marcas mas finas.
  2. RELOJES DISTINTOS. El precio de BTC se pide en una peticion HTTP separada
     DESPUES de recibir el libro, asi que cada fila compara el libro del
     instante T contra el BTC del instante T+delta, con delta variable y
     desconocido. Eso desdibuja justo lo que se quiere medir.

Aca los dos se muestrean a proposito:

  - BTC spot varias veces por segundo, guardando el instante exacto de cada
    lectura.
  - El libro del mercado de prediccion tan seguido como deje la API.

Y despues se busca el desfase preguntando: el precio que el mercado cotiza en
el instante T, ¿a que momento del precio de BTC se parece mas? Si la respuesta
es T, cotiza el presente. Si es T menos 4 segundos, va cuatro segundos atras y
esa es la ventana.

El metodo es un estudio de eventos, que es lo que resiste el ruido: se buscan
los SALTOS de BTC -- los momentos donde de verdad hay algo nuevo que
incorporar -- y se mide cuanto tarda la cotizacion en reaccionar. En los
tramos planos no hay informacion sobre latencia, solo ruido.

    source ~/.btc_env
    python3 lag_tiempo_real.py --minutos 30

Solo lectura. No coloca ordenes.
"""
import argparse
import json
import os
import statistics
import sys
import threading
import time
from collections import deque

import requests

# La busqueda del mercado y la lectura del libro se toman del logger en vez de
# reimplementarse: sus parametros salieron de descubrir a mano cada error
# -3026 "falta el parametro X", y rehacerlos aca solo repite ese trabajo mal.
os.environ.setdefault("SIGNAL_ALERTS", "0")
import local_odds_logger as L

SPOT = "https://data-api.binance.vision/api/v3/ticker/bookTicker"

spot_serie = deque(maxlen=200_000)     # (t_local, precio_medio_spot)
libro_serie = []                        # (t_local, mid, best_bid, best_ask)
_parar = threading.Event()


def muestrear_spot(intervalo):
    """BTC spot lo mas seguido posible. Se usa el punto medio de bid/ask, que
    es lo que mueve la probabilidad -- el ultimo trade salta entre los dos
    lados del spread y agrega ruido que no es informacion."""
    s = requests.Session()
    while not _parar.is_set():
        t0 = time.time()
        try:
            r = s.get(SPOT, params={"symbol": "BTCUSDT"}, timeout=5)
            if r.status_code == 200:
                d = r.json()
                mid = (float(d["bidPrice"]) + float(d["askPrice"])) / 2
                # El instante se toma a mitad del viaje de ida y vuelta, que es
                # la mejor estimacion de cuando el servidor respondio.
                spot_serie.append(((t0 + time.time()) / 2, mid))
        except Exception:
            pass
        time.sleep(max(0, intervalo - (time.time() - t0)))


def muestrear_libro(ctx, intervalo):
    """Usa get_order_book() del logger: es el mismo camino que ya funciona en
    produccion, con los parametros que costo descubrir uno por uno."""
    market_id, vendor, token_id, condition_id = ctx
    errores = 0
    while not _parar.is_set():
        t0 = time.time()
        try:
            r = L.get_order_book(market_id, vendor, token_id, condition_id)
            if r.status_code == 200:
                b = r.json()
                bids, asks = b.get("bids") or [], b.get("asks") or []
                if bids and asks:
                    bb, ba = float(bids[0]["price"]), float(asks[0]["price"])
                    libro_serie.append(((t0 + time.time()) / 2, (bb + ba) / 2, bb, ba))
            else:
                errores += 1
                if errores <= 3:
                    print(f"\n  [libro HTTP {r.status_code}] {r.text[:150]}")
        except Exception as exc:
            errores += 1
            if errores <= 3:
                print(f"\n  [libro error] {exc}")
        time.sleep(max(0, intervalo - (time.time() - t0)))


def spot_en(t):
    """Precio spot en el instante t, interpolando entre las dos lecturas que
    lo rodean. Sin interpolar, la resolucion queda limitada al muestreo."""
    if not spot_serie:
        return None
    arr = list(spot_serie)
    lo, hi = 0, len(arr) - 1
    if t <= arr[0][0] or t >= arr[-1][0]:
        return None
    while lo < hi - 1:
        mid = (lo + hi) // 2
        if arr[mid][0] < t:
            lo = mid
        else:
            hi = mid
    (t1, p1), (t2, p2) = arr[lo], arr[hi]
    if t2 == t1:
        return p1
    return p1 + (p2 - p1) * (t - t1) / (t2 - t1)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--minutos", type=int, default=30)
    ap.add_argument("--spot-hz", type=float, default=4.0, help="lecturas de BTC por segundo")
    ap.add_argument("--libro-hz", type=float, default=1.0, help="lecturas del libro por segundo")
    args = ap.parse_args()

    # local_odds_logger ya aborta al importarse si faltan las llaves, asi que
    # llegar hasta aca implica que estan.

    print("Buscando el mercado BTC 5m (con el buscador del logger)...")
    market_id, ronda, topic = L.find_btc_5m_market()
    if market_id is None:
        print("  No se encontro el mercado. El logger ya imprimio el detalle arriba.")
        return
    vendor, condition_id, token_id = L._extract_poll_context(ronda, topic)
    ctx = (market_id, vendor, token_id, condition_id)
    print(f"  marketId={market_id}  vendor={vendor}")

    # Una lectura de prueba antes de arrancar los 30 minutos: si el libro no
    # responde, es mejor enterarse ahora que despues de media hora muestreando.
    prueba = L.get_order_book(market_id, vendor, token_id, condition_id)
    if prueba.status_code != 200:
        print(f"\n  El libro respondio HTTP {prueba.status_code}:")
        print(f"  {prueba.text[:300]}")
        return
    print("  Libro OK.")

    print(f"\nMuestreando {args.minutos} min:  BTC {args.spot_hz}/s   "
          f"libro {args.libro_hz}/s")
    print("Ctrl+C para cortar antes.\n")

    hilos = [
        threading.Thread(target=muestrear_spot, args=(1 / args.spot_hz,), daemon=True),
        threading.Thread(target=muestrear_libro,
                         args=(ctx, 1 / args.libro_hz), daemon=True),
    ]
    for h in hilos:
        h.start()

    fin = time.time() + args.minutos * 60
    try:
        while time.time() < fin:
            time.sleep(5)
            sys.stdout.write(f"\r  spot: {len(spot_serie):,}   libro: {len(libro_serie):,}   "
                             f"faltan {int(fin - time.time())}s   ")
            sys.stdout.flush()
    except KeyboardInterrupt:
        pass
    _parar.set()
    time.sleep(1.5)
    print("\n")

    if len(libro_serie) < 60 or len(spot_serie) < 200:
        print(f"Muestra insuficiente (libro {len(libro_serie)}, spot {len(spot_serie)}).")
        return

    print("=" * 70)
    print("SALTOS DE BTC Y REACCION DEL MERCADO")
    print("=" * 70)
    # Un salto es un movimiento de BTC grande respecto a su propio ruido en la
    # ventana. Solo ahi hay informacion nueva que el mercado deba incorporar.
    arr = list(spot_serie)
    precios = [p for _, p in arr]
    difs = [abs(precios[i] - precios[i - 1]) for i in range(1, len(precios))]
    umbral = statistics.quantiles(difs, n=100)[97] if len(difs) > 200 else max(difs)
    print(f"  Umbral de salto: ${umbral:.2f} entre lecturas consecutivas")

    correl = {}
    for lag_ms in range(0, 12_001, 250):
        lag = lag_ms / 1000
        errores = []
        for i in range(1, len(libro_serie)):
            t, mid, _, _ = libro_serie[i]
            t_ant, mid_ant, _, _ = libro_serie[i - 1]
            s_ahora = spot_en(t - lag)
            s_antes = spot_en(t_ant - lag)
            if s_ahora is None or s_antes is None:
                continue
            d_spot = s_ahora - s_antes
            d_mid = mid - mid_ant
            if abs(d_spot) < umbral:
                continue          # tramo plano: no dice nada de latencia
            # Si el mercado sigue al spot, los dos se mueven en el mismo
            # sentido; se mide cuanto de la variacion del libro explica la
            # del spot desplazada `lag`.
            errores.append((d_spot, d_mid))
        if len(errores) < 20:
            continue
        mx = statistics.fmean(a for a, _ in errores)
        my = statistics.fmean(b for _, b in errores)
        num = sum((a - mx) * (b - my) for a, b in errores)
        da = sum((a - mx) ** 2 for a, _ in errores) ** 0.5
        db = sum((b - my) ** 2 for _, b in errores) ** 0.5
        if da and db:
            correl[lag] = (num / (da * db), len(errores))

    if not correl:
        print("\n  No hubo suficientes saltos de BTC en este rato.")
        print("  Corre de nuevo en un momento mas movido, o con --minutos mayor.")
        return

    print(f"\n  {'desfase':<12}{'correlacion spot->libro':>26}{'saltos':>10}")
    print("  " + "-" * 48)
    for lag in sorted(correl):
        c, n = correl[lag]
        barra = "#" * int(max(0, c) * 40)
        print(f"  {lag:>5.2f} s{'':<5}{c:>+10.3f}  {barra:<20}{n:>8}")

    mejor = max(correl.items(), key=lambda kv: kv[1][0])
    print("\n" + "=" * 70)
    print("LECTURA")
    print("=" * 70)
    print(f"  Correlacion maxima con un desfase de {mejor[0]:.2f} s "
          f"(r={mejor[1][0]:+.3f}, {mejor[1][1]} saltos)")
    print()
    if mejor[0] <= 0.5:
        print("  El mercado reacciona practicamente al instante. No hay ventana")
        print("  de latencia que explotar desde aca.")
    else:
        print(f"  El mercado tarda ~{mejor[0]:.1f} s en incorporar un movimiento de BTC.")
        print("  Esa es la ventana. Para usarla hay que ver el salto y tener la")
        print("  orden puesta antes de que se cierre -- y ahi mandan la latencia")
        print("  de red y el tamaño disponible en el libro, no la estadistica.")
    print()
    print(f"  Muestreo real conseguido: spot {len(spot_serie)/(args.minutos*60):.1f}/s, "
          f"libro {len(libro_serie)/(args.minutos*60):.1f}/s")
    print("  Un desfase menor al intervalo de muestreo del libro no se puede")
    print("  distinguir: subi --libro-hz si la API lo permite.")


if __name__ == "__main__":
    main()
