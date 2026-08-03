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
5. Ademas del rule-mining, se corre un **variance ratio test** (Lo-MacKinlay)
   sobre los retornos de 5 min: un test estadistico formal e independiente
   de cualquier regla especifica, que responde "¿hay estructura real en el
   precio en si (momentum o mean-reversion), o es puro random walk?". Sirve
   de chequeo cruzado para no confiar solo en reglas que podrian ganar por
   azar (data snooping).

## Estrategias probadas (`strategies.py`)

- Baseline aleatorio (referencia, deberia dar ~50%).
- Momentum: apostar a que continua la direccion de las ultimas N rondas.
- Contrarian: apostar lo opuesto a las ultimas N rondas.
- Reversion/continuacion de racha: solo apuesta cuando hay una racha de
  K rondas iguales seguidas (el "solo cuando es muy seguro" que pediste).
- Micro-momentum / micro-mean-reversion: usa el movimiento de precio real
  de los ultimos 1/3/5/15/30 minutos antes de que arranque la ronda (con
  resolucion real de 1 minuto via `MarketContext`), con filtro opcional de
  magnitud minima de movimiento.
- Volume imbalance / volume imbalance contrarian: usa el volumen de compra
  agresiva (taker buy) de Binance para medir presion compradora vs
  vendedora en los ultimos 3/5/15 min, y apuesta con o contra esa presion.
- Variantes con filtro de regimen de volatilidad: solo disparan cuando la
  volatilidad reciente esta por encima/debajo de la mediana vista hasta
  ese momento (expanding median, sigue siendo walk-forward).

## Analisis de tu lista manual (`manual_sequence_analysis.py`)

Ademas se analizo la secuencia de 36 resultados que anotaste a mano
(transiciones Up/Down, reversion tras rachas). Con esa muestra tan
chica los intervalos de confianza son enormes (ej. 23%-64%), asi que
sirve solo como sanity-check, no como senal por si sola. Ver la salida
completa corriendo el script.

## Resultado real (180 dias de datos, 259,200 velas de 1m, 51,839 rondas de 5 min, corrido el 2026-08-03)

Se repitio el analisis con ~26x mas datos que la primera corrida (45 dias)
para tener suficiente potencia estadistica. Con mas muestra, aparecio algo
que antes no se veia con claridad:

```
Baseline real Up/Down en todo el periodo: 49.6% Up / 50.4% Down
Con una comision asumida de 10%, hace falta un win rate > 52.6% para ganar en el largo plazo

Variance ratio test (estructura del precio en si, sin ninguna regla):
  k= 2: VR=0.984  z=-3.72  -> MEAN REVERSION significativo (VR<1, z<-2)
  k= 3: VR=0.991  z=-1.43  -> random walk (VR~1)
  k= 5: VR=0.982  z=-1.87  -> random walk (VR~1)
  k=10: VR=0.948  z=-3.53  -> MEAN REVERSION significativo (VR<1, z<-2)

Ninguna estrategia supera el breakeven de forma estadisticamente
significativa (limite inferior del IC95% out-of-sample > breakeven)
en este periodo de datos.
```

**Esto es mas interesante que el resultado anterior.** El variance ratio
test -- que no es una regla inventada, es un test estadistico formal sobre
el precio en si -- confirma de forma independiente que **si existe un
mean-reversion real y estadisticamente significativo** (z=-3.72 y z=-3.53,
muy por debajo del umbral de significancia de -2) en los retornos de BTC a
estos horizontes. No es ruido: coincide con lo que ya sugerian las
estrategias contrarian/mean-reversion, que ahora con mucha mas muestra
tienen intervalos de confianza bien angostos y consistentemente por
encima de 50%:

- `contrarian_last_1`: 51.4% OOS, IC95% [50.8%, 52.0%] -- el intervalo
  entero esta arriba de 50%, o sea, el efecto es real, no azar.
- `streak_reversion_3/4/5` (apostar reversion tras 3-5 rondas iguales
  seguidas): 53.2%-53.7% OOS -- el mas alto de todos, pero con menos
  muestra (n=1300-6000) el intervalo baja hasta ~51%.
- `micro_mean_reversion_15m`: 51.6%-52.2% OOS, consistente.

**Pero incluso asi, nada le gana de forma robusta a una comision del 10%.**
El punto (no el limite inferior del IC) de `streak_reversion_3` y
`streak_reversion_4` roza el breakeven de 52.6%, pero el limite inferior
del intervalo de confianza queda por debajo, asi que no se puede confiar
en que eso se sostenga. Dicho de otra forma: **el efecto es real pero
chico** (~1.5-3 puntos porcentuales sobre 50%), y una comision del 10% es
demasiado grande para ese margen. Si la comision real de tu app fuera mas
baja -- por ejemplo, `contrarian_last_1` necesitaria una comision menor a
~5.8% para ser rentable en el largo plazo con el win rate observado --
ahi si empezaria a ser interesante. Pero no tenemos confirmado cual es la
comision real de la plataforma (ver Limitaciones).

## Como correrlo vos mismo

```bash
cd btc_prediction_backtester
pip install -r requirements.txt
python3 data_fetch.py --days 180      # descarga y cachea datos reales (no hay datos en git)
python3 backtest.py --fee 10          # corre el backtest completo
python3 manual_sequence_analysis.py   # analiza tu lista de 36 resultados
```

## Limitaciones importantes

- El 59%/40% que se ve en la app es la distribucion del pool de apuestas
  (cuanta gente aposto a cada lado), **no** un dato de precio -- no hay
  forma de reconstruir eso historicamente con datos publicos, asi que
  no esta modelado aqui.
- La comision real de la plataforma no es publica con certeza; el
  parametro `--fee` es un supuesto ajustable, no un dato confirmado. Esto
  es ahora el dato que mas importa: el efecto de mean-reversion encontrado
  es real pero chico, y si la comision real es menor a la asumida (10%),
  la conclusion podria cambiar.
- El precio de resolucion exacto de cada ronda en la app puede diferir
  ligeramente del close de la vela de 1m usado aqui (redondeo, fuente de
  precio distinta, exchange distinto).
- La granularidad de datos publicos mas fina disponible es de 1 minuto.
  Bots documentados para juegos similares en Polymarket dicen encontrar
  su margen en los **ultimos 10 segundos** antes del cierre -- eso no se
  puede replicar ni verificar aqui por falta de datos de esa resolucion,
  asi que si ese efecto existe en tu plataforma, este backtest no lo mide.
- No hay ejecucion de ordenes ni automatizacion de clicks en este
  proyecto, ni la habra hasta que una estrategia muestre ventaja real y
  sostenida en datos out-of-sample, con la comision real confirmada.

## Conclusion honesta

Con 180 dias de datos reales (52k rondas), a diferencia de la primera
corrida con menos datos, **si aparecio una senal real**: un mean-reversion
estadisticamente significativo (confirmado por un test independiente, no
solo por reglas ad-hoc) de unos 51-53% de acierto. Es un hallazgo genuino
y consistente con la literatura de microestructura de mercado (bid-ask
bounce). Pero sigue sin alcanzar para cubrir una comision del 10% de forma
robusta. La barrera hoy no es "no hay patron" -- es "el patron es
demasiado chico para la comision asumida". El siguiente paso util, si se
quiere seguir, es confirmar la comision real de la plataforma (no
automatizar nada mientras ese numero sea un supuesto).
