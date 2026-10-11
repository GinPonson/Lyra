import assert from "node:assert/strict";
import { describe, it } from "node:test";

import {
  AgentEventSequencer,
  areAgentEventsContiguous,
  planAgentEventResume,
} from "../src/domain/agent/index.js";

describe("AgentEventSequencer", () => {
  it("为同一会话生成严格递增的统一事件", () => {
    const sequencer = new AgentEventSequencer("session-1");
    const started = sequencer.next({
      type: "message.started",
      messageId: "message-1",
      occurredAt: "2026-08-30T00:00:00Z",
      payload: { provider: "genkit" },
    });
    const delta = sequencer.next({
      type: "message.delta",
      messageId: "message-1",
      occurredAt: "2026-08-30T00:00:01Z",
      payload: { text: "当前" },
    });

    assert.equal(started.sequence, 1);
    assert.equal(delta.sequence, 2);
    assert.equal(delta.sessionId, "session-1");
    assert.equal(areAgentEventsContiguous(started, delta), true);
  });

  it("从持久化序号继续，并能识别跨会话或缺号事件", () => {
    const first = new AgentEventSequencer("session-1", 40).next({
      type: "message.completed",
      messageId: "message-1",
      occurredAt: "2026-08-30T00:01:00Z",
      payload: {},
    });
    const gap = new AgentEventSequencer("session-1", 42).next({
      type: "session.completed",
      messageId: null,
      occurredAt: "2026-08-30T00:01:01Z",
      payload: {},
    });
    const otherSession = new AgentEventSequencer("session-2", 41).next({
      type: "session.completed",
      messageId: null,
      occurredAt: "2026-08-30T00:01:01Z",
      payload: {},
    });

    assert.equal(first.sequence, 41);
    assert.equal(gap.sequence, 43);
    assert.equal(areAgentEventsContiguous(first, gap), false);
    assert.equal(areAgentEventsContiguous(first, otherSession), false);
  });

  it("缓冲区完整覆盖客户端游标时只续传连续后缀", () => {
    const sequencer = new AgentEventSequencer("session-resume", 7);
    const events = [
      sequencer.next({
        type: "message.delta",
        messageId: "message-1",
        occurredAt: "2026-08-31T05:00:00Z",
        payload: { text: "A" },
      }),
      sequencer.next({
        type: "message.completed",
        messageId: "message-1",
        occurredAt: "2026-08-31T05:00:01Z",
        payload: {},
      }),
    ];

    assert.deepEqual(planAgentEventResume({
      sessionId: "session-resume",
      afterSequence: 7,
      lastPersistedSequence: 9,
      bufferedEvents: events,
    }), { mode: "events", events });
    assert.deepEqual(planAgentEventResume({
      sessionId: "session-resume",
      afterSequence: 9,
      lastPersistedSequence: 9,
      bufferedEvents: events,
    }), { mode: "events", events: [] });
  });

  it("事件缺口、客户端游标超前或混入其他会话时要求重新获取快照", () => {
    const event = new AgentEventSequencer("session-resume", 8).next({
      type: "message.completed",
      messageId: "message-1",
      occurredAt: "2026-08-31T05:00:01Z",
      payload: {},
    });
    assert.deepEqual(planAgentEventResume({
      sessionId: "session-resume",
      afterSequence: 7,
      lastPersistedSequence: 9,
      bufferedEvents: [event],
    }), { mode: "snapshot_required", reason: "event_gap" });
    assert.deepEqual(planAgentEventResume({
      sessionId: "session-resume",
      afterSequence: 10,
      lastPersistedSequence: 9,
      bufferedEvents: [],
    }), { mode: "snapshot_required", reason: "client_ahead" });
    assert.deepEqual(planAgentEventResume({
      sessionId: "session-resume",
      afterSequence: 7,
      lastPersistedSequence: 9,
      bufferedEvents: [{ ...event, sessionId: "session-other" }],
    }), { mode: "snapshot_required", reason: "mixed_session" });
  });
});
