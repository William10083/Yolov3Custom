"""Predice la proxima vela de 5 minutos: Up o Down.

Usa el modelo congelado en model.json -- el mismo que dio 54.5% de acierto
sobre 765 rondas que nunca vio, y que aguanto los cuatro controles de
robustness_check.py. Aca no se entrena nada: se cargan los pesos y se aplican.

Lo importante de como funciona:

  - Solo se pronuncia cuando la confianza llega al umbral. En el ~93% de las
    rondas el modelo no tiene nada que decir, y decirlo es la respuesta
    correcta. Forzar una opinion en cada ronda es exactamente lo que baja el
    acierto de 54.5% a 50.7%.
  - Las features salen de velas cerradas ANTES del inicio de la ronda, asi que
    la prediccion queda firme recien en el borde de los :00/:05/:10. Antes de
    eso es preliminar y lo dice.
  - 54.5% no es dinero. A un precio de 0.50 deja margen; a 0.55 no deja nada.
    Sin comparar contra el precio cotizado esto es una prediccion, no una
    apuesta -- para eso esta market_vs_model.py.

    python3 predict_next.py           # una prediccion
    python3 predict_next.py --wait    # espera al borde y da la definitiva
    python3 predict_next.py --loop    # se queda prediciendo cada ronda
"""
import argparse
import json
import math
import os
import time
from datetime import datetime, timezone

import requests

import predict_model as pm

HERE = os.path.dirname(os.path.abspath(__file__))
MODEL = os.path.join(HERE, "model.json")
KLINES = "https://data-api.binance.vision/api/v3/klines"
TICKER = "https://data-api.binance.vision/api/v3/ticker/price"
SYMBOL = "BTCUSDT"
ROUND_MS = 5 * 60 * 1000


def fetch_candles(limit=320):
    r = requests.get(
        KLINES, params={"symbol": SYMBOL, "interval": "1m", "limit": limit}, timeout=20
    )
    r.raise_for_status()
    return [
        {
            "open_time_ms": int(k[0]),
            "open": float(k[1]),
            "high": float(k[2]),
            "low": float(k[3]),
            "close": float(k[4]),
            "volume": float(k[5]),
            "taker_buy_base": float(k[9]),
        }
        for k in r.json()
    ]


def fetch_price():
    r = requests.get(TICKER, params={"symbol": SYMBOL}, timeout=10)
    r.raise_for_status()
    return float(r.json()["price"])


def load_model():
    if not os.path.exists(MODEL):
        raise SystemExit(f"Falta {MODEL}. Corre primero:  python3 export_model.py")
    with open(MODEL) as f:
        return json.load(f)


def apply_model(m, feats):
    v = []
    for k in m["keys"]:
        st = m["standardize"][k]
        v.append((feats[k] - st["mean"]) / st["std"])
    z = m["bias"] + sum(wi * vi for wi, vi in zip(m["weights"], v))
    return 1 / (1 + math.exp(-max(-30, min(30, z))))


def contributions(m, feats):
    """Cuanto empuja cada feature esta prediccion en particular.

    El peso solo dice cuanto pesa la feature en general; lo que mueve ESTA
    ronda es peso x valor estandarizado. Eso es lo que se puede explicar.
    """
    out = []
    for k, w in zip(m["keys"], m["weights"]):
        st = m["standardize"][k]
        z = (feats[k] - st["mean"]) / st["std"]
        out.append((k, w * z, feats[k]))
    out.sort(key=lambda t: -abs(t[1]))
    return out


NOMBRES = {
    "mv_5": "movimiento ultimos 5 min",
    "mv_15": "movimiento ultimos 15 min",
    "mv_60": "movimiento ultima hora",
    "vol_15": "volatilidad 15 min",
    "vol_rank": "volatilidad vs su propia historia",
    "eff_15": "que tan limpia es la tendencia (15m)",
    "eff_60": "que tan limpia es la tendencia (60m)",
    "flow_5": "flujo comprador 5 min",
    "flow_15": "flujo comprador 15 min",
    "flow_60": "flujo comprador 1 hora",
    "pos_60": "posicion en el rango de 1h",
    "pos_240": "posicion en el rango de 4h",
    "mv15_x_eff15": "movimiento 15m x limpieza de tendencia",
    "flow15_x_volrank": "flujo 15m x volatilidad relativa",
    "mv5_x_volrank": "movimiento 5m x volatilidad relativa",
}


def next_boundary_ms(now_ms=None):
    now_ms = now_ms if now_ms is not None else int(time.time() * 1000)
    return ((now_ms // ROUND_MS) + 1) * ROUND_MS


def predict_round(m, candles, start_ms, price_now):
    by_time = {c["open_time_ms"]: c for c in candles}
    feats = pm.features_at(by_time, start_ms, price_now=price_now)
    if feats is None:
        return None, None
    return apply_model(m, feats), feats


def report(m, target_ms, p_up, feats, price_now, firme):
    margin = m.get("confidence_margin", 0.05)
    ts = datetime.fromtimestamp(target_ms / 1000, tz=timezone.utc)
    fin = datetime.fromtimestamp((target_ms + ROUND_MS) / 1000, tz=timezone.utc)

    print("=" * 62)
    print(f"RONDA {ts:%H:%M} → {fin:%H:%M} UTC     BTC ${price_now:,.2f}")
    print("=" * 62)

    conf = max(p_up, 1 - p_up)
    lado = "Up" if p_up >= 0.5 else "Down"

    if not firme:
        faltan = (target_ms - int(time.time() * 1000)) / 1000
        print(f"  PRELIMINAR — faltan {int(faltan)//60}:{int(faltan)%60:02d} para el inicio.")
        print("  Puede cambiar con las velas que faltan cerrar.\n")

    if conf - 0.5 < margin:
        print(f"  SIN OPINION   (confianza {conf*100:.1f}%, hace falta {(0.5+margin)*100:.0f}%)")
        print()
        print("  El modelo no ve nada en esta ronda. No es un fallo: en el 93% de")
        print("  las rondas no hay señal, y las que se saltan son justamente las")
        print("  que hunden el acierto si uno se obliga a opinar siempre.")
        return None

    print(f"  >>> {lado.upper()}   confianza {conf*100:.1f}%")
    print()
    print("  Por que:")
    for k, contrib, valor in contributions(m, feats)[:5]:
        empuja = "Up" if contrib > 0 else "Down"
        nombre = NOMBRES.get(k, k)
        print(f"    {nombre:<38} {valor:>8.3f}  → {empuja}")

    print()
    print(f"  Precio maximo que se puede pagar y no perder plata (fee 2%):")
    print(f"    {conf*0.98:.3f}")
    print(f"  Si el mercado cobra mas que eso por {lado}, no vale la pena:")
    print(f"  la ventaja ya esta en el precio.")
    return lado


def one_shot(m, wait):
    target = next_boundary_ms()
    if wait:
        while True:
            faltan = target / 1000 - time.time()
            if faltan <= 2:
                break
            print(f"\r  esperando el borde de ronda... {int(faltan)//60}:{int(faltan)%60:02d}   ",
                  end="", flush=True)
            time.sleep(min(10, max(1, faltan - 2)))
        print("\r" + " " * 50 + "\r", end="")

    candles = fetch_candles()
    price = fetch_price()
    firme = (target / 1000 - time.time()) <= 5
    p_up, feats = predict_round(m, candles, target, price)
    if p_up is None:
        print("No hay suficientes velas para calcular las features todavia.")
        return
    report(m, target, p_up, feats, price, firme)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--wait", action="store_true",
                    help="esperar al borde de ronda y dar la prediccion definitiva")
    ap.add_argument("--loop", action="store_true",
                    help="predecir cada ronda, indefinidamente")
    args = ap.parse_args()

    m = load_model()
    hasta = datetime.fromtimestamp(m["trained_until_ms"] / 1000, tz=timezone.utc)
    print(f"Modelo: {m['trained_on_rounds']:,} rondas, entrenado hasta {hasta:%d-%b-%Y}")
    print("Acierto medido out-of-sample cuando se pronuncia: 54.5% (765 rondas)\n")

    if not args.loop:
        one_shot(m, args.wait)
        return

    while True:
        try:
            one_shot(m, wait=True)
            print()
        except KeyboardInterrupt:
            return
        except Exception as exc:
            print(f"[error] {exc}")
        time.sleep(10)


if __name__ == "__main__":
    main()
