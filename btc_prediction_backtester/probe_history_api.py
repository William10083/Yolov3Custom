"""¿La API guarda el historial de precios del mercado de prediccion?

Toda la espera del proyecto -- juntar dias con el logger -- existe por una
suposicion mia: que el order book es efimero y nadie guarda a cuanto cotizaba
"Up" el martes a las 14:35. Si esa suposicion es falsa, los dias de
recoleccion sobran y la pregunta se contesta hoy.

Nunca la verifique. Este script la verifica.

Los dos endpoints conocidos (`market/list`, `order-book`) salieron de
ingenieria inversa, no de documentacion publica, asi que puede perfectamente
haber mas en el mismo namespace. Esto prueba nombres plausibles y reporta
cuales responden 200.

Es de SOLO LECTURA: puros GET. No coloca ordenes ni mueve fondos. Aun asi,
leelo antes de correrlo -- es tu cuenta.

    export BINANCE_API_KEY=...      (mejor: source ~/.btc_env)
    export BINANCE_API_SECRET=...
    python3 probe_history_api.py

Una pista que vale mas que cualquier adivinanza mia: si la app te muestra un
GRAFICO de como se movio el precio de Up durante la ronda, esos datos salen
de algun lado y ese endpoint existe. Mira la pantalla del mercado; si hay
curva, hay historial.
"""
import hashlib
import hmac
import json
import os
import time
import urllib.parse

import requests

API_KEY = os.environ.get("BINANCE_API_KEY")
API_SECRET = os.environ.get("BINANCE_API_SECRET")
if not API_KEY or not API_SECRET:
    raise SystemExit(
        "Faltan las llaves. Antes de correr:\n"
        "  export BINANCE_API_KEY=...\n"
        "  export BINANCE_API_SECRET=...\n"
        "(nunca las pegues en un chat)"
    )

REST_BASE = "https://api.binance.com"
NS = "/sapi/v1/w3w/wallet/prediction"

# Nombres plausibles dentro del mismo namespace. Ninguno esta documentado --
# son conjeturas informadas por como suelen llamarse estas cosas.
CANDIDATOS = [
    (f"{NS}/market/list", {"limit": 5}),
    (f"{NS}/market/list", {"limit": 5, "status": "CLOSED"}),
    (f"{NS}/market/list", {"limit": 5, "status": "RESOLVED"}),
    (f"{NS}/market/detail", {}),
    (f"{NS}/market/history", {"limit": 5}),
    (f"{NS}/market/closed", {"limit": 5}),
    (f"{NS}/market/round/list", {"limit": 5}),
    (f"{NS}/round/list", {"limit": 5}),
    (f"{NS}/round/history", {"limit": 5}),
    (f"{NS}/price/history", {"limit": 5}),
    (f"{NS}/price/kline", {"limit": 5}),
    (f"{NS}/kline", {"limit": 5}),
    (f"{NS}/klines", {"limit": 5}),
    (f"{NS}/candles", {"limit": 5}),
    (f"{NS}/chart", {"limit": 5}),
    (f"{NS}/trades", {"limit": 5}),
    (f"{NS}/trade/list", {"limit": 5}),
    (f"{NS}/trade/history", {"limit": 5}),
    (f"{NS}/order-book/history", {"limit": 5}),
    (f"{NS}/settlement/list", {"limit": 5}),
    (f"{NS}/statistics", {}),
]


def sondear(path, params):
    p = dict(params)
    p["timestamp"] = str(int(time.time() * 1000))
    p["recvWindow"] = "5000"
    qs = urllib.parse.urlencode(sorted(p.items()))
    sig = hmac.new(API_SECRET.encode(), qs.encode(), hashlib.sha256).hexdigest()
    url = f"{REST_BASE}{path}?{qs}&signature={sig}"
    try:
        r = requests.get(url, headers={"X-MBX-APIKEY": API_KEY}, timeout=15)
    except Exception as exc:
        return None, f"error de red: {exc}"
    cuerpo = r.text[:400].replace("\n", " ")
    return r.status_code, cuerpo


def main():
    print("Sondeando el namespace de prediccion. Solo GET, solo lectura.\n")
    vivos = []
    for path, params in CANDIDATOS:
        extra = "".join(f" {k}={v}" for k, v in params.items() if k != "limit")
        etiqueta = path.replace(NS, "…") + extra
        code, cuerpo = sondear(path, params)
        if code == 200:
            vivos.append((path, params, cuerpo))
            print(f"  200  {etiqueta}")
            print(f"       {cuerpo[:200]}")
        elif code is None:
            print(f"  ---  {etiqueta}  ({cuerpo})")
        else:
            # -1121 / 404 = no existe; -2015 = existe pero sin permiso.
            corto = cuerpo[:110]
            print(f"  {code}  {etiqueta}  {corto}")
        time.sleep(0.4)  # no gatillar rate limits

    print("\n" + "=" * 70)
    print("LECTURA")
    print("=" * 70)
    if not vivos:
        print("  Ningun endpoint de historial respondio. Con esto queda")
        print("  confirmado que el precio historico hay que juntarlo en vivo,")
        print("  que es lo que hace run_all.py.")
        print()
        print("  Igual mira la app: si el mercado muestra un grafico de como")
        print("  se movio el precio, ese endpoint existe y solo hay que dar")
        print("  con su nombre. Se puede capturar con las DevTools del navegador")
        print("  en la version web y pasarme la URL que aparece.")
    else:
        print(f"  Respondieron {len(vivos)} endpoint(s):")
        for path, params, _ in vivos:
            print(f"    {path}  {params}")
        print()
        print("  Pegame la salida completa. Si alguno trae precios pasados por")
        print("  ronda, la espera de dias se cae y contestamos hoy.")

    print("\n  Nada de esto coloca ordenes ni mueve fondos: son GET.")


if __name__ == "__main__":
    main()
