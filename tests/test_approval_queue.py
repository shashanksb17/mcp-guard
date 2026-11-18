import asyncio

import pytest

from mcp_guard.daemon.approval_queue import ApprovalQueue
from mcp_guard.models import ApprovalRequestCreate, Decision


@pytest.mark.asyncio
async def test_queue_decide_allow():
    q = ApprovalQueue(timeout_seconds=5, fail_closed=True)

    async def submit():
        return await q.submit_and_wait(
            ApprovalRequestCreate(
                server_name="s",
                tool_name="t",
                arguments={"a": 1},
                transport="stdio",
            )
        )

    task = asyncio.create_task(submit())
    for _ in range(50):
        pending = await q.list_pending()
        if pending:
            break
        await asyncio.sleep(0.02)
    assert pending
    result = await q.decide(pending[0].id, Decision.ALLOW, decided_by="test")
    waited = await task
    assert result.decision == Decision.ALLOW
    assert waited.decision == Decision.ALLOW


@pytest.mark.asyncio
async def test_queue_timeout_fail_closed():
    q = ApprovalQueue(timeout_seconds=1, fail_closed=True)
    result = await q.submit_and_wait(
        ApprovalRequestCreate(
            server_name="s",
            tool_name="t",
            arguments={},
            transport="http",
        )
    )
    assert result.timed_out is True
    assert result.decision == Decision.TIMEOUT
