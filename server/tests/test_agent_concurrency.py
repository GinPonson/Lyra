"""用被闸门暂停的模型验证真实 HTTP、WebSocket 与取消能同时工作。"""

from concurrent.futures import ThreadPoolExecutor
from threading import Event

from test_agent_turn_persistence import SESSION_ID, _auth, _with_revision, wired as _turn_wired

from lyra.domain.agent.turn_stream import MessageDelta, MessageSnapshot, TurnSettled
from lyra.infrastructure.db.agent_session_repository import DuckDbAgentSessionRepository

# 复用真实数据库与设备认证夹具，避免这里另造一套应用装配。
wired = _turn_wired


class GatedExecutor:
    """先发可见文本，再等待测试释放；等待期间必须能够访问其它端点。

    @param started: 第一段内容已经交给编排器后置位。
    @param release: 测试允许模型继续生成时置位。
    """

    def __init__(self, started: Event, release: Event) -> None:
        self.started = started
        self.release = release

    def run(self, *, session, content, context, emit) -> None:
        """产生两个分片，超时则明确失败，避免测试线程永久等待。"""
        emit(MessageDelta(delta="已经生成的内容"))
        self.started.set()
        if not self.release.wait(timeout=10):
            raise TimeoutError("测试没有释放模型执行闸门")
        emit(MessageDelta(delta="后续内容"))
        emit(MessageSnapshot(content="已经生成的内容后续内容", stop_reason="stop"))
        emit(TurnSettled())


def test_generation_does_not_block_http_or_live_events(wired) -> None:
    """模型尚未结束时，真实连接能收到事件、查询会话并拒绝第二个回合。"""
    client, issued, _, database = wired
    started, release = Event(), Event()
    client.app.state.agent_executor = GatedExecutor(started, release)
    row = DuckDbAgentSessionRepository(database.connection).load_session(SESSION_ID)
    assert row is not None
    client.app.state.agent_orchestrator.register(
        # 与生产路径一致的登记方式：设备与用户都来自会话行。
        _with_revision(row, row.revision), device_id=issued.device_id,
        user_id=row.user_id,
    )
    with ThreadPoolExecutor(max_workers=3) as pool:
        try:
            with client.websocket_connect("/v1/events", headers=_auth(issued)) as socket:
                socket.send_json({
                    "type": "subscribe", "sessionId": SESSION_ID, "afterSequence": 0,
                })
                assert socket.receive_json()["mode"] == "events"
                turn = pool.submit(
                    client.post, f"/v1/agent/sessions/{SESSION_ID}/messages",
                    headers=_auth(issued), json={"content": "第一次提问"},
                )
                assert started.wait(timeout=3)
                first = pool.submit(socket.receive_json).result(timeout=3)
                second = pool.submit(socket.receive_json).result(timeout=3)
                assert [first["event"]["type"], second["event"]["type"]] == [
                    "message.started", "message.delta",
                ]
                assert first["event"]["sequence"] < second["event"]["sequence"]
                snapshot = pool.submit(
                    client.get, f"/v1/agent/sessions/{SESSION_ID}", headers=_auth(issued)
                ).result(timeout=3)
                assert snapshot.status_code == 200
                assert snapshot.json()["session"]["status"] == "running"
                duplicate = pool.submit(
                    client.post, f"/v1/agent/sessions/{SESSION_ID}/messages",
                    headers=_auth(issued), json={"content": "重叠提问"},
                ).result(timeout=3)
                assert duplicate.status_code == 409
                assert not turn.done()
                release.set()
                assert turn.result(timeout=3).status_code == 200
        finally:
            release.set()


def test_cancel_persists_without_waiting_for_generation(wired) -> None:
    """取消及时返回 cancelling，下一分片中止执行并持久化唯一的取消终态。"""
    client, issued, _, database = wired
    started, release = Event(), Event()
    client.app.state.agent_executor = GatedExecutor(started, release)
    with ThreadPoolExecutor(max_workers=2) as pool:
        try:
            turn = pool.submit(
                client.post, f"/v1/agent/sessions/{SESSION_ID}/messages",
                headers=_auth(issued), json={"content": "会被停止的问题"},
            )
            assert started.wait(timeout=3)
            cancellation = pool.submit(
                client.post, f"/v1/agent/sessions/{SESSION_ID}/cancel", headers=_auth(issued)
            ).result(timeout=3)
            assert cancellation.status_code == 200
            assert cancellation.json()["session"]["status"] == "cancelling"
            assert not turn.done()
            release.set()
            completed = turn.result(timeout=3)
            assert completed.status_code == 200, completed.text
            result = completed.json()
            assert result["session"]["status"] == "idle"
            assert result["events"][-1]["payload"]["outcome"] == "cancelled"
            persisted = database.connection.execute(
                "SELECT status, content FROM agent_messages "
                "WHERE session_id = CAST(? AS UUID) AND role = 'assistant'", [SESSION_ID]
            ).fetchall()
            assert persisted == [("cancelled", "已经生成的内容")]
        finally:
            release.set()
