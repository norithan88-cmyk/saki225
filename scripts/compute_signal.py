#!/usr/bin/env python3
"""
AI 日経225先物研究所 - 本日のAIシグナル 自動計算スクリプト

やっていること（概要）:
  Yahoo Finance の非公式チャートAPI（無料・キー不要）から、日経225先物
  （NIY=F、CME上場・円建て）の価格を取得し、WMAボリンジャーバンド
  （加重移動平均±2σ、傾きを持つ線形回帰チャネルとは別方式）で2つの
  シグナルロジックを計算し、両方を同時に公開する。

  ★なぜNIY=Fか: 大阪取引所の日経225先物そのものには無料で使えるAPIが
    見つからなかった。NIY=Fは同じ日経225指数を原資産とする円建て先物で、
    CME Globexでほぼ24時間取引されており、大阪取引所の日経225先物（夜間
    取引含む）の値動きの性質に近い代理指標として採用している。同じ標準
    サイズの先物同士（取引所が違うだけ）なので、姉妹サイトのミニ225版
    より本来の日経225先物との相性はむしろ良い。大阪取引所そのものとは
    別市場である点に注意。

2つのシグナルロジック（読者が選べるよう両方を同じページに掲載）:
  [freq] 頻度重視版: 1時間・4時間足の一致(WMAボリンジャー、LOOKBACK=15)
         ＋5分足の反発(30pt)。Yahoo Financeの5分足データ制約により
         直近70日分でのみ検証。42件・勝率85.7%・PF3.89・月18件ペース
         （BUY17件PF7.07／SELL25件PF3.18、両方向とも安定してプラス）。
  [long] 長期検証版: 4時間・日足の一致(WMAボリンジャー、LOOKBACK=10)
         ＋1時間足の反発(100pt)。60分足が2年分取得できるため長期検証済み。
         52件・勝率73.1%・PF2.71・月2.1件ペース
         （BUY30件PF2.12／SELL22件PF3.90、両方向とも安定してプラス）。
  どちらも「上位足の方向一致＋下位足の反発確認」という基本設計は共通で、
  時間足の組み合わせと検証期間の長さが異なる。frequency版は頻度が高い分
  検証期間が短く、long版は検証期間が長い分頻度が低い、というトレードオフ。

WMAボリンジャーバンドについて:
  通常のボリンジャーバンド(SMA±2σ)の中心線を、単純移動平均(SMA)ではなく
  加重移動平均(WMA、直近の値ほど重みを大きくする)に変えたもの。バンド幅
  (σ)自体は通常通りの標準偏差を使用。
"""

import json
import os
import statistics
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

YAHOO_CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart/NIY=F"

EDGE_THRESHOLD = 1.3
REVERT_WINDOW = 10

VARIANTS = {
    "freq": {
        "label": "頻度重視版（1時間・4時間一致＋5分反発）",
        "align_tfs": ("h1", "h4"),
        "trigger_tf": "m5",
        "lookback": 15,
        "revert_min_pt": 30.0,
        "sl_buffer_pt": 30.0,
        "backtest_note": "70日間・42件・勝率85.7%・PF3.89・月18件ペース（BUY17件PF7.07／SELL25件PF3.18）",
    },
    "long": {
        "label": "長期検証版（4時間・日足一致＋1時間反発）",
        "align_tfs": ("h4", "d1"),
        "trigger_tf": "h1",
        "lookback": 10,
        "revert_min_pt": 100.0,
        "sl_buffer_pt": 100.0,
        "backtest_note": "2年間・52件・勝率73.1%・PF2.71・月2.1件ペース（BUY30件PF2.12／SELL22件PF3.90）",
    },
}

TF_LABEL_JA = {"m5": "5分足", "h1": "1時間足", "h4": "4時間足", "d1": "日足"}


def http_get_json(url, retries=3, wait_sec=5):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    last_err = None
    for _ in range(retries):
        try:
            with urllib.request.urlopen(req, timeout=30) as res:
                return json.loads(res.read().decode("utf-8"))
        except (urllib.error.URLError, TimeoutError) as e:
            last_err = e
            time.sleep(wait_sec)
    raise RuntimeError(f"取得に失敗しました: {url} ({last_err})")


def fetch_niy_bars(interval, range_):
    url = f"{YAHOO_CHART_URL}?interval={interval}&range={range_}"
    data = http_get_json(url)
    result = data.get("chart", {}).get("result")
    if not result:
        raise RuntimeError(f"日経225先物データが取得できませんでした: {data}")
    ts = result[0].get("timestamp") or []
    quote = result[0]["indicators"]["quote"][0]
    bars = []
    for i, t in enumerate(ts):
        o, h, l, c = quote["open"][i], quote["high"][i], quote["low"][i], quote["close"][i]
        if o is None or h is None or l is None or c is None:
            continue
        bars.append({"t": t, "o": float(o), "h": float(h), "l": float(l), "c": float(c)})
    if not bars:
        raise RuntimeError(f"日経225先物データが空でした（interval={interval}, range={range_}）")
    return bars


def aggregate_to_4h(hourly_bars):
    buckets = {}
    order = []
    for b in hourly_bars:
        dt = datetime.fromtimestamp(b["t"], tz=timezone.utc)
        key = (dt.toordinal() * 24 + dt.hour) // 4
        if key not in buckets:
            buckets[key] = {"t": b["t"], "o": b["o"], "h": b["h"], "l": b["l"], "c": b["c"]}
            order.append(key)
        else:
            buckets[key]["h"] = max(buckets[key]["h"], b["h"])
            buckets[key]["l"] = min(buckets[key]["l"], b["l"])
            buckets[key]["c"] = b["c"]
    return [buckets[k] for k in order]


def wma(series):
    n = len(series)
    weights = list(range(1, n + 1))
    wsum = sum(weights)
    return sum(w * v for w, v in zip(weights, series)) / wsum


def wma_bollinger_channel(closes, lookback):
    series = closes[-lookback:] if len(closes) > lookback else closes[:]
    n = len(series)
    if n < 5:
        raise RuntimeError("WMAボリンジャーバンド計算に必要なデータ本数が不足しています")
    mid = wma(series)
    sigma = statistics.pstdev(series)
    sigma = sigma if sigma > 1e-6 else 1e-6
    upper = mid + 2 * sigma
    lower = mid - 2 * sigma
    latest = series[-1]
    position = (latest - mid) / sigma
    return {"mid": mid, "upper": upper, "lower": lower, "sigma": sigma, "position": position, "latest": latest}


MOMENTUM_LABEL_JA = {"UP": "上方向", "DOWN": "下方向", "FLAT": "中央"}


def momentum_direction(ch):
    pos = ch["position"]
    if pos >= EDGE_THRESHOLD:
        return "UP"
    if pos <= -EDGE_THRESHOLD:
        return "DOWN"
    return "FLAT"


def detect_reversal_setup(bars, ch, direction, revert_min_pt):
    if len(bars) < REVERT_WINDOW:
        return None
    recent = bars[-REVERT_WINDOW:]
    closes = [b["c"] for b in recent]
    latest = closes[-1]
    sigma, mid = ch["sigma"], ch["mid"]
    if direction == "BUY":
        trough_idx = min(range(len(closes)), key=lambda i: closes[i])
        trough = closes[trough_idx]
        if trough_idx == len(closes) - 1:
            return None
        if (trough - mid) / sigma > -EDGE_THRESHOLD:
            return None
        if (latest - trough) < revert_min_pt:
            return None
        return trough
    else:
        peak_idx = max(range(len(closes)), key=lambda i: closes[i])
        peak = closes[peak_idx]
        if peak_idx == len(closes) - 1:
            return None
        if (peak - mid) / sigma < EDGE_THRESHOLD:
            return None
        if (peak - latest) < revert_min_pt:
            return None
        return peak


def build_variant_signal(key, cfg, bars_by_tf):
    align_a, align_b = cfg["align_tfs"]
    trigger_tf = cfg["trigger_tf"]
    lookback = cfg["lookback"]

    ch_a = wma_bollinger_channel([b["c"] for b in bars_by_tf[align_a]], lookback)
    ch_b = wma_bollinger_channel([b["c"] for b in bars_by_tf[align_b]], lookback)
    timeframes = [
        {"label": TF_LABEL_JA[align_a], "key": align_a, "channel": ch_a},
        {"label": TF_LABEL_JA[align_b], "key": align_b, "channel": ch_b},
    ]
    for tf in timeframes:
        tf["momentum"] = momentum_direction(tf["channel"])

    dirs = [tf["momentum"] for tf in timeframes]
    if dirs[0] == "UP" and dirs[1] == "UP":
        candidate = "BUY"
    elif dirs[0] == "DOWN" and dirs[1] == "DOWN":
        candidate = "SELL"
    else:
        candidate = None

    trig_bars = bars_by_tf[trigger_tf]
    ch_trig = wma_bollinger_channel([b["c"] for b in trig_bars], lookback)
    extreme = detect_reversal_setup(trig_bars, ch_trig, candidate, cfg["revert_min_pt"]) if candidate else None
    bias = candidate if (candidate and extreme is not None) else "WAIT"

    if bias in ("SELL", "BUY"):
        avg_abs_pos = sum(abs(tf["channel"]["position"]) for tf in timeframes) / len(timeframes)
        confidence = 50 + 30 + min(avg_abs_pos, 3.0) * 5
        confidence = max(50, min(95, round(confidence)))
        stars = max(1, min(5, round(confidence / 20)))
    else:
        confidence = 50
        stars = 2

    latest_price = trig_bars[-1]["c"]
    trig_label = TF_LABEL_JA[trigger_tf]
    align_label = f"{TF_LABEL_JA[align_a]}・{TF_LABEL_JA[align_b]}"

    if bias == "SELL":
        entry = latest_price
        move = abs(entry - extreme)
        sl = extreme + cfg["sl_buffer_pt"]
        tp = entry - move
        trade_lead = f"戻り売り ― {align_label}の下降方向一致＋{trig_label}の戻りからの反落"
    elif bias == "BUY":
        entry = latest_price
        move = abs(entry - extreme)
        sl = extreme - cfg["sl_buffer_pt"]
        tp = entry + move
        trade_lead = f"押し目買い ― {align_label}の上昇方向一致＋{trig_label}の押し目からの反発"
    else:
        entry = tp = sl = None
        if candidate == "SELL":
            trade_lead = f"様子見 ― {align_label}は戻り売り方向で一致、{trig_label}の反落シグナル待ち"
        elif candidate == "BUY":
            trade_lead = f"様子見 ― {align_label}は押し目買い方向で一致、{trig_label}の反発シグナル待ち"
        else:
            trade_lead = f"様子見 ― {align_label}の方向が一致していない"

    reversal_setup = None
    if bias in ("SELL", "BUY"):
        reverted = round((entry - extreme), 1) if bias == "BUY" else round((extreme - entry), 1)
        reversal_setup = {"extreme": round(extreme, 1), "reverted_pt": reverted}

    return {
        "key": key,
        "label": cfg["label"],
        "backtest_note": cfg["backtest_note"],
        "latest_price": round(latest_price, 1),
        "signal": {
            "bias": bias,
            "bias_label": {"SELL": "戻り売り優勢", "BUY": "押し目買い優勢", "WAIT": "方向感なし"}[bias],
            "stars": stars,
            "confidence": confidence,
        },
        "priority_trade": {
            "lead": trade_lead,
            "entry": round(entry, 1) if entry is not None else None,
            "take_profit": round(tp, 1) if tp is not None else None,
            "stop_loss": round(sl, 1) if sl is not None else None,
        },
        "reversal_setup": reversal_setup,
        "channels": [
            {
                "key": tf["key"], "label": tf["label"],
                "position_sigma": round(tf["channel"]["position"], 2),
                "momentum": tf["momentum"],
                "mid": round(tf["channel"]["mid"], 1),
                "upper": round(tf["channel"]["upper"], 1),
                "lower": round(tf["channel"]["lower"], 1),
            }
            for tf in timeframes
        ],
    }


def load_trade_log(base_dir):
    path = os.path.join(base_dir, "trade_log.json")
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data.get("variants"), dict):
            raise ValueError("形式不正")
        return data
    except (FileNotFoundError, json.JSONDecodeError, OSError, ValueError, AttributeError):
        return {"variants": {k: {"trades": []} for k in VARIANTS}}


def pnl_pt_for(bias, entry, price):
    diff = (entry - price) if bias == "SELL" else (price - entry)
    return round(diff, 1)


def update_one_variant(bucket, bias, priority_trade, latest_price, confidence, now_iso):
    trades = bucket.get("trades", [])
    open_trade = trades[-1] if trades and trades[-1].get("status") == "OPEN" else None

    if open_trade is not None:
        ob = open_trade["bias"]
        tp, sl = open_trade["take_profit"], open_trade["stop_loss"]
        hit_tp = (latest_price <= tp) if ob == "SELL" else (latest_price >= tp)
        hit_sl = (latest_price >= sl) if ob == "SELL" else (latest_price <= sl)
        if hit_tp or hit_sl:
            open_trade["status"] = "WIN" if hit_tp else "LOSS"
            open_trade["closed_at_utc"] = now_iso
            open_trade["closed_price"] = round(latest_price, 1)
            open_trade["pnl_pt"] = pnl_pt_for(ob, open_trade["entry"], latest_price)
            open_trade = None

    if open_trade is None and bias in ("SELL", "BUY"):
        entry, tp, sl = priority_trade.get("entry"), priority_trade.get("take_profit"), priority_trade.get("stop_loss")
        if entry is not None and tp is not None and sl is not None:
            trades.append({
                "id": now_iso, "opened_at_utc": now_iso, "bias": bias,
                "entry": entry, "take_profit": tp, "stop_loss": sl, "confidence": confidence,
                "status": "OPEN", "closed_at_utc": None, "closed_price": None, "pnl_pt": None,
            })

    bucket["trades"] = trades
    bucket["stats"] = compute_trade_stats(trades)
    return bucket


def compute_trade_stats(trades):
    closed = [t for t in trades if t.get("status") in ("WIN", "LOSS")]
    wins = [t for t in closed if t["status"] == "WIN"]
    losses = [t for t in closed if t["status"] == "LOSS"]
    total_closed = len(closed)
    gross_win = sum(t["pnl_pt"] for t in wins)
    gross_loss = abs(sum(t["pnl_pt"] for t in losses))
    return {
        "total_closed": total_closed,
        "wins": len(wins),
        "losses": len(losses),
        "win_rate_pct": round(len(wins) / total_closed * 100, 1) if total_closed else None,
        "avg_win_pt": round(gross_win / len(wins), 1) if wins else None,
        "avg_loss_pt": round(-gross_loss / len(losses), 1) if losses else None,
        "profit_factor": round(gross_win / gross_loss, 2) if gross_loss > 0 else None,
        "total_pt": round(sum(t["pnl_pt"] for t in closed), 1) if closed else 0.0,
    }


def build_signal(out_path=None):
    now = datetime.now(timezone.utc)

    m5 = fetch_niy_bars("5m", "60d")
    h1 = fetch_niy_bars("60m", "2y")
    d1 = fetch_niy_bars("1d", "5y")
    h4 = aggregate_to_4h(h1)
    bars_by_tf = {"m5": m5, "h1": h1, "h4": h4, "d1": d1}

    variants_result = {key: build_variant_signal(key, cfg, bars_by_tf) for key, cfg in VARIANTS.items()}

    latest_price = m5[-1]["c"] if m5 else h1[-1]["c"]

    result = {
        "generated_at_utc": now.isoformat(),
        "pair": "日経225先物（NIY=F、CME円建て、大阪取引所の日経225先物の代理指標）",
        "latest_price": round(latest_price, 1),
        "variants": variants_result,
        "disclaimer": (
            "本データはルールベースの参考情報であり、投資成果を保証するものではありません。"
            "実際の大阪取引所の日経225先物ではなく、同じ日経225指数を原資産とする"
            "CME上場・円建て日経225先物(NIY=F)を代理指標として使用しています。"
            "頻度重視版は検証期間が70日間と短く、長期検証版は2年間検証済みですが"
            "月2件程度と頻度が低い、というトレードオフがあります。"
        ),
    }

    if out_path:
        base_dir = os.path.dirname(out_path)
        try:
            trade_log = load_trade_log(base_dir)
            variants_log = trade_log.setdefault("variants", {})
            for key in VARIANTS:
                bucket = variants_log.setdefault(key, {"trades": []})
                v = variants_result[key]
                variants_log[key] = update_one_variant(
                    bucket, v["signal"]["bias"], v["priority_trade"], latest_price,
                    v["signal"]["confidence"], now.isoformat(),
                )
            trade_log["updated_at_utc"] = now.isoformat()
            with open(os.path.join(base_dir, "trade_log.json"), "w", encoding="utf-8") as f:
                json.dump(trade_log, f, ensure_ascii=False, indent=2)
        except Exception as e:  # noqa: BLE001
            print(f"[WARN] trade_log.jsonの更新に失敗しました（シグナル本体は継続します）: {e}", file=sys.stderr)

    return result


def main():
    out_path = os.path.join(os.path.dirname(__file__), "..", "signal.json")
    out_path = os.path.abspath(out_path)
    try:
        signal = build_signal(out_path=out_path)
    except Exception as e:  # noqa: BLE001
        print(f"[ERROR] シグナル計算に失敗しました: {e}", file=sys.stderr)
        sys.exit(1)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(signal, f, ensure_ascii=False, indent=2)
    print(f"書き出し完了: {out_path}")
    print(json.dumps(signal, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
