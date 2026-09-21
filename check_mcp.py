"""Read-only MCP handshake by default. --live runs a tiny real Claude round trip."""
import asyncio
import json
from pathlib import Path
import sys
import time
import uuid
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


def unpack(result):
    if result.isError:
        raise RuntimeError(str(result.content))
    return json.loads(result.content[0].text)


async def main():
    base = Path(__file__).resolve().parent
    params = StdioServerParameters(command=sys.executable, args=[str(base / 'server.py')])
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            names = [t.name for t in (await session.list_tools()).tools]
            assert {'submit_task', 'task_result', 'notify_claude', 'read_replies', 'read_reply', 'archive_reply'} <= set(names), names
            print('MCP tools:', ', '.join(names), flush=True)
            print('Info:', unpack(await session.call_tool('dispatcher_info', {})), flush=True)
            if '--live' not in sys.argv:
                return
            job = unpack(await session.call_tool('submit_task', {
                'title': 'MCP live smoke test',
                'prompt': 'Это проверка связи. Запомни контрольное слово KEDR-742. Ответь только DISPATCHER_OK. Не используй инструменты.',
                'request_key': 'smoke-' + str(uuid.uuid4()), 'timeout_seconds': 120}))
            print('Submitted:', job['id'], flush=True)
            for followup in (False, True):
                if followup:
                    job = unpack(await session.call_tool('submit_task', {
                        'title': 'MCP session continuity test', 'prompt': 'Какое контрольное слово я просил запомнить? Ответь только этим словом.',
                        'request_key': 'resume-' + str(uuid.uuid4()), 'resume_job_id': job['id'], 'timeout_seconds': 120}))
                    print('Resume submitted:', job['id'], flush=True)
                deadline = time.monotonic() + 140
                while time.monotonic() < deadline:
                    state = unpack(await session.call_tool('task_status', {'job_id': job['id']}))
                    if state['status'] in ('completed', 'failed', 'cancelled', 'interrupted'):
                        result = unpack(await session.call_tool('task_result', {'job_id': job['id']}))
                        print('Result:', json.dumps(result, ensure_ascii=False), flush=True)
                        assert state['status'] == 'completed', state
                        assert ('KEDR-742' if followup else 'DISPATCHER_OK') in result['text'], result
                        break
                    await asyncio.sleep(2)
                else:
                    raise TimeoutError(job['id'])


asyncio.run(main())
