"""LangGraph checkpoint 的导出与导入(只支持 AsyncSqliteSaver)。

直接搬运 checkpoints / writes 两张表的原始行(blob 用 base64),不经反序列化,
所以导出内容与库里逐字节一致。

导入必须先校验再写入:LangGraph 读 checkpoint 时会按数据里写的模块名 import 并调用,
不受信任的备份可以借此执行任意代码。因此导入时用 LangGraph 自己的安全类型白名单逐层检查
msgpack 扩展类型,不在白名单里的整份 checkpoint 都拒绝。
"""

import base64
import binascii
import json
from dataclasses import dataclass, field
from typing import Any

import ormsgpack
from langgraph.checkpoint.serde import _msgpack as _lg_msgpack
from langgraph.checkpoint.serde.jsonplus import (
    EXT_CONSTRUCTOR_KW_ARGS,
    EXT_CONSTRUCTOR_POS_ARGS,
    EXT_CONSTRUCTOR_SINGLE_ARG,
    EXT_DELTA_SNAPSHOT,
    EXT_METHOD_SINGLE_ARG,
    EXT_PYDANTIC_V1,
    EXT_PYDANTIC_V2,
)
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

PAYLOAD_VERSION = 1
MAX_ROWS = 20000
MAX_RAW_BYTES = 150 * 1024 * 1024
# 我们自己的 saver 只会写这几种;json / pickle 在读取时有各自的执行风险,不接受
_ALLOWED_TYPES = frozenset({"msgpack", "null", "bytes", "bytearray"})
_CONSTRUCTOR_CODES = (
    EXT_CONSTRUCTOR_SINGLE_ARG,
    EXT_CONSTRUCTOR_POS_ARGS,
    EXT_CONSTRUCTOR_KW_ARGS,
    EXT_PYDANTIC_V1,
    EXT_PYDANTIC_V2,
)


class CheckpointRejected(ValueError):
    """checkpoint 不合法、不安全,或当前环境无法保存。"""


@dataclass
class ParsedCheckpoint:
    # (ns, id, parent_id, type, checkpoint, metadata)
    checkpoints: list[tuple] = field(default_factory=list)
    # (ns, checkpoint_id, task_id, task_path, idx, channel, type, value)
    writes: list[tuple] = field(default_factory=list)

    @property
    def ids(self) -> set[str]:
        return {c[1] for c in self.checkpoints}


def _b64(data: bytes | None) -> str | None:
    return None if data is None else base64.b64encode(data).decode("ascii")


async def _has_task_path(saver: AsyncSqliteSaver) -> bool:
    cur = await saver.conn.execute("PRAGMA table_info(writes)")
    return any(row[1] == "task_path" for row in await cur.fetchall())


async def export_thread(saver: Any, thread_id: str) -> dict | None:
    """线程的全部 checkpoint 与 writes;没有记录或 saver 不是 SQLite 时返回 None。

    超过 MAX_ROWS / MAX_RAW_BYTES 抛 CheckpointRejected。"""
    if not isinstance(saver, AsyncSqliteSaver):
        return None
    await saver.setup()
    async with saver.lock:
        task_path = "task_path" if await _has_task_path(saver) else "'' AS task_path"
        cur = await saver.conn.execute(
            "SELECT checkpoint_ns, checkpoint_id, parent_checkpoint_id, type, checkpoint, metadata "
            "FROM checkpoints WHERE thread_id = ? ORDER BY checkpoint_ns, checkpoint_id",
            (thread_id,),
        )
        cps = await cur.fetchall()
        cur = await saver.conn.execute(
            f"SELECT checkpoint_ns, checkpoint_id, task_id, {task_path}, idx, channel, type, value "
            "FROM writes WHERE thread_id = ? ORDER BY checkpoint_ns, checkpoint_id, task_id, idx",
            (thread_id,),
        )
        ws = await cur.fetchall()
    if not cps:
        if ws:
            raise CheckpointRejected("checkpoint 缺失,仅存在写入记录")
        return None
    if len(cps) + len(ws) > MAX_ROWS:
        raise CheckpointRejected(f"checkpoint 记录过多({len(cps) + len(ws)} 条)")
    raw = sum(len(r[4] or b"") + len(r[5] or b"") for r in cps) + sum(len(r[7] or b"") for r in ws)
    if raw > MAX_RAW_BYTES:
        raise CheckpointRejected("checkpoint 数据过大")
    return {
        "payload_version": PAYLOAD_VERSION,
        "checkpoints": [
            {
                "ns": ns,
                "id": cid,
                "parent_id": parent,
                "type": typ,
                "checkpoint": _b64(blob),
                "metadata": _b64(meta),
            }
            for ns, cid, parent, typ, blob, meta in cps
        ],
        "writes": [
            {
                "ns": ns,
                "checkpoint_id": cid,
                "task_id": task_id,
                "task_path": tp,
                "idx": idx,
                "channel": channel,
                "type": typ,
                "value": _b64(value),
            }
            for ns, cid, task_id, tp, idx, channel, typ, value in ws
        ],
    }


def _check_msgpack(data: bytes) -> None:
    """逐层检查扩展类型都在白名单里;不执行任何构造。"""

    rejected: list[str] = []

    def reject(reason: str) -> None:
        # ormsgpack 会把 ext_hook 里抛出的异常包成 ValueError,原因先存下来
        rejected.append(reason)
        raise CheckpointRejected(reason)

    def hook(code: int, payload: bytes) -> None:
        if code == EXT_DELTA_SNAPSHOT:
            ormsgpack.unpackb(payload, ext_hook=hook, option=ormsgpack.OPT_NON_STR_KEYS)
            return None
        if code in _CONSTRUCTOR_CODES:
            tup = ormsgpack.unpackb(payload, ext_hook=hook, option=ormsgpack.OPT_NON_STR_KEYS)
            if (
                not isinstance(tup, list | tuple)
                or len(tup) < 3
                or (tup[0], tup[1]) not in _lg_msgpack.SAFE_MSGPACK_TYPES
            ):
                name = (
                    f"{tup[0]}.{tup[1]}" if isinstance(tup, list | tuple) and len(tup) > 1 else "?"
                )
                reject(f"checkpoint 含不在白名单内的类型 {name}")
            return None
        if code == EXT_METHOD_SINGLE_ARG:
            tup = ormsgpack.unpackb(payload, ext_hook=hook, option=ormsgpack.OPT_NON_STR_KEYS)
            if (
                not isinstance(tup, list | tuple)
                or len(tup) < 4
                or (tup[0], tup[1], tup[3]) not in _lg_msgpack.SAFE_MSGPACK_METHODS
            ):
                reject("checkpoint 含不在白名单内的方法调用")
            return None
        reject(f"checkpoint 含未知的扩展类型 {code}")

    try:
        ormsgpack.unpackb(data, ext_hook=hook, option=ormsgpack.OPT_NON_STR_KEYS)
    except Exception as exc:
        if rejected:
            raise CheckpointRejected(rejected[0]) from exc
        raise CheckpointRejected(f"checkpoint 数据损坏:{type(exc).__name__}") from exc


def _blob(row: dict, key: str, *, required: bool) -> bytes | None:
    value = row.get(key)
    if value is None:
        if required:
            raise CheckpointRejected(f"checkpoint 缺少 {key}")
        return None
    if not isinstance(value, str):
        raise CheckpointRejected(f"checkpoint 字段 {key} 格式不对")
    try:
        return base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise CheckpointRejected(f"checkpoint 字段 {key} 不是合法的 base64") from exc


def _typed_blob(row: dict, key: str, *, required: bool) -> tuple[str | None, bytes | None]:
    typ = row.get("type")
    if typ is not None and typ not in _ALLOWED_TYPES:
        raise CheckpointRejected(f"checkpoint 使用了不接受的序列化类型 {typ!r}")
    data = _blob(row, key, required=required)
    if typ == "msgpack":
        if data is None:
            raise CheckpointRejected("checkpoint 数据为空")
        _check_msgpack(data)
    return typ, data


def _text(row: dict, key: str, *, max_len: int = 256) -> str:
    value = row.get(key)
    if not isinstance(value, str) or len(value) > max_len:
        raise CheckpointRejected(f"checkpoint 字段 {key} 格式不对")
    return value


def parse_payload(payload: Any) -> ParsedCheckpoint:
    """校验并解码 checkpoint.json 的内容;不合法抛 CheckpointRejected。"""
    if not isinstance(payload, dict) or payload.get("payload_version") != PAYLOAD_VERSION:
        raise CheckpointRejected("checkpoint 格式版本不支持")
    cps, ws = payload.get("checkpoints"), payload.get("writes")
    if not isinstance(cps, list) or not isinstance(ws, list) or not cps:
        raise CheckpointRejected("checkpoint 内容为空或格式不对")
    if len(cps) + len(ws) > MAX_ROWS:
        raise CheckpointRejected("checkpoint 记录过多")

    parsed = ParsedCheckpoint()
    seen: set[tuple[str, str]] = set()
    for row in cps:
        if not isinstance(row, dict):
            raise CheckpointRejected("checkpoint 记录格式不对")
        ns, cid = _text(row, "ns"), _text(row, "id")
        if (ns, cid) in seen or not cid:
            raise CheckpointRejected("checkpoint 记录重复或缺少 id")
        seen.add((ns, cid))
        parent = row.get("parent_id")
        if parent is not None and not isinstance(parent, str):
            raise CheckpointRejected("checkpoint 字段 parent_id 格式不对")
        typ, blob = _typed_blob(row, "checkpoint", required=True)
        meta = _blob(row, "metadata", required=False)
        if meta is not None:
            try:
                json.loads(meta)
            except ValueError as exc:
                raise CheckpointRejected("checkpoint 元数据不是合法的 JSON") from exc
        parsed.checkpoints.append((ns, cid, parent, typ, blob, meta))

    written: set[tuple[str, str, str, int]] = set()
    for row in ws:
        if not isinstance(row, dict):
            raise CheckpointRejected("checkpoint 写入记录格式不对")
        ns, cid = _text(row, "ns"), _text(row, "checkpoint_id")
        if (ns, cid) not in seen:
            raise CheckpointRejected("写入记录指向不存在的 checkpoint")
        task_id = _text(row, "task_id")
        idx = row.get("idx")
        if not isinstance(idx, int) or isinstance(idx, bool):
            raise CheckpointRejected("checkpoint 字段 idx 格式不对")
        if (ns, cid, task_id, idx) in written:
            raise CheckpointRejected("checkpoint 写入记录重复")
        written.add((ns, cid, task_id, idx))
        typ, value = _typed_blob(row, "value", required=False)
        parsed.writes.append(
            (
                ns,
                cid,
                task_id,
                _text(row, "task_path", max_len=1024),
                idx,
                _text(row, "channel"),
                typ,
                value,
            )
        )
    return parsed


async def import_thread(saver: Any, thread_id: str, parsed: ParsedCheckpoint) -> None:
    """把解析好的行写进 saver,作为 thread_id 这个新线程。同一事务,失败回滚。"""
    if not isinstance(saver, AsyncSqliteSaver):
        raise CheckpointRejected("当前服务没有持久化 checkpoint,无法恢复上下文")
    await saver.setup()
    async with saver.lock:
        has_task_path = await _has_task_path(saver)
        try:
            await saver.conn.executemany(
                "INSERT INTO checkpoints (thread_id, checkpoint_ns, checkpoint_id, "
                "parent_checkpoint_id, type, checkpoint, metadata) VALUES (?, ?, ?, ?, ?, ?, ?)",
                [(thread_id, *row) for row in parsed.checkpoints],
            )
            if has_task_path:
                await saver.conn.executemany(
                    "INSERT INTO writes (thread_id, checkpoint_ns, checkpoint_id, task_id, "
                    "task_path, idx, channel, type, value) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    [(thread_id, *row) for row in parsed.writes],
                )
            else:
                await saver.conn.executemany(
                    "INSERT INTO writes (thread_id, checkpoint_ns, checkpoint_id, task_id, "
                    "idx, channel, type, value) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    [(thread_id, r[0], r[1], r[2], *r[4:]) for r in parsed.writes],
                )
            await saver.conn.commit()
        except BaseException:
            await saver.conn.rollback()
            raise
