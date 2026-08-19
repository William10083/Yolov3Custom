"""Construye el modelo ajustado al historico y lo deja listo para usar.

Esto es exactamente lo pedido: mirar cada ronda del historico, ajustar para
que salga correcta, y repetir hasta acertar todo lo posible. El metodo es
memorizar -- se parten las features en cajones y para cada combinacion se
guarda lo que realmente paso la mayoria de las veces.

El resultado se guarda en modelo_ajustado.json.gz para que `duelo.py` lo corra
en vivo contra el modelo actual, sobre las mismas rondas, y el marcador
decida.

Mi opinion sobre esto ya la di y esta medida en demo_sobreajuste.py: en el
historico llega al ~92% y en rondas nunca vistas cae a ~50%. Pero la opinion
no decide nada -- decide el marcador en vivo, y por eso se construye.

    python3 construir_ajustado.py --data data/btcusdt_1m_long.csv
"""
import argparse
import gzip
import json
import os
from collections import defaultdict

import predict_model as pm

FEATURES = ["mv_5", "mv_15", "vol_15", "eff_15", "flow_15", "pos_60"]
OUT = os.path.join(os.path.dirname(__file__), "modelo_ajustado.json.gz")


def cuantiles(valores, n):
    v = sorted(valores)
    return [v[int(len(v) * i / n)] for i in range(1, n)]


def cajon(x, bordes):
    lo, hi = 0, len(bordes)
    while lo < hi:
        mid = (lo + hi) // 2
        if x < bordes[mid]:
            hi = mid
        else:
            lo = mid + 1
    return lo


def clave(feats, bordes):
    return ",".join(str(cajon(feats[k], bordes[k])) for k in FEATURES)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", required=True)
    ap.add_argument("--cajones", type=int, default=16,
                    help="mas cajones = mas acierto historico y menos cobertura")
    args = ap.parse_args()

    print("Cargando...")
    rows = pm.build_dataset(pm.load_csv(args.data))
    rows.sort(key=lambda r: r["t"])
    n = len(rows)
    tr, te = rows[: int(n * 0.8)], rows[int(n * 0.8):]
    print(f"Ajuste sobre {len(tr):,} rondas   ·   test {len(te):,} nunca vistas\n")

    bordes = {k: cuantiles([r["x"][k] for r in tr], args.cajones) for k in FEATURES}
    tabla = defaultdict(lambda: [0, 0])
    for r in tr:
        c = clave(r["x"], bordes)
        tabla[c][1] += 1
        tabla[c][0] += r["y"]

    # Se guarda el lado mayoritario y con cuanta muestra, para poder exigir un
    # minimo de respaldo antes de opinar.
    compacto = {c: [v[0], v[1]] for c, v in tabla.items()}

    def medir(filas, minimo=1):
        ok = cub = 0
        for r in filas:
            celda = compacto.get(clave(r["x"], bordes))
            if celda is None or celda[1] < minimo:
                continue
            cub += 1
            ok += (1 if celda[0] * 2 >= celda[1] else 0) == r["y"]
        return ok, cub

    print("=" * 70)
    print("RESULTADO DEL AJUSTE")
    print("=" * 70)
    for minimo in (1, 3, 10):
        a_tr, c_tr = medir(tr, minimo)
        a_te, c_te = medir(te, minimo)
        print(f"  respaldo minimo {minimo:>2} rondas por celda:")
        print(f"    historico (ajustado): {a_tr}/{c_tr} = "
              f"{a_tr/c_tr*100 if c_tr else 0:.1f}%")
        if c_te:
            p, lo, hi = pm.wilson(a_te, c_te)
            print(f"    nunca visto:          {a_te}/{c_te} = {p*100:.1f}%  "
                  f"IC95% [{lo*100:.1f}%, {hi*100:.1f}%]  (opina en {c_te/len(te)*100:.0f}%)")
        print()

    payload = {
        "features": FEATURES,
        "bordes": bordes,
        "cajones": args.cajones,
        "tabla": compacto,
        "entrenado_hasta_ms": tr[-1]["t"],
        "nota": ("Ajustado al historico por memorizacion. El acierto historico "
                 "es alto por construccion; el que importa es el de rondas "
                 "nunca vistas, impreso arriba."),
    }
    with gzip.open(OUT, "wt") as f:
        json.dump(payload, f)
    mb = os.path.getsize(OUT) / 1e6
    print(f"Guardado en {OUT}  ({mb:.1f} MB, {len(compacto):,} celdas)")
    print("\nAhora corre el duelo:  python3 duelo.py")


if __name__ == "__main__":
    main()
