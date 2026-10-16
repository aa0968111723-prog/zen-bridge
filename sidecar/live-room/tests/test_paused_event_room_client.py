"""Verify room_client.js handles paused / resumed (queue item 29).

Production already wires onPause for:
- WS messages with type "paused" / "resumed"
- hello.paused === true on (re)connect

These tests only assert that wiring (static source + node behavioral).
No production edits. Stdlib + optional node; no network / port 8645.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
ROOM_CLIENT = ROOT / "app" / "static" / "room_client.js"


@pytest.fixture(scope="module")
def room_client_src() -> str:
    assert ROOM_CLIENT.is_file(), f"missing {ROOM_CLIENT}"
    return ROOM_CLIENT.read_text(encoding="utf-8")


def test_room_client_exports_connect_room_with_on_pause(room_client_src: str):
    assert "export function connectRoom(" in room_client_src
    assert "onPause" in room_client_src
    # Parameter list includes onPause (not only a comment).
    assert "onExpire, onPause, onDraft" in room_client_src or (
        "onPause" in room_client_src
        and "function connectRoom({" in room_client_src
    )


def test_deliver_routes_paused_and_resumed_to_on_pause(room_client_src: str):
    """deliver() must call onPause(true|false) and must not treat them as captions."""
    assert 'data.type === "paused" || data.type === "resumed"' in room_client_src
    assert "onPause(data.type === \"paused\")" in room_client_src
    # paused/resumed return early from deliver (before onEvent).
    deliver_idx = room_client_src.index("function deliver(data)")
    pause_idx = room_client_src.index(
        'data.type === "paused" || data.type === "resumed"', deliver_idx
    )
    return_idx = room_client_src.index("return;", pause_idx)
    on_event_idx = room_client_src.index("onEvent(data)", deliver_idx)
    assert pause_idx < return_idx < on_event_idx


def test_hello_reports_paused_flag_via_on_pause(room_client_src: str):
    """Live hello must surface hello.paused so reconnect into a paused room stays private."""
    assert "onPause(data.paused === true)" in room_client_src
    hello_idx = room_client_src.index('data.type === "hello"')
    pause_hello_idx = room_client_src.index("onPause(data.paused === true)", hello_idx)
    assert pause_hello_idx > hello_idx


def _node_bin() -> str:
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed")
    return node


def _run_pause_script(script: str) -> dict:
    """Run an ESM snippet that imports connectRoom; returns parsed JSON stdout."""
    proc = subprocess.run(
        [_node_bin(), "--input-type=module", "-e", script],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
        check=False,
    )
    assert proc.returncode == 0, proc.stdout + "\n" + proc.stderr
    lines = [ln for ln in (proc.stdout or "").splitlines() if ln.strip()]
    assert lines, "expected JSON line from node script"
    return json.loads(lines[-1])


def _pause_probe_script() -> str:
    client_uri = ROOM_CLIENT.as_uri()
    return f"""
import {{ connectRoom }} from {json.dumps(client_uri)};

const states = [];
const captions = [];
const sockets = [];

const connection = connectRoom({{
  room: "class",
  url: () => "ws://localhost/ws/listen",
  onState() {{}},
  onEvent: (item) => captions.push(item),
  onPause: (paused) => states.push(paused),
  openSocket: () => {{
    const socket = {{
      readyState: 1,
      send() {{}},
      close() {{ this.onclose?.(); }},
    }};
    sockets.push(socket);
    return socket;
  }},
}});

await new Promise((resolve) => setTimeout(resolve, 0));
const socket = sockets[0];
socket.onopen();
const message = (data) => socket.onmessage({{ data: JSON.stringify(data) }});

message({{ type: "hello", paused: true, history: [], latest_cursor: 0, epoch: 1 }});
message({{ type: "resumed" }});
message({{ type: "paused" }});
message({{
  type: "caption",
  id: "c1",
  seq: 1,
  version: 1,
  cursor: 1,
  zh: "可見",
  session_id: "s",
}});
message({{ type: "resumed" }});

connection.stop();
console.log(JSON.stringify({{ states, captionCount: captions.length, captionIds: captions.map((c) => c.id) }}));
"""


def test_connect_room_on_pause_survives_hello_resume_and_pause():
    """Behavioral: hello.paused, resumed, paused → onPause sequence; not captions."""
    result = _run_pause_script(_pause_probe_script())
    assert result["states"] == [True, False, True, False], result
    assert result["captionCount"] == 1, result
    assert result["captionIds"] == ["c1"], result


def test_paused_and_resumed_events_are_not_delivered_as_captions():
    client_uri = ROOM_CLIENT.as_uri()
    script = f"""
import {{ connectRoom }} from {json.dumps(client_uri)};
const captions = [];
const sockets = [];
const connection = connectRoom({{
  room: "class",
  url: () => "ws://localhost/ws/listen",
  onState() {{}},
  onEvent: (item) => captions.push(item),
  onPause() {{}},
  openSocket: () => {{
    const socket = {{ readyState: 1, send() {{}}, close() {{ this.onclose?.(); }} }};
    sockets.push(socket);
    return socket;
  }},
}});
await new Promise((r) => setTimeout(r, 0));
const socket = sockets[0];
socket.onopen();
const message = (data) => socket.onmessage({{ data: JSON.stringify(data) }});
message({{ type: "hello", paused: false, history: [], latest_cursor: 0, epoch: 1 }});
message({{ type: "paused" }});
message({{ type: "resumed" }});
message({{ type: "paused", id: "trap", seq: 9, version: 1, cursor: 9, zh: "不應出現" }});
connection.stop();
console.log(JSON.stringify({{ captions }}));
"""
    result = _run_pause_script(script)
    assert result["captions"] == [], result


def test_hello_without_paused_reports_false_to_on_pause():
    client_uri = ROOM_CLIENT.as_uri()
    script = f"""
import {{ connectRoom }} from {json.dumps(client_uri)};
const states = [];
const sockets = [];
const connection = connectRoom({{
  room: "class",
  url: () => "ws://localhost/ws/listen",
  onState() {{}},
  onEvent() {{}},
  onPause: (paused) => states.push(paused),
  openSocket: () => {{
    const socket = {{ readyState: 1, send() {{}}, close() {{ this.onclose?.(); }} }};
    sockets.push(socket);
    return socket;
  }},
}});
await new Promise((r) => setTimeout(r, 0));
const socket = sockets[0];
socket.onopen();
socket.onmessage({{
  data: JSON.stringify({{ type: "hello", history: [], latest_cursor: 0, epoch: 2 }}),
}});
connection.stop();
console.log(JSON.stringify({{ states }}));
"""
    result = _run_pause_script(script)
    assert result["states"] == [False], result
