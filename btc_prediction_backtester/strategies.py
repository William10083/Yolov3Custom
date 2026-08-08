"""Prediction strategies for the 5-minute Up/Down game.

Every strategy receives:
  - history: list of past round outcomes so far, e.g. ["Up", "Down", ...]
  - ctx: a MarketContext giving read-only access to 1-MINUTE candle data
    strictly BEFORE the current round's start (no lookahead)
  - start_ms: the current round's start timestamp (ms)

and returns "Up" / "Down" (a signal) or None ("no confident signal, skip
this round" -- this is what lets a strategy only fire on rounds it
considers "safe").
"""
import statistics


class MarketContext:
    """Read-only lookup over 1-minute candles, indexed by open_time_ms."""

    def __init__(self, candles):
        self.by_time = {c["open_time_ms"]: c for c in candles}

    def candle_at(self, ms):
        return self.by_time.get(ms)

    def close_minutes_before(self, ref_ms, minutes_back):
        """Price exactly `minutes_back` minutes before ref_ms, or None.

        Uses the OPEN of the candle at that exact timestamp -- the open of
        the candle at time T is the price AT T (matching how backtest.py
        defines round boundaries). Using that candle's CLOSE instead would
        leak ~1 minute of future price into minutes_back=0 (the candle's
        close is the price one minute later, at T+60s).
        """
        c = self.by_time.get(ref_ms - minutes_back * 60_000)
        return c["open"] if c else None

    def window(self, ref_ms, minutes):
        """List of candles in [ref_ms - minutes*60s, ref_ms), in order."""
        out = []
        for m in range(minutes, 0, -1):
            c = self.by_time.get(ref_ms - m * 60_000)
            if c is not None:
                out.append(c)
        return out

    def pct_move(self, ref_ms, minutes):
        start = self.close_minutes_before(ref_ms, minutes)
        end = self.close_minutes_before(ref_ms, 0)
        if start is None or end is None or start == 0:
            return None
        return (end - start) / start * 100

    def taker_buy_ratio(self, ref_ms, minutes):
        """Fraction of traded volume that was taker-BUY over the last `minutes`
        minutes before ref_ms. >0.5 means more aggressive buying than selling."""
        win = self.window(ref_ms, minutes)
        vol = sum(c["volume"] for c in win)
        buy = sum(c["taker_buy_base"] for c in win)
        if vol <= 0:
            return None
        return buy / vol

    def realized_vol(self, ref_ms, minutes):
        """Stdev of 1-minute log-ish returns over the last `minutes` minutes."""
        win = self.window(ref_ms, minutes)
        if len(win) < 3:
            return None
        closes = [c["open"] for c in win]
        rets = [(closes[i] - closes[i - 1]) / closes[i - 1] for i in range(1, len(closes)) if closes[i - 1]]
        if len(rets) < 2:
            return None
        return statistics.pstdev(rets)

    # --- what a trader actually eyeballs on the previous round's candle ---

    def close_position(self, ref_ms, minutes=5):
        """Where the last `minutes` closed inside their own high-low range,
        as 0..1 (0 = closed at the very low, 1 = at the very high).

        Closing pinned at an extreme is the classic "ran out of buyers"
        read; closing mid-range says the move had no conviction either way.
        """
        win = self.window(ref_ms, minutes)
        if not win:
            return None
        high = max(c["high"] for c in win)
        low = min(c["low"] for c in win)
        end = self.close_minutes_before(ref_ms, 0)
        if end is None or high <= low:
            return None
        return (end - low) / (high - low)

    def path_efficiency(self, ref_ms, minutes=5):
        """|net move| / (sum of per-minute absolute moves), 0..1.

        Near 1 means a clean directional push; near 0 means the price
        thrashed back and forth and ended up nowhere. Same net move means
        something very different depending on which of these it was.
        """
        win = self.window(ref_ms, minutes)
        if len(win) < 2:
            return None
        opens = [c["open"] for c in win] + [self.close_minutes_before(ref_ms, 0)]
        if opens[-1] is None:
            return None
        net = abs(opens[-1] - opens[0])
        travel = sum(abs(opens[i] - opens[i - 1]) for i in range(1, len(opens)))
        return None if travel == 0 else net / travel

    def last_minute_reversal(self, ref_ms, minutes=5):
        """True when the final minute moved against the whole window's net
        direction -- a late turn right at the boundary."""
        net = self.pct_move(ref_ms, minutes)
        last = self.pct_move(ref_ms, 1)
        if net is None or last is None or net == 0 or last == 0:
            return None
        return (net > 0) != (last > 0)

    def range_pct(self, ref_ms, minutes=5):
        """High-low range of the last `minutes`, as % of price -- the plain
        'how wild was it' measure."""
        win = self.window(ref_ms, minutes)
        if not win:
            return None
        high = max(c["high"] for c in win)
        low = min(c["low"] for c in win)
        return None if low <= 0 else (high - low) / low * 100

    def consecutive_direction(self, ref_ms, max_look=6):
        """(direction, count) of the run of same-direction 1-minute candles
        ending right before ref_ms. direction is +1 up, -1 down."""
        opens = []
        for m in range(max_look, -1, -1):
            p = self.close_minutes_before(ref_ms, m)
            if p is None:
                return None
            opens.append(p)
        moves = [opens[i] - opens[i - 1] for i in range(1, len(opens))]
        if not moves or moves[-1] == 0:
            return None
        direction = 1 if moves[-1] > 0 else -1
        count = 0
        for mv in reversed(moves):
            if (mv > 0 and direction == 1) or (mv < 0 and direction == -1):
                count += 1
            else:
                break
        return direction, count

    def distance_from_mean(self, ref_ms, minutes=20):
        """How far price sits from its own recent average, in units of that
        window's stdev -- a z-score of 'stretched vs normal'."""
        win = self.window(ref_ms, minutes)
        if len(win) < 5:
            return None
        opens = [c["open"] for c in win]
        end = self.close_minutes_before(ref_ms, 0)
        if end is None:
            return None
        mean = statistics.fmean(opens)
        sd = statistics.pstdev(opens)
        return None if sd == 0 else (end - mean) / sd


def random_baseline(history, ctx, start_ms):
    import random

    return random.choice(["Up", "Down"])


def momentum_last_n(n):
    def strategy(history, ctx, start_ms):
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
    def strategy(history, ctx, start_ms):
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
    def strategy(history, ctx, start_ms):
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
    def strategy(history, ctx, start_ms):
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
    """Predict continuation of the REAL price trend over the last `minutes`
    minutes (1-minute-candle resolution), before the round starts."""

    def strategy(history, ctx, start_ms):
        pct = ctx.pct_move(start_ms, minutes)
        if pct is None or abs(pct) < min_move_pct:
            return None
        return "Up" if pct > 0 else "Down"

    strategy.__name__ = f"micro_momentum_{minutes}m_min{min_move_pct}"
    return strategy


def micro_mean_reversion(minutes, min_move_pct=0.0):
    def strategy(history, ctx, start_ms):
        pct = ctx.pct_move(start_ms, minutes)
        if pct is None or abs(pct) < min_move_pct:
            return None
        return "Down" if pct > 0 else "Up"

    strategy.__name__ = f"micro_mean_reversion_{minutes}m_min{min_move_pct}"
    return strategy


def volume_imbalance(minutes, threshold):
    """Bet with the side that has been aggressively buying/selling.

    threshold e.g. 0.55 means: only fire if taker-buy ratio is above 0.55
    (buy pressure) or below 0.45 (sell pressure) over the window.
    """

    def strategy(history, ctx, start_ms):
        ratio = ctx.taker_buy_ratio(start_ms, minutes)
        if ratio is None:
            return None
        if ratio >= threshold:
            return "Up"
        if ratio <= (1 - threshold):
            return "Down"
        return None

    strategy.__name__ = f"volume_imbalance_{minutes}m_t{threshold}"
    return strategy


def volume_imbalance_contrarian(minutes, threshold):
    """Opposite bet: fade strong one-sided taker flow (absorption hypothesis)."""

    def strategy(history, ctx, start_ms):
        ratio = ctx.taker_buy_ratio(start_ms, minutes)
        if ratio is None:
            return None
        if ratio >= threshold:
            return "Down"
        if ratio <= (1 - threshold):
            return "Up"
        return None

    strategy.__name__ = f"volume_imbalance_contrarian_{minutes}m_t{threshold}"
    return strategy


def close_position_reversion(threshold, minutes=5):
    """Fade a round that closed pinned near one end of its own range.

    Trader's read: a close jammed against the high means the push spent
    itself getting there. Only fires at the extremes -- a mid-range close
    carries no information worth acting on.
    """

    def strategy(history, ctx, start_ms):
        pos = ctx.close_position(start_ms, minutes)
        if pos is None:
            return None
        if pos >= threshold:
            return "Down"
        if pos <= 1 - threshold:
            return "Up"
        return None

    strategy.__name__ = f"close_position_reversion_{minutes}m_t{threshold}"
    return strategy


def close_position_continuation(threshold, minutes=5):
    """The opposite read of the same fact: closing on the high is strength
    that carries. Included so the data decides which story is true."""

    def strategy(history, ctx, start_ms):
        pos = ctx.close_position(start_ms, minutes)
        if pos is None:
            return None
        if pos >= threshold:
            return "Up"
        if pos <= 1 - threshold:
            return "Down"
        return None

    strategy.__name__ = f"close_position_continuation_{minutes}m_t{threshold}"
    return strategy


def efficient_move_reversion(min_efficiency, min_move_pct, minutes=5):
    """Fade only CLEAN directional moves -- high path efficiency plus real
    size. Filters out the chop that a plain move-size rule would catch."""

    def strategy(history, ctx, start_ms):
        eff = ctx.path_efficiency(start_ms, minutes)
        pct = ctx.pct_move(start_ms, minutes)
        if eff is None or pct is None:
            return None
        if eff < min_efficiency or abs(pct) < min_move_pct:
            return None
        return "Down" if pct > 0 else "Up"

    strategy.__name__ = f"efficient_move_reversion_{minutes}m_e{min_efficiency}_m{min_move_pct}"
    return strategy


def choppy_move_reversion(max_efficiency, min_move_pct, minutes=5):
    """Mirror of the above: fade moves that got there by thrashing. If
    reversion is really about exhaustion, chop should revert harder."""

    def strategy(history, ctx, start_ms):
        eff = ctx.path_efficiency(start_ms, minutes)
        pct = ctx.pct_move(start_ms, minutes)
        if eff is None or pct is None:
            return None
        if eff > max_efficiency or abs(pct) < min_move_pct:
            return None
        return "Down" if pct > 0 else "Up"

    strategy.__name__ = f"choppy_move_reversion_{minutes}m_e{max_efficiency}_m{min_move_pct}"
    return strategy


def last_minute_reversal_follow(minutes=5):
    """When the final minute turned against the round's net direction, bet
    that the turn is the real signal and carries into the next round."""

    def strategy(history, ctx, start_ms):
        reversed_late = ctx.last_minute_reversal(start_ms, minutes)
        last = ctx.pct_move(start_ms, 1)
        if not reversed_late or last is None:
            return None
        return "Up" if last > 0 else "Down"

    strategy.__name__ = f"last_minute_reversal_follow_{minutes}m"
    return strategy


def consecutive_candle_reversion(min_run):
    """Fade a run of `min_run`+ same-direction 1-minute candles -- the
    minute-level analogue of the round-level streak signal."""

    def strategy(history, ctx, start_ms):
        res = ctx.consecutive_direction(start_ms)
        if res is None:
            return None
        direction, count = res
        if count < min_run:
            return None
        return "Down" if direction == 1 else "Up"

    strategy.__name__ = f"consecutive_candle_reversion_{min_run}"
    return strategy


def stretched_from_mean_reversion(min_z, minutes=20):
    """Fade price that has stretched `min_z` stdevs from its own recent
    average -- the textbook overextension entry."""

    def strategy(history, ctx, start_ms):
        z = ctx.distance_from_mean(start_ms, minutes)
        if z is None or abs(z) < min_z:
            return None
        return "Down" if z > 0 else "Up"

    strategy.__name__ = f"stretched_from_mean_reversion_{minutes}m_z{min_z}"
    return strategy


def wide_range_reversion(min_range_pct, minutes=5):
    """Fade the direction only after an unusually WIDE round -- volatility
    expansion as the trigger rather than the move itself."""

    def strategy(history, ctx, start_ms):
        rng = ctx.range_pct(start_ms, minutes)
        pct = ctx.pct_move(start_ms, minutes)
        if rng is None or pct is None or rng < min_range_pct or pct == 0:
            return None
        return "Down" if pct > 0 else "Up"

    strategy.__name__ = f"wide_range_reversion_{minutes}m_r{min_range_pct}"
    return strategy


class _RunningMedian:
    """Two-heap running median: O(log n) insert, O(1) query."""

    def __init__(self):
        self._lo = []  # max-heap (negated values): lower half
        self._hi = []  # min-heap: upper half

    def add(self, x):
        import heapq

        if not self._lo or x <= -self._lo[0]:
            heapq.heappush(self._lo, -x)
        else:
            heapq.heappush(self._hi, x)
        if len(self._lo) > len(self._hi) + 1:
            heapq.heappush(self._hi, -heapq.heappop(self._lo))
        elif len(self._hi) > len(self._lo):
            heapq.heappush(self._lo, -heapq.heappop(self._hi))

    def median(self):
        if not self._lo:
            return None
        if len(self._lo) > len(self._hi):
            return -self._lo[0]
        return (-self._lo[0] + self._hi[0]) / 2

    def __len__(self):
        return len(self._lo) + len(self._hi)


def vol_regime_gated(base_strategy, minutes, vol_lookback, mode):
    """Wrap base_strategy so it only fires during a volatility regime.

    mode="high": only fire when recent realized vol is above the running
    median seen so far. mode="low": only fire when below.
    NOTE: uses an expanding median of vol seen up to now, so it's still
    walk-forward (no lookahead into the future).
    """
    running = _RunningMedian()

    def strategy(history, ctx, start_ms):
        vol = ctx.realized_vol(start_ms, vol_lookback)
        if vol is None:
            return None
        if len(running) < 20:
            running.add(vol)
            return None
        median = running.median()
        running.add(vol)
        is_high = vol > median
        if (mode == "high" and not is_high) or (mode == "low" and is_high):
            return None
        return base_strategy(history, ctx, start_ms)

    strategy.__name__ = f"{base_strategy.__name__}_volregime_{mode}{vol_lookback}"
    return strategy


def build_default_strategies():
    strategies = [random_baseline]
    for n in (1, 2, 3, 5):
        strategies.append(momentum_last_n(n))
        strategies.append(contrarian_last_n(n))
    for k in (2, 3, 4, 5):
        strategies.append(streak_reversion(k))
        strategies.append(streak_continuation(k))
    for m in (1, 3, 5, 15, 30):
        strategies.append(micro_momentum(m))
        strategies.append(micro_momentum(m, min_move_pct=0.05))
        strategies.append(micro_mean_reversion(m))
        strategies.append(micro_mean_reversion(m, min_move_pct=0.05))
    for m in (3, 5, 15):
        for t in (0.55, 0.6, 0.65):
            strategies.append(volume_imbalance(m, t))
            strategies.append(volume_imbalance_contrarian(m, t))
    # volatility-regime gated variants of the two most theoretically-motivated
    # base strategies (contrarian tends to work post-volatility-spike; momentum
    # tends to work in calm trending regimes)
    strategies.append(vol_regime_gated(contrarian_last_n(1), 5, 30, "high"))
    strategies.append(vol_regime_gated(momentum_last_n(1), 5, 30, "low"))
    strategies.append(vol_regime_gated(micro_mean_reversion(5), 5, 30, "high"))
    strategies.append(vol_regime_gated(micro_momentum(5), 5, 30, "low"))

    # Candle-shape / path-quality reads -- the things a trader eyeballs on the
    # previous round rather than just its net direction. Reversion AND
    # continuation variants of the same fact are both included so the data
    # decides which story holds, instead of only testing the one we expect.
    for t in (0.8, 0.9):
        strategies.append(close_position_reversion(t))
        strategies.append(close_position_continuation(t))
    for eff in (0.6, 0.75):
        strategies.append(efficient_move_reversion(eff, 0.05))
    for eff in (0.3, 0.45):
        strategies.append(choppy_move_reversion(eff, 0.05))
    strategies.append(last_minute_reversal_follow())
    for run in (3, 4, 5):
        strategies.append(consecutive_candle_reversion(run))
    for z in (1.0, 1.5, 2.0):
        strategies.append(stretched_from_mean_reversion(z))
    for r in (0.1, 0.15, 0.2):
        strategies.append(wide_range_reversion(r))
    return strategies
