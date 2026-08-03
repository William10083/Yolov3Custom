# BTC "Up or Down 5m" -- Backtester (investigacion, sin dinero real)

Este proyecto **no coloca apuestas reales ni hace clicks automaticos**. Es
una herramienta para medir, con precios historicos reales de BTC, si
alguna estrategia simple tiene una ventaja real sobre el juego de
prediccion "BTC Up or Down 5m" (tipo el de la pestana Prediccion de
Binance) antes de arriesgar dinero en el.

## Por que existe

Ver la conversacion que lo origino: el juego de 5 minutos parece tentador
porque uno "siente" rachas (varios Down seguidos, etc.), pero eso puede
ser pura falacia del apostador. En vez de confiar en la intuicion o en
bots de terceros sin resultados verificables, esto corre las estrategias
mas obvias contra datos reales y reporta si de verdad ganan mas de lo que
cuesta la comision.

## Como funciona

1. `data_fetch.py` descarga velas de 1 minuto de BTCUSDT desde el mirror
   publico de datos de mercado de Binance (`data-api.binance.vision`,
   sin API key, sin acceso a cuenta ni ordenes -- solo precios historicos).
2. `backtest.py` arma rondas de 5 minutos alineadas al reloj (:00, :05,
   :10, ...) igual que el juego, y evalua cada estrategia de forma
   **walk-forward**: cada prediccion solo puede usar datos de ANTES del
   inicio de esa ronda (cero adelanto de informacion / data leakage).
3. Los datos se dividen en dos mitades: in-sample (para mirar) y
   out-of-sample (para confirmar), asi no nos enganamos con una
   estrategia que solo se ve bien por azar.
4. Se reporta el win rate con intervalo de confianza 95% (Wilson score) y
   se compara contra el win rate necesario para no perder dinero dado un
   supuesto de comision (`--fee`, default 10%).

## Estrategias probadas (`strategies.py`)

- Baseline aleatorio (referencia, deberia dar ~50%).
- Momentum: apostar a que continua la direccion de las ultimas N rondas.
- Contrarian: apostar lo opuesto a las ultimas N rondas.
- Reversion/continuacion de racha: solo apuesta cuando hay una racha de
  K rondas iguales seguidas (el "solo cuando es muy seguro" que pediste).
- Micro-momentum / micro-mean-reversion: usa el movimiento de precio de
  los ultimos 1/3/5 minutos antes de que arranque la ronda, con filtro
  opcional de magnitud minima de movimiento (mas conviccion = solo
  dispara si el movimiento fue de al menos X%).

## Analisis de tu lista manual (`manual_sequence_analysis.py`)

Ademas se analizo la secuencia de 36 resultados que anotaste a mano
(transiciones Up/Down, reversion tras rachas). Con esa muestra tan
chica los intervalos de confianza son enormes (ej. 23%-64%), asi que
sirve solo como sanity-check, no como senal por si sola. Ver la salida
completa corriendo el script.

## Resultado real (45 dias de datos, 12,959 rondas de 5 min, corrido el 2026-08-03)

```
Baseline real Up/Down en todo el periodo: 49.3% Up / 50.7% Down
Con una comision asumida de 10%, hace falta un win rate > 52.6% para ganar en el largo plazo

Ninguna estrategia supera el breakeven de forma estadisticamente
significativa (limite inferior del IC95% out-of-sample > breakeven)
en este periodo de datos.
```

Dato interesante (no suficiente para apostar, pero real): las variantes
**contrarian / mean-reversion** rondan 51-52% out-of-sample de forma
consistente, mientras que **momentum** ronda 48-49%. Esto es coherente
con el "bid-ask bounce" tipico de microestructura de mercado (el precio
rebota un poco tras un movimiento brusco), pero el margen no alcanza a
cubrir una comision del 10%. Con comision 0% alguna variante contrarian
casi tocaria breakeven -- lo cual confirma que **la comision/rake es la
verdadera barrera**, no la falta de patrones.

## Como correrlo vos mismo

```bash
cd btc_prediction_backtester
pip install -r requirements.txt
python3 data_fetch.py --days 45      # descarga y cachea datos reales (no hay datos en git)
python3 backtest.py --fee 10          # corre el backtest completo
python3 manual_sequence_analysis.py   # analiza tu lista de 36 resultados
```

## Limitaciones importantes

- El 59%/40% que se ve en la app es la distribucion del pool de apuestas
  (cuanta gente aposto a cada lado), **no** un dato de precio -- no hay
  forma de reconstruir eso historicamente con datos publicos, asi que
  no esta modelado aqui.
- La comision real de la plataforma no es publica con certeza; el
  parametro `--fee` es un supuesto ajustable, no un dato confirmado.
- El precio de resolucion exacto de cada ronda en la app puede diferir
  ligeramente del close de la vela de 1m usado aqui (redondeo, fuente de
  precio distinta, exchange distinto).
- No hay ejecucion de ordenes ni automatizacion de clicks en este
  proyecto, ni la habra hasta que una estrategia muestre ventaja real y
  sostenida en datos out-of-sample.

## Conclusion honesta

Con 45 dias de datos reales, ninguna de las estrategias obvias (momentum,
contrarian, rachas, micro-momentum) genera una ventaja que cubra una
comision tipica. Esto es consistente con lo que se encontro investigando
bots publicos para juegos similares (Polymarket BTC 5m): nadie mostro
una ventaja sostenida y verificable. Si en el futuro se quiere revisar de
nuevo, correr `backtest.py` con datos mas recientes (`data_fetch.py
--days N`) es gratis y no arriesga nada.
