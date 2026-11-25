"""Build release assets from maintained sources using the official PetUI tool."""
import argparse
import ast
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import zipfile

ROOT = Path(__file__).resolve().parents[1]
COMPONENT = ROOT / "package-python/magpie-usage-hachimo"
COLLECTOR_FILES = (
    "magpie_usage/__init__.py", "magpie_usage/__main__.py",
    "magpie_usage/client.py", "magpie_usage/feed.py", "magpie_usage/metrics.py",
    "magpie_usage/protocol.py", "magpie_usage/state.py",
    "start-collector.command", "README.md", "LICENSE", "THIRD_PARTY_NOTICES.md",
)


def plugin_source():
    """Inline shared modules without retaining imports unavailable in MicroPython."""
    entry = (ROOT / "tools/plugin.py").read_text(encoding="utf-8")
    lines = entry.splitlines(keepends=True)
    excluded = set()
    for node in ast.parse(entry).body:
        if isinstance(node, ast.ImportFrom) and node.module in (
            "magpie_usage.protocol", "magpie_usage.projection"
        ):
            excluded.update(range(node.lineno - 1, node.end_lineno))
    sources = [
        (ROOT / "collector/magpie_usage" / name).read_text(encoding="utf-8")
        for name in ("protocol.py", "projection.py")
    ]
    sources.append("".join(line for index, line in enumerate(lines) if index not in excluded))
    source = "\n\n".join(sources)
    if len(source.encode("utf-8")) >= 16 * 1024:
        raise ValueError("Python 插件超出 16 KiB 预算")
    return source


def collector_zip(output):
    # A positive list keeps runtime data and build-only projection code out.
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for relative in sorted(COLLECTOR_FILES):
            entry = zipfile.ZipInfo("collector/" + relative, (1980, 1, 1, 0, 0, 0))
            entry.compress_type = zipfile.ZIP_DEFLATED
            entry.external_attr = (0o100755 if relative.endswith(".command") else 0o100644) << 16
            archive.writestr(entry, (ROOT / "collector" / relative).read_bytes())


def build(output_dir, petui):
    manifest = json.loads((COMPONENT / "component.json").read_text(encoding="utf-8"))
    version = manifest["version"]
    output_dir = Path(output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    # Only copy this component's supported sources, never local authoring history.
    with tempfile.TemporaryDirectory(prefix="magpie-release-") as temporary:
        staging = Path(temporary)
        component = staging / "component"
        shutil.copytree(COMPONENT, component, ignore=shutil.ignore_patterns(
            ".DS_Store", "__pycache__", "*.pyc", "plugin.py", "hachiapp.json"
        ))
        (component / "pc/plugin.py").write_text(plugin_source(), encoding="utf-8")
        shutil.copyfile(ROOT / "LICENSE", component / "LICENSE.txt")
        shutil.copyfile(ROOT / "THIRD_PARTY_NOTICES.md", component / "THIRD_PARTY_NOTICES.txt")
        package = staging / (manifest["id"] + "-" + version + ".hachiapp")
        subprocess.run([str(petui), "pack", str(component), str(package)], check=True)
        subprocess.run([str(petui), "validate", str(package)], check=True)
        collector = staging / ("magpie-collector-" + version + ".zip")
        collector_zip(collector)
        instructions = staging / "使用说明.md"
        instructions.write_text(
            (ROOT / "docs/使用说明.md").read_text(encoding="utf-8").replace("{version}", version),
            encoding="utf-8",
        )
        assets = [package, collector, instructions]
        checksums = staging / "SHA256SUMS.txt"
        checksums.write_text("".join(
            hashlib.sha256(asset.read_bytes()).hexdigest() + "  " + asset.name + "\n"
            for asset in assets
        ), encoding="utf-8")
        for asset in assets + [checksums]:
            shutil.copyfile(asset, output_dir / asset.name)
    return {"version": version, "outputDir": str(output_dir), "assets": [
        asset.name for asset in assets + [checksums]
    ]}


def main():
    parser = argparse.ArgumentParser(description="构建 .hachiapp、采集器 ZIP、使用说明与校验和")
    parser.add_argument("--output-dir", type=Path, default=ROOT / "delivery")
    parser.add_argument("--petui", default=os.environ.get(
        "PETUI", str(Path.home() / ".claw-pet/authoring/petui")
    ))
    args = parser.parse_args()
    print(json.dumps(build(args.output_dir, args.petui), ensure_ascii=False))


if __name__ == "__main__":
    main()
