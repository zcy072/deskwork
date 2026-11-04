"""通过后端 HTTP API 跑单个用例并收集轨迹(与真实用户走同一条链路)。"""

import json
import time
import uuid
from pathlib import Path

import httpx
from checks import RunResult


class Client:
    def __init__(self, base_url: str, api_key: str | None):
        headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        self.http = httpx.Client(base_url=base_url, headers=headers, timeout=60)

    def send(self, session_id: str, query: str, files: list[Path]) -> str:
        handles = [("files", (p.name, p.read_bytes())) for p in files]
        r = self._post_with_backoff(
            "/send-message", data={"session_id": session_id, "user_query": query}, files=handles
        )
        return r.json()["prompt_id"]

    def _post_with_backoff(self, url: str, **kw) -> httpx.Response:
        """遵守服务端限流:遇到 429 按 Retry-After(缺省 10s)等待后重试,不去放宽限流。"""
        for _ in range(12):
            r = self.http.post(url, **kw)
            if r.status_code != 429:
                r.raise_for_status()
                return r
            time.sleep(float(r.headers.get("Retry-After", 10)))
        r.raise_for_status()
        return r

    def events(self, session_id: str, prompt_id: str, deadline: float):
        """与前端 streamEvents 相同:服务端空闲会主动断开,带最后的 id 重连直到 stream.end。"""
        last = "0-0"
        while time.monotonic() < deadline:
            body = {"session_id": session_id, "prompt_id": prompt_id, "start_id": last, "block_ms": 4000}
            with self.http.stream(
                "POST", "/stream-events", json=body, timeout=httpx.Timeout(30, read=30)
            ) as resp:
                if resp.status_code == 429:
                    time.sleep(float(resp.headers.get("Retry-After", 5)))
                    continue
                resp.raise_for_status()
                for line in resp.iter_lines():
                    if not line.startswith("data:"):
                        continue
                    payload = line[5:].strip()
                    if payload == "[DONE]":
                        return
                    packet = json.loads(payload)
                    last = packet.get("id", last)
                    yield packet
                    if packet["event"] == "stream.end":
                        return

    def cancel(self, session_id: str) -> None:
        try:
            self.http.post(f"/thread-cancel/{session_id}")
        except httpx.HTTPError:
            pass

    def download(self, session_id: str, rel: str) -> bytes:
        r = self.http.get("/files/download", params={"session_id": session_id, "relative_path": rel})
        r.raise_for_status()
        return r.content

    def capabilities(self) -> dict:
        r = self.http.get("/capabilities")
        r.raise_for_status()
        return r.json()

    def list_memories(self) -> list[dict]:
        r = self.http.get("/memories")
        r.raise_for_status()
        return r.json()

    def add_memory(self, content: str) -> None:
        self.http.post("/memories", json={"content": content, "ttl_days": 0}).raise_for_status()

    def clear_memories(self) -> None:
        for m in self.list_memories():
            self.http.delete(f"/memories/{m['id']}").raise_for_status()

    def delete_conversation(self, session_id: str) -> None:
        try:
            self.http.delete(f"/conversations/{session_id}")
        except httpx.HTTPError:
            pass


RISK_CONFIRM_PREFIX = "即将执行高风险操作"


def run_case(client: Client, case: dict, fixtures: Path, timeout: float) -> tuple[RunResult, dict]:
    """返回 (可判定产物, 指标+完整轨迹)。"""
    session_id = f"eval-{uuid.uuid4().hex[:12]}"
    files = [fixtures / f for f in case.get("files", [])]
    res = RunResult()
    trajectory: list[dict] = []
    usage: dict = {}
    started = time.monotonic()
    deadline = started + timeout
    presented: list[dict] = []
    text_parts: list[str] = []
    asked_this_turn = False
    # 用例可以带 answers:模型提问(ask.user)后依次用它们回答,衡量"问完能否把事做完"
    queue = [case["prompt"], *case.get("answers", [])]
    uses_memory = bool(case.get("memory"))
    if uses_memory:  # 记忆是全局状态:先清空再播种,保证用例之间互不影响
        client.clear_memories()
        for m in case.get("seed_memories", []):
            client.add_memory(m)
    try:
        for turn, query in enumerate(queue):
            if turn > 0 and not asked_this_turn:
                break  # 没有提问就不需要回答
            asked_this_turn = False
            res.ended = False
            prompt_id = client.send(session_id, query, files if turn == 0 else [])
            for packet in client.events(session_id, prompt_id, deadline):
                ev, data = packet["event"], packet.get("data") or {}
                if ev != "ai.token":  # token 逐字太碎,最终文本单独存
                    trajectory.append({"t": round(time.monotonic() - started, 2), **packet})
                if ev == "ai.token":
                    text_parts.append(data.get("text", ""))
                elif ev == "ai.tool_call":
                    if data.get("text") and not text_parts:
                        text_parts.append(data["text"])
                    res.tool_calls.extend(data.get("tool_calls", []))
                elif ev == "tool.result":
                    res.tool_results.append(data)
                elif ev == "ask.user":
                    res.asks.append(data)
                    asked_this_turn = True
                elif ev == "files.presented":
                    presented.extend(data.get("files", []))
                elif ev == "usage":
                    usage = _add_usage(usage, data)
                elif ev == "error":
                    res.error = data.get("message", "unknown error")
                elif ev == "stream.end":
                    res.ended = True
            if res.error:
                break
        if asked_this_turn and not res.error and res.asks[-1].get("question", "").startswith(RISK_CONFIRM_PREFIX):
            # 后端的高风险命令确认不是用例要测的提问;没有脚本回答时运行会静默结束,必须明确标成失败
            res.error = "被高风险命令确认拦住且无脚本回答: " + res.asks[-1]["question"].replace("\n", " ")[:160]
        if not res.ended and time.monotonic() >= deadline:
            res.error = f"超时({timeout:.0f}s)"
            client.cancel(session_id)
    except Exception as exc:
        res.error = f"runner 异常: {exc!r}"
        client.cancel(session_id)
    if uses_memory:
        try:
            res.memories = [m["content"] for m in client.list_memories()]
        finally:
            client.clear_memories()
    # 最终回答 = 最后一次工具调用之后的文本;没有工具调用时即全部文本
    res.answer = _final_answer(trajectory, text_parts)
    for f in presented:
        rel = _rel(f.get("container_path") or "")
        if rel.startswith("output/"):
            try:
                res.files[rel] = client.download(session_id, rel)
            except httpx.HTTPError as exc:
                trajectory.append({"event": "runner.download_failed", "data": {"path": rel, "err": str(exc)}})
    elapsed = time.monotonic() - started
    client.delete_conversation(session_id)
    metrics = {
        "session_id": session_id,
        "seconds": round(elapsed, 1),
        "tool_calls": len(res.tool_calls),
        "usage": usage,
        "trajectory": trajectory,
    }
    return res, metrics


def _rel(container_path: str) -> str:
    return container_path.removeprefix("/workspace/").lstrip("/")


def _final_answer(trajectory: list[dict], tokens: list[str]) -> str:
    # tokens 是整轮的全部正文,按最后一个 tool.result 之后切分较复杂;
    # 评测关心整轮对用户说了什么,故直接返回全部正文。
    return "".join(tokens)


def _add_usage(total: dict, one: dict) -> dict:
    """多轮用例把各轮的 usage 累加;models 取并集。"""
    out = dict(total)
    for k, v in one.items():
        if k == "models":
            out[k] = sorted(set(out.get(k, [])) | set(v))
        elif isinstance(v, int | float):
            out[k] = out.get(k, 0) + v
    return out
