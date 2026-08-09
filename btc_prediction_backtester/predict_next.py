"""Predice la proxima vela de 5 minutos: Up o Down.

Usa el modelo congelado en model.json -- el mismo que dio 54.3% de acierto
en walk-forward sobre 18 meses y 23,571 llamadas, y que aguanto los controles
de robustness_check.py. Aca no se entrena nada: se cargan los pesos y se
aplican.

La fecha de entrenamiento se ve vieja (Dic-2024) y no lo es en la practica:
walk_forward.py comparo este modelo congelado contra reentrenarlo cada mes
con toda la historia disponible, y la diferencia fue +0.22 pp con z=0.41 --
indistinguible de cero. El corte viejo no le cuesta acierto.

Lo importante de como funciona:

  - Solo se pronuncia cuando la confianza llega al umbral. En el ~86% de las
    rondas el modelo no tiene nada que decir, y decirlo es la respuesta
    correcta. Forzar una opinion en cada ronda es exactamente lo que baja el
    acierto de 54.3% a 52.1%.
  - Las features salen de velas cerradas ANTES del inicio de la ronda, asi que
    la prediccion queda firme recien en el borde de los :00/:05/:10. Antes de
    eso es preliminar y lo dice.
  - 54.3% no es dinero. A un precio de 0.50 deja margen; a 0.55 no deja nada.
    Sin comparar contra el precio cotizado esto es una prediccion, no una
    apuesta -- para eso esta market_vs_model.py.

    python3 predict_next.py           # una prediccion
    python3 predict_next.py --wait    # espera al borde y da la definitiva
    python3 predict_next.py --loop    # se queda prediciendo cada ronda
"""
import argparse
import csv
import json
import math
import os
import sys
import time
from datetime import datetime, timezone

import requests

import predict_model as pm

HERE = os.path.dirname(os.path.abspath(__file__))
MODEL = os.path.join(HERE, "model.json")
LOG = os.path.join(HERE, "predictions_log.csv")
LOG_HEADER = [
    "logged_at_ms", "round_start_ms", "side", "confidence",
    "btc_at_prediction", "outcome", "correct",
]
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


def read_log():
    if not os.path.exists(LOG):
        return []
    with open(LOG, newline="") as f:
        rows = list(csv.reader(f))
    if not rows:
        return []
    body = rows[1:] if rows[0] and rows[0][0] == LOG_HEADER[0] else rows
    out = []
    for r in body:
        if not r:
            continue
        d = dict.fromkeys(LOG_HEADER, "")
        for i, v in enumerate(r):
            if i < len(LOG_HEADER):
                d[LOG_HEADER[i]] = v
        out.append(d)
    return out


def write_log(rows):
    with open(LOG, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(LOG_HEADER)
        for r in rows:
            w.writerow([r.get(k, "") for k in LOG_HEADER])


def record(round_start_ms, side, conf, price):
    """Write the call BEFORE the round resolves.

    This is the whole point of the file. A prediction written down after the
    fact can be reinterpreted; one written down first cannot. Everything
    measured in this repo that later fell apart fell apart because the rule
    was chosen after seeing the outcome.
    """
    rows = read_log()
    if any(r["round_start_ms"] == str(round_start_ms) for r in rows):
        return
    rows.append({
        "logged_at_ms": int(time.time() * 1000),
        "round_start_ms": round_start_ms,
        "side": side,
        "confidence": f"{conf:.4f}",
        "btc_at_prediction": f"{price:.2f}",
        "outcome": "",
        "correct": "",
    })
    write_log(rows)


def grade_pending():
    """Resolve any logged prediction whose round has already finished.

    Uses the same rule the market uses and the backtest used: the OPEN of the
    candle at the round's end versus the OPEN at its start.
    """
    rows = read_log()
    pending = [r for r in rows if not r["outcome"] and r["round_start_ms"]]
    now_ms = int(time.time() * 1000)
    due = [r for r in pending if int(r["round_start_ms"]) + ROUND_MS + 60_000 < now_ms]
    if not due:
        return rows, 0

    lo = min(int(r["round_start_ms"]) for r in due)
    hi = max(int(r["round_start_ms"]) for r in due) + ROUND_MS
    try:
        resp = requests.get(
            KLINES,
            params={"symbol": SYMBOL, "interval": "1m",
                    "startTime": lo, "endTime": hi + 60_000, "limit": 1000},
            timeout=20,
        )
        resp.raise_for_status()
        opens = {int(k[0]): float(k[1]) for k in resp.json()}
    except Exception as exc:
        print(f"  [no se pudo calificar: {exc}]")
        return rows, 0

    graded = 0
    for r in due:
        s = int(r["round_start_ms"])
        a, b = opens.get(s), opens.get(s + ROUND_MS)
        if a is None or b is None:
            continue
        r["outcome"] = "Up" if b > a else "Down"
        r["correct"] = str(r["side"] == r["outcome"])
        graded += 1
    if graded:
        write_log(rows)
    return rows, graded


def show_tally(rows):
    done = [r for r in rows if r["correct"] in ("True", "False")]
    if not done:
        return
    hits = sum(1 for r in done if r["correct"] == "True")
    n = len(done)
    p = hits / n
    z = 1.96
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    mrg = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    lo, hi = max(0.0, c - mrg), min(1.0, c + mrg)
    print(f"Historial propio: {hits}/{n} = {p*100:.1f}%   "
          f"IC95% [{lo*100:.1f}%, {hi*100:.1f}%]")
    if n < 100:
        print(f"  (muy poco todavia para significar nada -- el intervalo lo dice)")
    print()


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
        if faltan > 0:
            print(f"  PRELIMINAR — faltan {int(faltan)//60}:{int(faltan)%60:02d} para el inicio.")
            print("  Puede cambiar con las velas que faltan cerrar.")
        else:
            print(f"  TARDE — la ronda arranco hace {int(-faltan)}s. No se anota.")
        print()

    if conf - 0.5 < margin:
        print(f"  SIN OPINION   (confianza {conf*100:.1f}%, hace falta {(0.5+margin)*100:.0f}%)")
        print()
        print("  El modelo no ve nada en esta ronda. No es un fallo: en el 85% de")
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
        # The countdown redraws one line with \r and no newline, which is right
        # for a terminal and wrong for anything reading line by line -- run_all.py
        # would block waiting for a newline that never arrives. Off when piped.
        spinner = sys.stdout.isatty()
        while True:
            faltan = target / 1000 - time.time()
            if faltan <= 2:
                break
            if spinner:
                print(f"\r  esperando el borde de ronda... "
                      f"{int(faltan)//60}:{int(faltan)%60:02d}   ", end="", flush=True)
            time.sleep(min(10, max(1, faltan - 2)))
        if spinner:
            print("\r" + " " * 50 + "\r", end="")

    candles = fetch_candles()
    price = fetch_price()
    # Firm only in a window around the boundary. Too early and the reading can
    # still change; too late and `price` is already the round in progress,
    # which is information the model is not supposed to have.
    faltan_s = target / 1000 - time.time()
    firme = -60 <= faltan_s <= 5
    p_up, feats = predict_round(m, candles, target, price)
    if p_up is None:
        print("No hay suficientes velas para calcular las features todavia.")
        return
    lado = report(m, target, p_up, feats, price, firme)

    # Only a firm call gets recorded. A preliminary reading can still change,
    # and logging one would quietly turn "what the model said" into "what the
    # model said at whatever moment I happened to look".
    if lado and firme:
        record(target, lado, max(p_up, 1 - p_up), price)
        print(f"\n  [anotado en {os.path.basename(LOG)} antes de conocer el resultado]")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--wait", action="store_true",
                    help="esperar al borde de ronda y dar la prediccion definitiva")
    ap.add_argument("--loop", action="store_true",
                    help="predecir cada ronda, indefinidamente")
    args = ap.parse_args()

    m = load_model()
    hasta = datetime.fromtimestamp(m["trained_until_ms"] / 1000, tz=timezone.utc)
    acc = m.get("accuracy_confident")
    ci = m.get("accuracy_confident_ci95") or [0, 0]
    nn = m.get("accuracy_confident_n") or 0
    print(f"Modelo: {m['trained_on_rounds']:,} rondas, entrenado hasta {hasta:%d-%b-%Y}")
    if acc:
        print(f"Acierto out-of-sample cuando se pronuncia: {acc*100:.1f}%  "
              f"IC95% [{ci[0]*100:.1f}%, {ci[1]*100:.1f}%]  ({nn:,} rondas)\n")
    else:
        print()

    rows, graded = grade_pending()
    if graded:
        print(f"Calificadas {graded} predicciones pendientes.")
    show_tally(rows)

    if not args.loop:
        one_shot(m, args.wait)
        return

    while True:
        try:
            one_shot(m, wait=True)
            print()
            rows, graded = grade_pending()
            if graded:
                show_tally(rows)
        except KeyboardInterrupt:
            return
        except Exception as exc:
            print(f"[error] {exc}")
        time.sleep(10)


if __name__ == "__main__":
    main()
