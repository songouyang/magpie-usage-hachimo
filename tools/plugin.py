"""Desktop plugin entry; build_release bundles its imports into one file."""
from magpie_usage.protocol import latest
from magpie_usage.projection import project


def snapshot(data):
    record = latest(data)
    if record is not None:
        value = record[2]
        sample = value.get("snapshot")
        try:
            result = project(sample)
            result["successAt"] = (
                sample.get("generated_at_ms", sample.get("generated_at", 0) * 1000)
                if sample else 0
            )
            result["failed"] = value["failed"]
            return result
        except (ValueError, TypeError, KeyError, OverflowError):
            pass
    result = project(None)
    result.update(successAt=0, failed=False)
    return result
