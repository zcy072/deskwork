import asyncio
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.conftest import isolated_session_registry
from tests.test_bash_cancel import FakeClient
from tests.test_run_recovery import runtime
from app.sandbox import host_files, session_paths
from app.sandbox.bash_handler import BashSession
from app.sandbox.manager_client import RemoteProcess
from app.sandbox.session_paths import SessionPaths
from app.services import file_versions


async def test_host_read_cannot_follow_replaced_ancestor(tmp_path, monkeypatch):
    monkeypatch.setattr(session_paths, 'SANDBOX_DATA_ROOT', tmp_path)
    paths = SessionPaths.create('s', tmp_path / 'sessions')
    inside = paths.scratchpad / 'a'
    target = inside / 'b' / 'data.txt'
    target.parent.mkdir(parents=True)
    target.write_text('inside')
    outside = tmp_path / 'outside'
    (outside / 'b').mkdir(parents=True)
    (outside / 'b' / 'data.txt').write_text('OUTSIDE_SESSION_SENTINEL')
    opened = os.open
    swapped = False

    def switch_before_open(path, flags, *args, **kwargs):
        nonlocal swapped
        if not swapped and path == target.parent:
            swapped = True
            inside.rename(paths.scratchpad / 'old-a')
            inside.symlink_to(outside, target_is_directory=True)
        return opened(path, flags, *args, **kwargs)

    monkeypatch.setattr(host_files.os, 'open', switch_before_open)
    session = BashSession(paths)
    result = await session.read_container_file(paths.virtual_to_container_path('/workspace/scratchpad/a/b/data.txt'))
    print({'host_read': result, 'ancestor_swapped': swapped})
    assert 'OUTSIDE_SESSION_SENTINEL' not in result


def test_atomic_delivery_cannot_reopen_replaced_temporary_file(tmp_path, monkeypatch):
    workspace = tmp_path / 'workspace'
    workspace.mkdir()
    source = workspace / 'source.txt'
    source.write_text('delivery bytes')
    outside = tmp_path / 'outside.txt'
    outside.write_text('must be preserved')
    destination = workspace / 'result.txt'
    copy = file_versions.shutil.copy2

    def replace_temporary_then_copy(src, temporary, *args, **kwargs):
        temporary = Path(temporary)
        temporary.unlink()
        temporary.symlink_to(outside)
        return copy(src, temporary, *args, **kwargs)

    monkeypatch.setattr(file_versions.shutil, 'copy2', replace_temporary_then_copy)
    file_versions.copy_file_atomic(source, destination)
    print({'outside_bytes': outside.read_text(), 'output_is_symlink': destination.is_symlink()})
    assert outside.read_text() == 'must be preserved'


async def test_second_bash_cancel_keeps_lock_until_process_stops(tmp_path, monkeypatch):
    monkeypatch.setattr(session_paths, 'SANDBOX_DATA_ROOT', tmp_path)
    paths = SessionPaths.create('s', tmp_path / 'sessions')
    paths.scratchpad.mkdir(parents=True)
    command, stopping, release, stopped = [asyncio.Event() for _ in range(4)]

    class Client(FakeClient):
        async def write_stdin(self, process, data):
            await super().write_stdin(process, data)
            if 'sleep 10' in data:
                command.set()

        async def terminate(self, process, grace_sec=5):
            stopping.set()
            await release.wait()
            await super().terminate(process, grace_sec)
            stopped.set()

    client = Client('hang')
    session = BashSession(paths, manager_client=client)
    task = asyncio.create_task(session.execute('sleep 10'))
    await asyncio.wait_for(command.wait(), 2)
    task.cancel()
    await asyncio.wait_for(stopping.wait(), 2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    held = session.workspace_lock.locked()
    alive = client.spawned[0].returncode is None
    release.set()
    await asyncio.wait_for(stopped.wait(), 2)
    print({'workspace_lock_held': held, 'process_still_alive_at_release': alive})
    assert held or not alive


async def test_remote_termination_without_exit_ack_is_not_success():
    class Socket:
        close_code = None
        def __init__(self):
            self.incoming = asyncio.Queue()
            self.sent = []
        def __aiter__(self):
            return self
        async def __anext__(self):
            frame = await self.incoming.get()
            if frame is None:
                raise StopAsyncIteration
            return frame
        async def send(self, frame):
            self.sent.append(frame)
        async def close(self):
            await self.incoming.put(None)

    socket = Socket()
    process = RemoteProcess(socket)
    error = None
    try:
        await process.terminate(grace_sec=0.01)
    except Exception as exc:
        error = exc
    await asyncio.gather(process._task, return_exceptions=True)
    print({'termination_error': str(error), 'exit_ack_received': False,
           'reported_returncode': process.returncode})
    assert error is not None


async def test_run_retains_gate_when_remote_cleanup_fails(runtime, monkeypatch):
    from langchain.agents import create_agent
    from langchain_core.messages import AIMessage
    from tests.test_ask_user import ScriptedModel
    from app.agents.context import RunContext
    from app.agents.state import DeepAgentState
    from app.runtime.sessions import sessions
    from app.services import chat_operations as ops
    from app.tools import file_tools
    monkeypatch.setattr(session_paths, 'SANDBOX_DATA_ROOT', runtime.root)
    paths = SessionPaths.create('s')
    paths.scratchpad.mkdir(parents=True, exist_ok=True)
    began = asyncio.Event()

    class Client(FakeClient):
        async def write_stdin(self, process, data):
            await super().write_stdin(process, data)
            if 'sleep 10' in data:
                began.set()
        async def terminate(self, process, grace_sec=5):
            raise RuntimeError('stop not confirmed')

    client = Client('hang')
    shell = BashSession(paths, manager_client=client)
    class Manager:
        async def bash_tool_helper(self, sid, command, timeout, restart):
            return await shell.execute(command, timeout)
    monkeypatch.setattr(file_tools, 'CONTAINER_MANAGER', Manager())
    model = ScriptedModel(script=[AIMessage(content='', tool_calls=[{
        'id': 'shell', 'name': 'bash_tool', 'args': {'description': 'work', 'command': 'sleep 10'}
    }]), AIMessage(content='done')])
    graph = create_agent(model=model, tools=[file_tools.bash_tool], checkpointer=runtime.saver,
                         state_schema=DeepAgentState, context_schema=RunContext)
    monkeypatch.setattr(ops, 'get_agent', lambda: graph)
    await ops.start_chat('s', 'work', [])
    await asyncio.wait_for(began.wait(), 3)
    await ops.cancel_chat('s')
    handle = sessions.get('s')
    print({'gate_retained': handle is not None, 'remote_process_alive': client.spawned[0].returncode is None})
    assert handle is not None and handle.phase == 'recovery_required'


async def test_rewind_second_parallel_answer_keeps_first_answer(runtime):
    from langchain_core.messages import AIMessage, ToolMessage
    from tests.test_ask_user import PARALLEL_ASK
    from tests.test_run_recovery import send
    from app.agents.recovery import _unanswered_interrupts
    from app.db import runs, repository
    graph = runtime.graph([PARALLEL_ASK.model_copy(deep=True), AIMessage(content='done'), AIMessage(content='changed')])
    await send('question')
    state = await graph.aget_state(await runs.graph_config('s'))
    first = _unanswered_interrupts(state)[0].id
    await send('红', mode='resume', interrupt_id=first)
    state = await graph.aget_state(await runs.graph_config('s'))
    second = _unanswered_interrupts(state)[0].id
    await send('Word', mode='resume', interrupt_id=second)
    target = next(m for m in await repository.get_messages('s') if m.role == 'user' and m.content == 'Word')
    changed = await send('PDF', mode='resume', interrupt_id=second, rewind_to_message_id=target.id)
    state = await graph.aget_state(await runs.graph_config('s'))
    answers = {m.tool_call_id: m.content for m in state.values['messages'] if isinstance(m, ToolMessage)}
    print({'rewind_status': changed.status, 'answers': answers,
           'pending_questions': [i.value for i in _unanswered_interrupts(state)]})
    assert changed.status == 'complete'
    assert answers == {'c1': 'User answered: 红', 'c2': 'User answered: PDF'}


async def test_delegated_stream_does_not_leak_into_main_answer(runtime, monkeypatch):
    from langchain.agents import create_agent
    from langchain_core.messages import AIMessage, AIMessageChunk
    from langchain_core.outputs import ChatGenerationChunk
    from tests.test_ask_user import ScriptedModel
    from tests.test_run_recovery import send
    from app.agents.context import RunContext
    from app.agents.state import DeepAgentState
    from app.tools.task_tools import create_task_tool
    from app.services import chat_operations as ops

    class StreamingHelper(ScriptedModel):
        def _stream(self, messages, stop=None, run_manager=None, **kwargs):
            yield ChatGenerationChunk(message=AIMessageChunk(content='PRIVATE_HELPER_TEXT'))

    delegated = create_task_tool([], [{'name':'helper','description':'test','system_prompt':'test','tools':[]}],
                                 StreamingHelper(script=[AIMessage(content='PRIVATE_HELPER_TEXT')]), DeepAgentState)
    main = ScriptedModel(script=[AIMessage(content='',tool_calls=[{
        'id':'delegate','name':'task','args':{'description':'test','subagent_type':'helper'}
    }]),AIMessage(content='PUBLIC_FINAL_ANSWER')])
    graph = create_agent(model=main, tools=[delegated],checkpointer=runtime.saver,
                         state_schema=DeepAgentState,context_schema=RunContext)
    monkeypatch.setattr(ops,'get_agent',lambda:graph)
    result = await send('question')
    text = ''.join(data.get('text','') for _,event,data in runtime.events if event == 'ai.token')
    print({'status':result.status,'visible_text':text})
    assert 'PRIVATE_HELPER_TEXT' not in text


async def test_cancel_before_entry_finalization_survives_cancelled_waiter(runtime, monkeypatch):
    from app.services import chat_operations as ops
    from app.runtime.sessions import sessions
    from app.db import runs
    entered, finish = asyncio.Event(), asyncio.Event()
    real_save = runs.save

    async def delayed_save(*args, **kwargs):
        entered.set()
        await finish.wait()
        return await real_save(*args, **kwargs)

    monkeypatch.setattr(runs, 'save', delayed_save)
    pid = await ops.start_chat('s', 'q', [])
    handle = sessions.get('s')
    handle.task.cancel()
    waiter = asyncio.create_task(ops.cancel_chat('s'))
    await asyncio.wait_for(entered.wait(), 2)
    waiter.cancel()
    await asyncio.sleep(0)
    finish.set()
    try:
        await waiter
    except asyncio.CancelledError:
        pass
    await asyncio.sleep(0)
    stored = await runs.get(pid)
    print({'run_status':stored.status, 'gate_still_held':sessions.get('s') is not None,
           'worker_done':handle.task.done(), 'worker_entered':handle.started})
    # Clean up the test's stranded handle after observing it.
    stranded = sessions.get('s') is not None
    sessions.release(handle)
    assert stored.status == 'cancelled' and not stranded


async def test_shell_deadline_crossed_before_queue_wait_is_still_timeout(tmp_path, monkeypatch):
    from app.sandbox import bash_handler
    values = iter([0.0, 0.5, 1.1])
    session = BashSession(SessionPaths.create('deadline', tmp_path))
    monkeypatch.setattr(bash_handler, 'time', SimpleNamespace(monotonic=lambda: next(values)))
    result = await session._read_until_sentinel(1)
    print({'command_result_after_deadline':result})
    assert result[1] is True


@pytest.mark.parametrize('name',['view_image','review_document','search_document'])
async def test_unknown_tool_errors_do_not_expose_internal_details(name, monkeypatch):
    from app.tools import image_tools, review_tools, search_tools
    runtime = SimpleNamespace(config={'configurable':{'thread_id':'s'}},tool_call_id='c',state={'messages':[]})
    marker = 'PRIVATE_INTERNAL_SENTINEL'
    async def fail(*args,**kwargs):
        raise RuntimeError(marker)
    if name == 'view_image':
        monkeypatch.setattr(image_tools,'vision_enabled',lambda:True)
        monkeypatch.setattr(image_tools,'call_index_this_turn',lambda *a:1)
        monkeypatch.setattr(image_tools,'load_image',fail)
        result = await image_tools.view_image.coroutine('/workspace/uploads/a.png','inspect',runtime)
    elif name == 'review_document':
        monkeypatch.setattr(review_tools,'render_pages',fail)
        result = await review_tools.review_document.coroutine('/workspace/output/a.pdf','inspect',runtime)
    else:
        monkeypatch.setattr(search_tools,'get_index',fail)
        result = await search_tools.search_document.coroutine('/workspace/uploads/a.txt','q','inspect',runtime)
    feedback = result.update['messages'][0].content
    print({'tool':name,'feedback':feedback})
    assert marker not in feedback
