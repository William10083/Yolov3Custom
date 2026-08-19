"""Los dos modelos, las mismas rondas, marcador en vivo.

Modelo A -- el actual: regresion logistica, 52% en entrenamiento y 52% fuera
de muestra. Aprendio poco y lo poco que aprendio se sostuvo.

Modelo B -- el ajustado al historico: memoriza cada situacion pasada y la
corrige. 91.3% sobre los datos que vio, 50.5% sobre los que no.

Yo ya dije cual creo que va a ganar y por que. Pero eso es una opinion sobre
el futuro, y las opiniones sobre el futuro se resuelven mirando, no
discutiendo. Aca los dos opinan sobre la MISMA ronda, se anota antes de saber
el resultado, y el marcador queda a la vista.

Nada de esto apuesta un centavo.

    python3 duelo.py            # una ronda, ahora
    python3 duelo.py --loop     # cada ronda, con marcador acumulado

Necesita model.json (modelo A) y modelo_ajustado.json.gz (modelo B).
"""
import argparse
import csv
import gzip
import json
import math
import os
import sys
import time
from datetime import datetime, timezone

import predict_model as pm
import predict_next as pn

HERE = os.path.dirname(os.path.abspath(__file__))
AJUSTADO = os.path.join(HERE, "modelo_ajustado.json.gz")
LOG = os.path.join(HERE, "duelo_log.csv")
CAB = ["round_start_ms", "btc", "A_lado", "A_conf", "B_lado", "B_apoyo",
       "outcome", "A_ok", "B_ok"]
ROUND_MS = 5 * 60 * 1000
UMBRAL_A = 0.55


def cargar_B():
    if not os.path.exists(AJUSTADO):
        raise SystemExit(f"Falta {AJUSTADO}. Corre construir_ajustado.py primero.")
    with gzip.open(AJUSTADO, "rt") as f:
        return json.load(f)


def cajon(x, bordes):
    lo, hi = 0, len(bordes)
    while lo < hi:
        mid = (lo + hi) // 2
        if x < bordes[mid]:
            hi = mid
        else:
            lo = mid + 1
    return lo


def predecir_B(mb, feats):
    """(lado, rondas de respaldo) o (None, 0) si nunca vio esta situacion."""
    c = ",".join(str(cajon(feats[k], mb["bordes"][k])) for k in mb["features"])
    celda = mb["tabla"].get(c)
    if celda is None:
        return None, 0
    ups, total = celda
    return ("Up" if ups * 2 >= total else "Down"), total


def leer_log():
    if not os.path.exists(LOG):
        return []
    with open(LOG, newline="") as f:
        filas = list(csv.reader(f))
    if not filas:
        return []
    cuerpo = filas[1:] if filas[0] and filas[0][0] == CAB[0] else filas
    out = []
    for r in cuerpo:
        if not r:
            continue
        d = dict.fromkeys(CAB, "")
        for i, v in enumerate(r):
            if i < len(CAB):
                d[CAB[i]] = v
        out.append(d)
    return out


def escribir_log(filas):
    with open(LOG, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(CAB)
        for r in filas:
            w.writerow([r.get(k, "") for k in CAB])


def calificar(filas):
    """Resuelve las rondas ya cerradas con el mismo criterio del mercado."""
    pend = [r for r in filas if not r["outcome"] and r["round_start_ms"]]
    ahora = int(time.time() * 1000)
    listas = [r for r in pend if int(r["round_start_ms"]) + ROUND_MS + 60_000 < ahora]
    if not listas:
        return filas, 0
    lo = min(int(r["round_start_ms"]) for r in listas)
    hi = max(int(r["round_start_ms"]) for r in listas) + ROUND_MS
    try:
        import requests
        resp = requests.get(pn.KLINES, params={
            "symbol": "BTCUSDT", "interval": "1m",
            "startTime": lo, "endTime": hi + 60_000, "limit": 1000}, timeout=20)
        resp.raise_for_status()
        opens = {int(k[0]): float(k[1]) for k in resp.json()}
    except Exception as exc:
        print(f"  [no se pudo calificar: {exc}]")
        return filas, 0
    n = 0
    for r in listas:
        s = int(r["round_start_ms"])
        a, b = opens.get(s), opens.get(s + ROUND_MS)
        if a is None or b is None:
            continue
        real = "Up" if b > a else "Down"
        r["outcome"] = real
        r["A_ok"] = str(r["A_lado"] == real) if r["A_lado"] else ""
        r["B_ok"] = str(r["B_lado"] == real) if r["B_lado"] else ""
        n += 1
    if n:
        escribir_log(filas)
    return filas, n


def marcador(filas):
    print("  " + "=" * 60)
    print(f"  {'modelo':<28}{'aciertos':>12}{'IC95%':>20}")
    print("  " + "-" * 60)
    for etiq, campo in (("A  actual (logistica)", "A_ok"),
                        ("B  ajustado al historico", "B_ok")):
        hechas = [r for r in filas if r[campo] in ("True", "False")]
        if not hechas:
            print(f"  {etiq:<28}{'sin datos':>12}")
            continue
        k = sum(1 for r in hechas if r[campo] == "True")
        p, lo, hi = pm.wilson(k, len(hechas))
        print(f"  {etiq:<28}{k:>5}/{len(hechas):<6}{p*100:>7.1f}%"
              f"   [{lo*100:.1f}%, {hi*100:.1f}%]")
    print("  " + "=" * 60)


def una_ronda(ma, mb, esperar):
    objetivo = ((int(time.time() * 1000) // ROUND_MS) + 1) * ROUND_MS
    if esperar:
        while True:
            falta = objetivo / 1000 - time.time()
            if falta <= 2:
                break
            if sys.stdout.isatty():
                print(f"\r  esperando... {int(falta)//60}:{int(falta)%60:02d}  ",
                      end="", flush=True)
            time.sleep(min(10, max(1, falta - 2)))
        if sys.stdout.isatty():
            print("\r" + " " * 40 + "\r", end="")

    velas = pn.fetch_candles()
    precio = pn.fetch_price()
    by = {c["open_time_ms"]: c for c in velas}
    feats = pm.features_at(by, objetivo, price_now=precio)
    if feats is None:
        print("  Sin suficientes velas todavia.")
        return

    p_up = pn.apply_model(ma, feats)
    conf_a = max(p_up, 1 - p_up)
    lado_a = ("Up" if p_up >= 0.5 else "Down") if conf_a >= UMBRAL_A else ""
    lado_b, apoyo_b = predecir_B(mb, feats)

    ts = datetime.fromtimestamp(objetivo / 1000, tz=timezone.utc)
    print(f"\n  RONDA {ts:%H:%M} UTC   BTC ${precio:,.2f}")
    print(f"    A  actual:    {lado_a or 'sin opinion':<12} confianza {conf_a*100:.1f}%")
    print(f"    B  ajustado:  {lado_b or 'nunca vio esto':<12} "
          f"respaldo {apoyo_b} ronda(s) del historico")
    if lado_a and lado_b and lado_a != lado_b:
        print("    -> OPINAN DISTINTO. Esta ronda separa a los dos.")

    filas = leer_log()
    if not any(r["round_start_ms"] == str(objetivo) for r in filas):
        filas.append({
            "round_start_ms": objetivo, "btc": f"{precio:.2f}",
            "A_lado": lado_a, "A_conf": f"{conf_a:.4f}",
            "B_lado": lado_b or "", "B_apoyo": apoyo_b,
            "outcome": "", "A_ok": "", "B_ok": "",
        })
        escribir_log(filas)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--loop", action="store_true")
    args = ap.parse_args()

    ma = pn.load_model()
    mb = cargar_B()
    print("=" * 64)
    print("  DUELO -- mismas rondas, anotado antes del resultado")
    print("=" * 64)
    print(f"  A  actual:    52% historico  ·  52% fuera de muestra")
    print(f"  B  ajustado:  91% historico  ·  50.5% fuera de muestra")
    print(f"     ({len(mb['tabla']):,} situaciones memorizadas)")
    print("=" * 64)

    filas, n = calificar(leer_log())
    if n:
        print(f"\n  Calificadas {n} rondas pendientes.")
    marcador(filas)

    if not args.loop:
        una_ronda(ma, mb, esperar=False)
        return
    while True:
        try:
            una_ronda(ma, mb, esperar=True)
            filas, n = calificar(leer_log())
            if n:
                marcador(filas)
        except KeyboardInterrupt:
            return
        except Exception as exc:
            print(f"  [error] {exc}")
        time.sleep(10)


if __name__ == "__main__":
    main()
