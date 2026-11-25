"""Stable-identity two-slot file publisher; never accesses Manager storage."""
import fcntl
import json
import os
from pathlib import Path
import stat

from .protocol import SLOT_SIZE, FEED_SIZE, FEED_SCHEMA, MAX_SEQUENCE, checksum, latest


def safe_path(path):
    path = Path(path).absolute()
    if any(parent.is_symlink() and parent not in (Path("/tmp"), Path("/var"))
           for parent in (path, *path.parents)):
        raise ValueError("输出路径不能包含符号链接")
    target = path.resolve()
    for protected in (Path.home()/".claw-pet", Path.home()/"Library/Application Support/com.petmanager.desktop"):
        if target == protected or protected in target.parents:
            raise ValueError("采集器输出不能写入组件库或 Pet Manager 私有目录")
    return path


def read_json(path, budget):
    path = Path(path)
    if path.is_symlink() or not path.is_file() or path.stat().st_size > budget:
        raise ValueError("文件无效、是符号链接或超出预算：" + path.name)
    return json.loads(path.read_text(encoding="utf-8"))


def encode_slot(sequence, value):
    payload = json.dumps(value, ensure_ascii=True, allow_nan=False, separators=(",", ":"))
    record = json.dumps({"sequence": sequence, "payload": payload,
                         "checksum": checksum(sequence, payload)}, separators=(",", ":")).encode("ascii")
    if len(record) >= SLOT_SIZE:
        raise ValueError("采样超出 4096 字节数据槽，保留原数据")
    return record + b" " * (SLOT_SIZE - 1 - len(record)) + b"\n"


class FileFeedWriter:
    def __init__(self, path):
        self.path = safe_path(path)
        self.fd = None
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        flags = os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK
        fd = os.open(self.path, flags, 0o600)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise ValueError("数据源必须是无硬链接的普通文件")
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as error:
                raise ValueError("该数据文件已有采集器运行") from error
            if info.st_size == 0:
                waiting = {"schema": FEED_SCHEMA, "snapshot": None, "failed": False}
                self._write_at(fd, b" " * SLOT_SIZE + encode_slot(0, waiting), 0)
                os.fsync(fd)
            elif info.st_size != FEED_SIZE:
                raise ValueError("数据文件格式无效，原文件保留")
            elif latest(os.pread(fd, FEED_SIZE, 0).decode("ascii")) is None:
                raise ValueError("两个数据槽均损坏，原文件保留")
            self.fd = fd
            self.identity = (info.st_dev, info.st_ino)
        except Exception:
            os.close(fd)
            raise

    @staticmethod
    def _write_at(fd, raw, offset):
        while raw:
            count = os.pwrite(fd, raw, offset)
            if count <= 0:
                raise OSError("数据文件写入失败")
            offset += count
            raw = raw[count:]

    def write(self, collection):
        safe_path(self.path)
        info = self.path.stat()
        if (info.st_dev, info.st_ino) != self.identity or info.st_nlink != 1 or info.st_size != FEED_SIZE:
            raise ValueError("数据文件已被替换或修改，请重启采集器并在组件中重新选择文件")
        raw = os.pread(self.fd, FEED_SIZE, 0).decode("ascii")
        previous = latest(raw)
        value = {"schema": FEED_SCHEMA, "snapshot": collection.snapshot, "failed": bool(collection.error)}
        if previous and previous[2] == value:
            return {"written": False, "reason": "unchanged"}
        sequence = previous[0] + 1 if previous else 1
        if sequence > MAX_SEQUENCE:
            raise ValueError("数据序号超出预算")
        index = 1 - previous[1] if previous else 0
        record = encode_slot(sequence, value)
        self._write_at(self.fd, record, index * SLOT_SIZE)
        os.fsync(self.fd)
        return {"written": True, "sequence": sequence, "slot": index, "bytes": FEED_SIZE}

    def close(self):
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None
