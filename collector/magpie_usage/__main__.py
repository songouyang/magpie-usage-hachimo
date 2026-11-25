"""15-second local collector; Pet Manager reads its file using the Python plugin."""
import argparse
import fcntl
import json
import os
from pathlib import Path
import sys
import tempfile
import time

from .client import MagpieClient, MagpieError
from .state import CollectionState
from .feed import FileFeedWriter, safe_path, read_json


def save(path, value):
    path = safe_path(path)
    fd, tmp = tempfile.mkstemp(prefix=".magpie-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as out:
            json.dump(value, out, ensure_ascii=False, allow_nan=False)
            out.write("\n")
            out.flush()
            os.fsync(out.fileno())
        os.replace(tmp, path)
    finally:
        Path(tmp).unlink(missing_ok=True)


def main():
    base = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description="Magpie 独立采集器 → 数据文件 → Pet Manager Python 插件")
    parser.add_argument("--magpie-url", default="http://127.0.0.1:3425")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--local-only", action="store_true", help="仅检查采集，不更新插件数据文件")
    parser.add_argument("--output", type=Path, help="可选本机诊断快照；父目录须已存在")
    parser.add_argument("--feed-file", type=Path, default=base/"data/magpie-usage.txt")
    parser.add_argument("--cache-file", type=Path, default=base/".last-success.json")
    args = parser.parse_args()
    guard = writer = None
    try:
        client = MagpieClient(args.magpie_url)
        args.cache_file = safe_path(args.cache_file)
        args.feed_file = safe_path(args.feed_file)
        if args.output:
            args.output = safe_path(args.output)
        lock = safe_path(args.cache_file.with_name(".collector.lock"))
        targets = [args.cache_file.resolve(), args.feed_file.resolve(), lock.resolve()]
        if args.output:
            targets.append(args.output.resolve())
        if len(set(targets)) != len(targets):
            raise ValueError("数据文件、缓存、诊断输出与进程锁必须使用不同路径")
        # Lock before changing any feed data, even when using a shared cache.
        guard = lock.open("a+")
        try:
            fcntl.flock(guard, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise ValueError("采集器已在运行，请勿重复启动") from error
        if not args.local_only:
            writer = FileFeedWriter(args.feed_file)
        state = CollectionState()
        if args.cache_file.exists():
            state.restore(read_json(args.cache_file, 16384), time.time(), time.monotonic())
        if writer:
            writer.write(state)
        due = time.monotonic()
        while True:
            if time.monotonic() >= due:
                try:
                    state.succeed(client.collect(), time.time(), time.monotonic())
                    save(args.cache_file, state.snapshot)
                except (MagpieError, ValueError, TypeError, OverflowError):
                    state.fail("读取 Magpie 失败，保留上次成功数据")
                due = time.monotonic() + 15
                delivery = writer.write(state) if writer else {"written": False}
                view = state.view(time.monotonic())
                view["delivery"] = "petui-python-text-file-v1" if writer else "local-only"
                view["feedWrite"] = delivery
                if args.output:
                    save(args.output, view)
                print(json.dumps(view, ensure_ascii=False, allow_nan=False), flush=True)
                if args.once:
                    return 1 if state.error else 0
            time.sleep(min(1, max(.01, due - time.monotonic())))
    except KeyboardInterrupt:
        return 0
    except (OSError, ValueError, KeyError, TypeError) as error:
        print("采集器停止：" + str(error), file=sys.stderr)
        return 1
    finally:
        if writer:
            writer.close()
        if guard:
            guard.close()


if __name__ == "__main__":
    raise SystemExit(main())
