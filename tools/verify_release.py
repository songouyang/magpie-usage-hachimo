"""Verify final assets with synthetic data, without accessing Magpie or a device."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import zipfile

from build_release import ROOT, COMPONENT, COLLECTOR_FILES, plugin_source

sys.path.insert(0, str(ROOT / "collector"))
from magpie_usage.feed import encode_slot
from magpie_usage.metrics import build_snapshot
from magpie_usage.protocol import FEED_SCHEMA, SLOT_SIZE, FEED_SIZE


def require(condition, message):
    if not condition:
        raise ValueError(message)


def fixtures():
    sample = build_snapshot(
        {"data": [{"windows": [{"remaining": 69}]}]},
        {"series": [{"calls": 7, "errors": 2, "input": 100, "output": 20}],
         "calls": 7, "sessionCount": 3, "cost": 0.25, "input": 100,
         "cache_read": 30, "output": 20, "models": [{"name": "sample-model", "calls": 5}]},
        {"rows": [{"served": "sample-model", "status": 200, "ms": 3102}]},
        at=1795572000,
    )

    def feed(value, sequence=1, failed=False):
        return encode_slot(sequence, {
            "schema": FEED_SCHEMA, "snapshot": value, "failed": failed
        }).decode("ascii")

    ready = feed(sample)
    return {
        "waiting": None,
        "corrupt": "?" * FEED_SIZE,
        "missing": feed(build_snapshot({}, {}, {}, at=1795572000)) + " " * SLOT_SIZE,
        "zero": feed(build_snapshot({}, {"series": [{"calls": 0, "errors": 0, "input": 0}],
                                         "cost": 0}, {}, at=1795572000)) + " " * SLOT_SIZE,
        "long-model": feed({**sample, "latest_model": "模型W" * 30}) + " " * SLOT_SIZE,
        "four-pages": ready + " " * SLOT_SIZE,
        "torn-newer": ready + "?" * SLOT_SIZE,
        "failed": ready + feed(sample, sequence=2, failed=True),
    }


HARNESS = '''
import sys
from unittest.mock import patch
from magpie_usage.client import MagpieClient, MagpieError
from magpie_usage.__main__ import main
sample = ({"data": []}, {"series": [{"calls": 7, "errors": 2, "input": 100}],
          "calls": 7, "sessionCount": 3}, {"rows": []})
at, mode = int(sys.argv[1]), sys.argv[2]
sys.argv = ["magpie_usage", "--once"]
with patch.object(MagpieClient, "collect", return_value=sample,
                  side_effect=MagpieError("offline") if mode == "fail" else None), \\
     patch("time.time", return_value=at):
    raise SystemExit(main())
'''


def verify(output_dir, petui):
    version = json.loads((COMPONENT / "component.json").read_text())["version"]
    output_dir = Path(output_dir).resolve()
    package = output_dir / ("magpie-usage-hachimo-" + version + ".hachiapp")
    collector = output_dir / ("magpie-collector-" + version + ".zip")
    expected_assets = {package.name, collector.name, "INSTALL.md"}
    sums = {}
    for line in (output_dir / "SHA256SUMS.txt").read_text().splitlines():
        digest, name = line.split("  ", 1)
        require(name not in sums, "重复校验和条目")
        sums[name] = digest
    require(set(sums) == expected_assets, "Release 附件清单不匹配")
    for name, digest in sums.items():
        require(hashlib.sha256((output_dir / name).read_bytes()).hexdigest() == digest,
                "校验和不匹配：" + name)
    namespace = {}
    exec(compile(plugin_source(), "generated-plugin.py", "exec"), namespace)
    with zipfile.ZipFile(package) as archive:
        require(archive.testzip() is None, "组件 ZIP CRC 错误")
        actual = {name for name in archive.namelist() if not name.endswith("/")}
        expected = {
            str(path.relative_to(COMPONENT)) for path in COMPONENT.rglob("*")
            if path.is_file() and path.name not in ("plugin.py", "hachiapp.json", ".DS_Store")
            and "__pycache__" not in path.parts and path.suffix != ".pyc"
        } | {"pc/plugin.py", "hachiapp.json", "LICENSE.txt", "THIRD_PARTY_NOTICES.txt"}
        require(actual == expected, "组件含缺失或多余文件")
        require(archive.read("pc/plugin.py") == plugin_source().encode("utf-8"), "插件与源码不同")
        for name, source in (("LICENSE.txt", "LICENSE"), ("THIRD_PARTY_NOTICES.txt", "THIRD_PARTY_NOTICES.md")):
            require(archive.read(name) == (ROOT / source).read_bytes(), "组件许可证不同")
        for name in expected - {"pc/plugin.py", "hachiapp.json", "LICENSE.txt", "THIRD_PARTY_NOTICES.txt"}:
            require(archive.read(name) == (COMPONENT / name).read_bytes(), "组件源码不同：" + name)
    subprocess.run([str(petui), "validate", str(package)], check=True)
    cases = fixtures()
    with tempfile.TemporaryDirectory(prefix="magpie-verify-") as temporary:
        staging = Path(temporary)
        for name, value in cases.items():
            fixture = staging / (name + ".json")
            fixture.write_text(json.dumps(value), encoding="utf-8")
            result = subprocess.run([str(petui), "python-preview", str(package), str(fixture)],
                                    capture_output=True, text=True, check=True)
            require(json.loads(result.stdout) == namespace["snapshot"](value),
                    "MicroPython 预览不匹配：" + name)
        with zipfile.ZipFile(collector) as archive:
            require(archive.testzip() is None, "采集器 ZIP CRC 错误")
            require(set(archive.namelist()) == {"collector/" + name for name in COLLECTOR_FILES},
                    "采集器含缺失或多余文件")
            for name in COLLECTOR_FILES:
                require(archive.read("collector/" + name) == (ROOT / "collector" / name).read_bytes(),
                        "采集器源码不同：" + name)
            require(archive.getinfo("collector/start-collector.command").external_attr >> 16 & 0o111,
                    "启动器缺少可执行权限")
            archive.extractall(staging)
        directory = staging / "collector"
        # Clear PYTHONPATH so these processes can only import extracted sources.
        env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
        env.pop("PYTHONPATH", None)
        inode = None
        runs = []
        for at, mode, status, code in (
            (1795572000, "ok", "ready", 0),
            (1795572090, "fail", "stale", 1),
            (1795572120, "ok", "ready", 0),
        ):
            result = subprocess.run([sys.executable, "-c", HARNESS, str(at), mode],
                                    cwd=directory, env=env, capture_output=True, text=True)
            require(result.returncode == code, "解压后 CLI 退出码错误：" + result.stderr)
            value = json.loads(result.stdout)
            require(value["status"] == status and value["feedWrite"]["written"], "CLI 状态错误")
            feed = directory / "data/magpie-usage.txt"
            require(feed.stat().st_size == FEED_SIZE, "数据文件大小错误")
            require(inode is None or feed.stat().st_ino == inode, "文件身份发生变化")
            inode = feed.stat().st_ino
            success_at = 1795572000 if mode == "fail" else at
            require(value["snapshot"]["generated_at"] == success_at, "成功时间被失败采集覆盖")
            cached = json.loads((directory / ".last-success.json").read_text())
            require(cached["generated_at"] == success_at, "重启缓存错误")
            view = namespace["snapshot"](feed.read_text(encoding="ascii"))
            require(view["successAt"] == success_at * 1000 and view["failed"] == (mode == "fail"),
                    "采集器与插件端到端输出不同")
            runs.append({"mode": mode, "status": status, "exitCode": code})
        fixture = staging / "four-pages.json"
        inspections = {}
        for command in ("first-data", "install-rehearsal"):
            result = subprocess.run([str(petui), command, str(package), "--fixture", str(fixture)],
                                    capture_output=True, text=True, check=True)
            inspection = json.loads(result.stdout)
            # Exit success is inspection success, not proof of device delivery.
            inspections[command] = {"completed": True, "hostSnapshot": inspection.get("hostSnapshot")}
    report = {"version": version, "passed": True, "network": "mocked-only",
              "microPythonCases": list(cases), "extractedCollector": runs,
              "sourceAndChecksumMatch": True, "inspections": inspections,
              "physicalDeviceTested": False}
    (output_dir / "verification.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    return report


def main():
    parser = argparse.ArgumentParser(description="用无隐私样例验证最终 Release 文件")
    parser.add_argument("--output-dir", type=Path, default=ROOT / "delivery")
    parser.add_argument("--petui", default=os.environ.get(
        "PETUI", str(Path.home() / ".claw-pet/authoring/petui")
    ))
    args = parser.parse_args()
    print(json.dumps(verify(args.output_dir, args.petui), ensure_ascii=False))


if __name__ == "__main__":
    main()
