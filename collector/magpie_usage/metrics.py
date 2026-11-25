"""Aggregate-only projection adapted from Magpie Usage Display (MIT).

Unlike its BLE defaults, unknown fields remain None rather than becoming a
fabricated zero or 100%. Raw requests, credentials and account names are omitted.
"""
from datetime import datetime, timezone
import math
from zoneinfo import ZoneInfo

SHANGHAI = ZoneInfo("Asia/Shanghai")
TOKEN_FIELDS = ("input", "output", "reasoning", "cache_read", "cache_write")


def number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if not math.isfinite(value) or value < 0:
        return None
    return value


def integer(value):
    value = number(value)
    return int(value) if value is not None else None


def rows(value):
    return [row for row in value if isinstance(row, dict)] if isinstance(value, list) else []


def text(value):
    if not isinstance(value, str) or not value:
        return None
    # Device string bindings have a byte budget, not a character budget.
    cleaned = "".join(char for char in value if ord(char) >= 32 and not 127 <= ord(char) <= 159)
    return cleaned.encode("utf-8")[:63].decode("utf-8", errors="ignore") or None


def tokens(bucket):
    values = [integer(bucket.get(key)) for key in TOKEN_FIELDS]
    if all(value is None for value in values):
        return None
    # Omitted optional token categories contribute nothing once token data exists.
    return sum(value or 0 for value in values)


def window_minutes(name):
    name = name.lower() if isinstance(name, str) else ""
    aliases = {"daily": 1440, "weekly": 10080, "monthly": 43200}
    if name in aliases:
        return aliases[name]
    parts = name.split()
    units = {"minute": 1, "hour": 60, "day": 1440, "week": 10080}
    if len(parts) != 2:
        return None
    try:
        amount = int(parts[0])
    except ValueError:
        return None
    unit = units.get(parts[1].rstrip("s"))
    return amount * unit if amount > 0 and unit else None


def reset_seconds(value, at):
    if not isinstance(value, str):
        return None
    try:
        reset = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if reset.tzinfo is None:
            return None
        return max(0, int(reset.timestamp() - at))
    except (ValueError, OverflowError):
        return None


def build_snapshot(quotas, usage, requests, at):
    windows = [window for provider in rows(quotas.get("data"))
               for window in rows(provider.get("windows"))
               if not window.get("unlimited") and number(window.get("remaining")) is not None]
    quota = min(windows, key=lambda window: window["remaining"], default={})
    remaining = number(quota.get("remaining"))
    series = rows(usage.get("series"))
    today = series[-1] if series else {}
    calls, errors = integer(today.get("calls")), integer(today.get("errors"))
    errors = min(calls, errors) if calls is not None and errors is not None else errors
    success = round((calls - errors) * 1000 / calls) if calls and errors is not None else None
    timed, ttft = integer(today.get("timed")), integer(today.get("ttft_ms"))
    latest_rows = rows(requests.get("rows"))
    latest = latest_rows[0] if latest_rows else {}
    status = integer(latest.get("status"))
    input_tokens, cache_tokens = integer(usage.get("input")), integer(usage.get("cache_read"))
    cache_base = input_tokens + cache_tokens if input_tokens is not None and cache_tokens is not None else None
    cache_rate = round(cache_tokens * 1000 / cache_base) if cache_base else None
    models = [model for model in rows(usage.get("models")) if integer(model.get("calls")) is not None]
    top = max(models, key=lambda model: model["calls"], default={})
    cost = number(usage.get("cost"))
    week = [tokens(bucket) for bucket in series[-7:]]
    return {
        "generated_at": int(at),
        "generated_at_ms": int(at * 1000),
        "updated_at": datetime.fromtimestamp(at, timezone.utc).astimezone(SHANGHAI).strftime("%m-%d %H:%M:%S"),
        "utc_offset_minutes": 480,
        "remaining_percent": min(100, round(remaining)) if remaining is not None else None,
        "limit_window_minutes": window_minutes(quota.get("name")),
        "quota_reset_seconds": reset_seconds(quota.get("resetsAt"), at),
        "tokens_today": tokens(today),
        "tokens_7d": sum(value or 0 for value in week) if any(value is not None for value in week) else None,
        "calls_today": calls,
        "errors_today": errors,
        "success_permille": success,
        "average_ttft_ms": ttft // timed if timed and ttft is not None else None,
        "latest_model": text(latest.get("served") or latest.get("model")),
        "latest_status": status,
        "latest_latency_ms": integer(latest.get("ms")),
        "latest_failed": bool(latest.get("err") or latest.get("error") or status < 200 or status >= 400) if status is not None else None,
        "session_count": integer(usage.get("sessionCount")),
        "total_calls": integer(usage.get("calls")),
        "total_input": input_tokens,
        "total_output": integer(usage.get("output")),
        "total_cache_read": cache_tokens,
        "total_reasoning": integer(usage.get("reasoning")),
        "total_cost_micros": round(cost * 1_000_000) if cost is not None else None,
        "cache_hit_permille": cache_rate,
        "top_model": text(top.get("name") or top.get("model")),
        "top_model_calls": integer(top.get("calls")),
    }
