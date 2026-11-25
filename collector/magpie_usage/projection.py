"""Four-page display projection, also bundled into the controlled plugin."""

def model_label(value):
    """Fit a single 17px model-name line, including an explicit ellipsis."""
    if not value:
        return "模型：—"
    used = 0
    chars = []
    for char in value:
        width = 17 if ord(char) > 255 else 4 if char in "ilI.,: " else 12 if char in "MW@" else 9
        if used+width > 150:
            return "".join(chars)+"…"
        chars.append(char)
        used += width
    return value


def compact(value):
    if value is None:
        return "—"
    if value >= 1_000_000:
        return "{:.2f}M".format(value / 1_000_000)
    if value >= 1000:
        return "{:.1f}K".format(value / 1000)
    return str(value)


def suffix(value, unit):
    return "—" if value is None else compact(value) + unit


def percent(value):
    return "—" if value is None else "{:.1f}%".format(value / 10)


def duration(value):
    if value is None:
        return "—"
    return "{} ms".format(value) if value < 1000 else "{:.2f} s".format(value / 1000)


def project(snapshot):
    s = snapshot or {}
    success = s.get("success_permille")
    cache = s.get("cache_hit_permille")
    calls = s.get("total_calls")
    quota = s.get("remaining_percent")
    session = s.get("session_count")
    top_calls = s.get("top_model_calls")
    cost = s.get("total_cost_micros")
    result = {
        "updated": "数据更新：" + s.get("updated_at", "—"),
        "pages": [
            {"hero": percent(success), "sub": "网关统计", "ring": round(success / 10) if success is not None else 0,
             "a": suffix(s.get("calls_today"), " 次"), "b": suffix(s.get("errors_today"), " 次"), "c": suffix(quota, "%")},
            {"hero": duration(s.get("latest_latency_ms")), "sub": model_label(s.get("latest_model")), "ring": round(success / 10) if success is not None else 0,
             "a": suffix(s.get("latest_status"), ""), "b": duration(s.get("average_ttft_ms")), "c": suffix(s.get("tokens_today"), "")},
            {"hero": suffix(session, ""), "sub": model_label(s.get("top_model")), "ring": round(top_calls * 100 / calls) if calls and top_calls is not None else 0,
             "a": suffix(calls, " 次"), "b": "—" if cost is None else "${:.2f}".format(cost / 1_000_000), "c": suffix(s.get("tokens_7d"), "")},
            {"hero": percent(cache), "sub": "输入 + 缓存读取", "ring": round(cache / 10) if cache is not None else 0,
             "a": suffix(s.get("total_cache_read"), ""), "b": suffix(s.get("total_input"), ""), "c": suffix(s.get("total_output"), "")},
        ],
    }
    return result
