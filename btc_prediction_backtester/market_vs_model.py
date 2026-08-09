"""The decisive question: does the market already price what the model sees?

robustness_check.py showed the model's confident calls hit 55.2% on 11,936
rounds it never saw, and that this survives time-splitting and side-balance
checks and barely moves across four orders of magnitude of regularisation.

But every feature comes from candles that closed BEFORE the round opened --
public information the market can price. So 55.2% accuracy proves nothing on
its own:

    paying 0.50 and winning 55.2% of the time  ->  +8.2% per bet
    paying 0.56 and winning 55.2% of the time  ->  -3.4% per bet

The gap between those two lines is the whole project. This script closes it
using the quotes actually collected in round_outcomes.csv: it replays the
frozen model over those same rounds and compares what the model thought
against what the market charged.

Run on the device holding the CSVs:

    python3 market_vs_model.py

Requires model.json (produced by export_model.py). Nothing here is fitted --
the weights are loaded, never trained, so this is a genuine out-of-sample
read on rounds after the model's training cutoff.
"""
import csv
import json
import math
import os
import statistics
import sys
from datetime import datetime, timezone

import requests

import predict_model as pm

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data")
OUTCOMES = os.path.join(DATA, "round_outcomes.csv")
MODEL = os.path.join(HERE, "model.json")

KLINES = "https://data-api.binance.vision/api/v3/klines"
SYMBOL = "BTCUSDT"
ROUND_MS_LOCAL = 5 * 60 * 1000
FEE = 0.02
STAKE = 10.0

OUTCOMES_SCHEMA = [
    "market_id", "round_start_ms", "round_end_ms", "start_price", "end_price",
    "outcome", "market_prob_up", "predicted_side", "predicted_reasoning",
    "signal_correct", "signal_name", "entry_price",
]


def load_positional(path, schema):
    if not os.path.exists(path):
        return []
    with open(path, newline="") as f:
        rows = list(csv.reader(f))
    if not rows:
        return []
    body = rows[1:] if rows[0] and rows[0][0] == schema[0] else rows
    out = []
    for r in body:
        if not r:
            continue
        d = dict.fromkeys(schema, "")
        for i, v in enumerate(r):
            if i < len(schema):
                d[schema[i]] = v
        out.append(d)
    return out


def fnum(s):
    try:
        return float(str(s).strip())
    except (TypeError, ValueError):
        return None


def fetch_range(start_ms, end_ms):
    """1m candles covering [start_ms, end_ms], paged 1000 at a time."""
    out = []
    cursor = start_ms
    while cursor <= end_ms:
        r = requests.get(
            KLINES,
            params={
                "symbol": SYMBOL, "interval": "1m",
                "startTime": cursor, "endTime": end_ms, "limit": 1000,
            },
            timeout=30,
        )
        r.raise_for_status()
        rows = r.json()
        if not rows:
            break
        for k in rows:
            out.append({
                "open_time_ms": int(k[0]),
                "open": float(k[1]),
                "high": float(k[2]),
                "low": float(k[3]),
                "close": float(k[4]),
                "volume": float(k[5]),
                "taker_buy_base": float(k[9]),
            })
        cursor = int(rows[-1][0]) + 60_000
        sys.stdout.write(f"\r  descargadas {len(out)} velas...")
        sys.stdout.flush()
    print()
    return out


def load_model():
    with open(MODEL) as f:
        m = json.load(f)
    return m


def apply_model(m, feats):
    keys = m["keys"]
    v = []
    for k in keys:
        st = m["standardize"][k]
        v.append((feats[k] - st["mean"]) / st["std"])
    z = m["bias"] + sum(wi * vi for wi, vi in zip(m["weights"], v))
    return 1 / (1 + math.exp(-max(-30, min(30, z))))


def wilson(k, n, z=1.96):
    if n == 0:
        return (0.0, 0.0, 0.0)
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    mrg = (z * ((p * (1 - p) / n + z * z / (4 * n * n)) ** 0.5)) / d
    return (p, max(0.0, c - mrg), min(1.0, c + mrg))


def correlation(xs, ys):
    if len(xs) < 3:
        return None
    mx, my = statistics.fmean(xs), statistics.fmean(ys)
    num = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    dx = math.sqrt(sum((x - mx) ** 2 for x in xs))
    dy = math.sqrt(sum((y - my) ** 2 for y in ys))
    return num / (dx * dy) if dx and dy else None


def main():
    if not os.path.exists(MODEL):
        print(f"Falta {MODEL}. Corre primero:  python3 export_model.py")
        return

    m = load_model()
    margin = m.get("confidence_margin", 0.05)
    CLAIMED = m.get("accuracy_confident", 0.545)
    cutoff = m.get("trained_until_ms", 0)

    outcomes = load_positional(OUTCOMES, OUTCOMES_SCHEMA)
    rounds = []
    for r in outcomes:
        start = fnum(r.get("round_start_ms"))
        prob_up = fnum(r.get("market_prob_up"))
        res = (r.get("outcome") or "").strip()
        if start is None or prob_up is None or res not in ("Up", "Down"):
            continue
        if not 0 < prob_up < 1:
            continue
        rounds.append({"t": int(start), "prob_up": prob_up, "y": 1 if res == "Up" else 0})

    print(f"Rondas en round_outcomes.csv: {len(outcomes)}")
    print(f"Rondas con precio de mercado y resultado: {len(rounds)}")
    if len(rounds) < 20:
        print("\nMuy pocas para responder nada. Segui recolectando con el logger.")
        return

    posteriores = sum(1 for r in rounds if r["t"] > cutoff)
    print(f"Posteriores al corte de entrenamiento del modelo: {posteriores}  "
          f"({'todas out-of-sample' if posteriores == len(rounds) else 'OJO: algunas no'})")

    lo_ms = min(r["t"] for r in rounds) - 260 * 60_000
    hi_ms = max(r["t"] for r in rounds) + 6 * 60_000
    d0 = datetime.fromtimestamp(lo_ms / 1000, tz=timezone.utc)
    d1 = datetime.fromtimestamp(hi_ms / 1000, tz=timezone.utc)
    print(f"\nDescargando velas de {d0:%d-%b %H:%M} a {d1:%d-%b %H:%M} UTC...")
    candles = fetch_range(lo_ms, hi_ms)
    if not candles:
        print("No se pudieron descargar velas.")
        return

    # Reuse build_dataset so the features are computed by exactly the same code
    # that produced the headline number -- no reimplementation to drift apart.
    feat_rows = {row["t"]: row for row in pm.build_dataset(candles)}

    joined = []
    for r in rounds:
        fr = feat_rows.get(r["t"])
        if fr is None:
            continue
        p_up = apply_model(m, fr["x"])
        joined.append({**r, "p_up": p_up, "y_real": fr["y"]})

    print(f"Rondas con features completas: {len(joined)}")
    if len(joined) < 20:
        print("Muy pocas tras el cruce. Segui recolectando.")
        return

    # -------------------------------------------------- ¿el mercado ya lo sabe?
    print("\n" + "=" * 74)
    print("¿EL MERCADO YA COTIZA LO QUE VE EL MODELO?")
    print("=" * 74)
    corr = correlation([j["p_up"] for j in joined], [j["prob_up"] for j in joined])
    print(f"  Correlacion entre confianza del modelo y precio del mercado: {corr:+.3f}")
    if corr is None:
        pass
    elif corr > 0.5:
        print("  Alta: el mercado ve practicamente lo mismo. El precio sube")
        print("  cuando el modelo sube, y el margen se evapora.")
    elif corr > 0.2:
        print("  Moderada: coinciden en parte, queda algo de espacio.")
    else:
        print("  Baja: el mercado NO se mueve con el modelo. Ahi puede haber margen.")

    dif = statistics.fmean(abs(j["p_up"] - j["prob_up"]) for j in joined)
    print(f"  Diferencia media |modelo - mercado|: {dif*100:.1f} pp")

    # ------------------------------------------------- las rondas de confianza
    print("\n" + "=" * 74)
    print(f"RONDAS DONDE EL MODELO SE COMPROMETE (confianza >= {0.5+margin:.2f})")
    print("=" * 74)
    conf = [j for j in joined if abs(j["p_up"] - 0.5) >= margin]
    if not conf:
        print("  Ninguna ronda supero el umbral en esta muestra.")
        return

    for j in conf:
        j["side"] = "Up" if j["p_up"] >= 0.5 else "Down"
        # Price of the model's side. The logged field is the Up chance, so the
        # Down side is its complement -- an approximation that ignores the
        # spread, and one that if anything flatters the result.
        j["price"] = j["prob_up"] if j["side"] == "Up" else 1 - j["prob_up"]
        j["won"] = (j["side"] == "Up") == (j["y"] == 1)
        j["conf"] = max(j["p_up"], 1 - j["p_up"])

    hits = sum(1 for j in conf if j["won"])
    acc, lo, hi = wilson(hits, len(conf))
    avg_price = statistics.fmean(j["price"] for j in conf)
    avg_conf = statistics.fmean(j["conf"] for j in conf)
    breakeven = avg_price / (1 - FEE)

    print(f"  Rondas: {len(conf)} de {len(joined)}  ({len(conf)/len(joined)*100:.0f}%)")
    print(f"  Acierto: {hits}/{len(conf)} = {acc*100:.1f}%   IC95% [{lo*100:.1f}%, {hi*100:.1f}%]")
    print(f"  Confianza media del modelo: {avg_conf*100:.1f}%")
    print(f"  Precio medio cobrado por el mercado: {avg_price:.3f}")
    print(f"  Acierto necesario a ese precio con fee 2%: {breakeven*100:.1f}%")

    print("\n  Detalle (modelo vs mercado, ronda por ronda):")
    print(f"    {'hora UTC':<14}{'lado':<6}{'modelo':>8}{'precio':>9}{'result':>9}")
    for j in sorted(conf, key=lambda x: x["t"])[:40]:
        ts = datetime.fromtimestamp(j["t"] / 1000, tz=timezone.utc)
        marca = "✓" if j["won"] else "✗"
        print(f"    {ts:%d-%b %H:%M}  {j['side']:<6}{j['conf']*100:>7.1f}%{j['price']:>9.3f}"
              f"{marca:>9}")
    if len(conf) > 40:
        print(f"    ... y {len(conf)-40} mas")

    # ------------------------------------------- la verificacion mejor medida
    # Las 17 rondas de confianza no alcanzan para nada. Pero la direccion que
    # el modelo prefiere en TODAS las rondas si tiene muestra, y es la unica
    # forma barata de ver si los 4 anios de historia se trasladan a hoy.
    print("\n" + "=" * 74)
    print("TODAS LAS RONDAS: ¿el modelo apunta al lado correcto mas de la mitad?")
    print("=" * 74)
    aciertos_todas = sum(
        1 for j in joined if ((j["p_up"] >= 0.5) == (j["y"] == 1))
    )
    pa, la, ha = wilson(aciertos_todas, len(joined))
    print(f"  {aciertos_todas}/{len(joined)} = {pa*100:.1f}%   IC95% [{la*100:.1f}%, {ha*100:.1f}%]")
    print(f"  (en 18 meses de walk-forward, opinando siempre, dio 52.1%)")
    if ha < 0.5:
        print("  Significativamente por DEBAJO del azar en este periodo.")
    elif la > 0.5:
        print("  Significativamente por encima del azar. La señal se traslada.")
    else:
        print("  No se distingue del azar en este periodo. Con esta muestra")
        print("  no se puede confirmar ni descartar que la señal se traslade.")

    # ------------------------------------------------- cuantos datos hay de verdad
    print("\n" + "=" * 74)
    print("CUANTA MUESTRA INDEPENDIENTE HAY REALMENTE")
    print("=" * 74)
    ordenadas = sorted(conf, key=lambda x: x["t"])
    episodios = 1
    for a, b in zip(ordenadas, ordenadas[1:]):
        if b["t"] - a["t"] > ROUND_MS_LOCAL:
            episodios += 1
    span_h = (max(j["t"] for j in joined) - min(j["t"] for j in joined)) / 3600_000
    print(f"  Rondas de confianza: {len(conf)}")
    print(f"  Episodios (rondas consecutivas cuentan como uno): {episodios}")
    print(f"  Periodo cubierto: {span_h:.1f} horas ({span_h/24:.1f} dias)")
    print()
    print("  Rondas seguidas comparten estado de mercado y ventanas de features:")
    print("  no son observaciones independientes. El IC de arriba asume que si,")
    print("  asi que el intervalo real es todavia MAS ancho que el impreso.")
    if span_h < 48:
        print()
        print("  Y todo esto es de menos de dos dias. El acierto historico se")
        print("  midio sobre 18 meses. Un dia malo no dice nada del modelo,")
        print("  igual que un dia bueno no lo confirmaria.")

    # --------------------------------------------------------------- paper P&L
    print("\n" + "=" * 74)
    print(f"SIMULACION EN PAPEL  (${STAKE:.0f} por ronda, sin apostar un centavo)")
    print("=" * 74)
    pnl = 0.0
    bankroll = 10.0
    quiebra = None
    for i, j in enumerate(sorted(conf, key=lambda x: x["t"]), 1):
        pnl += (STAKE * (1 - FEE) / j["price"] - STAKE) if j["won"] else -STAKE
        if bankroll > 0:
            bet = min(STAKE, bankroll)
            bankroll -= bet
            if j["won"]:
                bankroll += bet * (1 - FEE) / j["price"]
            if bankroll <= 0.009 and quiebra is None:
                quiebra = i
    roi = pnl / (STAKE * len(conf)) * 100
    print(f"  P&L acumulado a apuesta fija: ${pnl:+.2f}   ROI {roi:+.1f}%")
    print(f"  Banca de $10: " + (f"quiebra en la ronda #{quiebra}" if quiebra else f"${bankroll:.2f}"))

    # ----------------------------------------------------------------- lectura
    print("\n" + "=" * 74)
    print("LECTURA")
    print("=" * 74)
    if lo > breakeven:
        print(f"  El limite inferior del acierto ({lo*100:.1f}%) supera el breakeven")
        print(f"  que impone el precio ({breakeven*100:.1f}%). El mercado NO estaba")
        print("  cotizando esto. Vale seguir midiendo con mas rondas antes de")
        print("  arriesgar dinero, pero es la primera señal que pasa todos los filtros.")
    elif lo < CLAIMED < hi:
        # The measured accuracy is inside the range the model claims, so this
        # sample cannot tell the two apart -- whichever side of breakeven the
        # point estimate happens to land on.
        print(f"  El acierto medido ({acc*100:.1f}%) queda por debajo del breakeven")
        print(f"  ({breakeven*100:.1f}%), pero el IC95% [{lo*100:.1f}%, {hi*100:.1f}%] contiene")
        print(f"  tanto el breakeven como el {CLAIMED*100:.1f}% que el modelo declara. Con esta")
        print("  muestra NO se puede distinguir entre las dos cosas: no es")
        print("  evidencia en contra, es falta de datos.")
    else:
        print(f"  El acierto ({acc*100:.1f}%) NO cubre el breakeven ({breakeven*100:.1f}%)")
        print("  que impone el precio del mercado, y el intervalo tampoco llega.")
        print("  Con esta muestra el modelo no paga lo que cuesta jugar.")

    # What it would actually take to settle this -- the honest number, not a
    # comforting one. Separating 54.5% from the breakeven the market charges
    # needs the interval narrower than the gap between them.
    print("\n  Cuanto falta para decidirlo de verdad:")
    gap = CLAIMED - breakeven
    if gap <= 0:
        print(f"    Al precio que cobra el mercado ({avg_price:.3f}) el breakeven es")
        print(f"    {breakeven*100:.1f}%, por encima del {CLAIMED*100:.1f}% del modelo. No hay nada")
        print("    que medir: aunque el modelo funcione, a este precio no alcanza.")
    else:
        need = CLAIMED * (1 - CLAIMED) * (1.96 / gap) ** 2
        total = need / (len(conf) / len(joined))
        print(f"    separar {CLAIMED*100:.1f}% de {breakeven*100:.1f}% (brecha {gap*100:.1f} pp) exige"
              f" ~{need:,.0f} rondas de confianza")
        print(f"    a un {len(conf)/len(joined)*100:.0f}% de disparo -> ~{total:,.0f} rondas totales"
              f" = ~{total*5/60/24:.0f} dias de logging continuo")
        print(f"    llevas {len(conf)}")
    print(f"\n  Muestra: {len(conf)} rondas. Todo lo de arriba es en papel.")


if __name__ == "__main__":
    main()
