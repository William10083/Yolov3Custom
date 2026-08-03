"""Prediction strategies for the 5-minute Up/Down game.

Every strategy receives only information available strictly BEFORE the
round's start time (no lookahead) and returns either:
  - "Up" / "Down": a signal
  - None: "no confident signal, skip this round" (this is what lets a
    strategy only fire on the rounds it considers "safe")

`history` is the list of past round outcomes so far: ["Up", "Down", ...]
`closes` is the list of 1-minute closes strictly before the round start
(most recent last), used for micro-momentum style strategies.
"""


def random_baseline(history, closes):
    import random

    return random.choice(["Up", "Down"])


def momentum_last_n(n):
    def strategy(history, closes):
        if len(history) < n:
            return None
        window = history[-n:]
        ups = window.count("Up")
        downs = window.count("Down")
        if ups == downs:
            return None
        return "Up" if ups > downs else "Down"

    strategy.__name__ = f"momentum_last_{n}"
    return strategy


def contrarian_last_n(n):
    def strategy(history, closes):
        if len(history) < n:
            return None
        window = history[-n:]
        ups = window.count("Up")
        downs = window.count("Down")
        if ups == downs:
            return None
        return "Down" if ups > downs else "Up"

    strategy.__name__ = f"contrarian_last_{n}"
    return strategy


def streak_reversion(k):
    """Only bets when the last k rounds were all the same direction; bets the opposite."""

    def strategy(history, closes):
        if len(history) < k:
            return None
        window = history[-k:]
        if all(x == "Down" for x in window):
            return "Up"
        if all(x == "Up" for x in window):
            return "Down"
        return None

    strategy.__name__ = f"streak_reversion_{k}"
    return strategy


def streak_continuation(k):
    """Only bets when the last k rounds were all the same direction; bets the same way."""

    def strategy(history, closes):
        if len(history) < k:
            return None
        window = history[-k:]
        if all(x == "Down" for x in window):
            return "Down"
        if all(x == "Up" for x in window):
            return "Up"
        return None

    strategy.__name__ = f"streak_continuation_{k}"
    return strategy


def micro_momentum(minutes, min_move_pct=0.0):
    """Predict continuation of the price trend over the last `minutes` minutes.

    min_move_pct: only fire if the absolute % move over the window is at
    least this large (a confidence filter -- bigger recent move = more
    conviction). 0.0 means always fire.
    """

    def strategy(history, closes):
        if len(closes) < minutes + 1:
            return None
        start = closes[-(minutes + 1)]
        end = closes[-1]
        if start == 0:
            return None
        pct = (end - start) / start * 100
        if abs(pct) < min_move_pct:
            return None
        return "Up" if pct > 0 else "Down"

    strategy.__name__ = f"micro_momentum_{minutes}m_min{min_move_pct}"
    return strategy


def micro_mean_reversion(minutes, min_move_pct=0.0):
    def strategy(history, closes):
        if len(closes) < minutes + 1:
            return None
        start = closes[-(minutes + 1)]
        end = closes[-1]
        if start == 0:
            return None
        pct = (end - start) / start * 100
        if abs(pct) < min_move_pct:
            return None
        return "Down" if pct > 0 else "Up"

    strategy.__name__ = f"micro_mean_reversion_{minutes}m_min{min_move_pct}"
    return strategy


def build_default_strategies():
    strategies = [random_baseline]
    for n in (1, 2, 3, 5):
        strategies.append(momentum_last_n(n))
        strategies.append(contrarian_last_n(n))
    for k in (2, 3, 4, 5):
        strategies.append(streak_reversion(k))
        strategies.append(streak_continuation(k))
    for m in (1, 3, 5):
        strategies.append(micro_momentum(m))
        strategies.append(micro_momentum(m, min_move_pct=0.05))
        strategies.append(micro_mean_reversion(m))
        strategies.append(micro_mean_reversion(m, min_move_pct=0.05))
    return strategies
