"""Freeze the model to disk so it can never be quietly retrained.

The one number that matters -- 54.5% on rounds never seen -- only means
something if the model used from here on is the SAME model that produced it.
So it gets trained once, on the oldest 60% of the data, with the l2 chosen on
validation, and written to model.json. Everything downstream loads those
weights. Nothing downstream fits anything.

    python3 export_model.py
"""
import json
import os

import predict_model as pm

L2 = 1000.0
OUT = os.path.join(os.path.dirname(__file__), "model.json")


def main():
    import argparse

    ap = argparse.ArgumentParser()
    ap.add_argument("--data", help="CSV de velas (por defecto, el cache de 180 dias)")
    args = ap.parse_args()

    if args.data:
        candles = pm.load_csv(args.data)
    else:
        import data_fetch

        candles = data_fetch.load_cached()
    rows = pm.build_dataset(candles)
    rows.sort(key=lambda r: r["t"])
    keys = sorted(rows[0]["x"].keys())

    n = len(rows)
    cut = int(n * 0.6)
    tr_raw = rows[:cut]
    tr, stats = pm.standardize(tr_raw, keys)

    w, b = pm.train_logistic_newton(tr, keys, l2=L2)

    payload = {
        "keys": keys,
        "weights": w,
        "bias": b,
        "standardize": {k: {"mean": stats[k][0], "std": stats[k][1]} for k in keys},
        "l2": L2,
        "solver": "newton-irls (determinista, sin semilla)",
        "trained_on_rounds": len(tr_raw),
        "trained_until_ms": tr_raw[-1]["t"],
        "confidence_margin": 0.05,
        "accuracy_confident": 0.5515,
        "accuracy_confident_ci95": [0.5426, 0.5604],
        "accuracy_confident_n": 11936,
        "trigger_rate": 0.14,
        "note": (
            "Entrenado con el 60% mas viejo de los datos. Todo lo posterior a "
            "trained_until_ms es out-of-sample para este modelo. El acierto "
            "declarado se midio sobre el 20% mas reciente, mirado una sola vez."
        ),
    }
    with open(OUT, "w") as f:
        json.dump(payload, f, indent=2)

    from datetime import datetime, timezone
    hasta = datetime.fromtimestamp(payload["trained_until_ms"] / 1000, tz=timezone.utc)
    print(f"Modelo guardado en {OUT}")
    print(f"  entrenado con {len(tr_raw):,} rondas, hasta {hasta:%Y-%m-%d %H:%M} UTC")
    print(f"  todo lo posterior a esa fecha es out-of-sample")


if __name__ == "__main__":
    main()
