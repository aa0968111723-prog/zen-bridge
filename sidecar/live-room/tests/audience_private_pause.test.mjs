import assert from 'node:assert/strict';
import { connectRoom } from '../app/static/room_client.js';
const states = [], captions = [], sockets = [];
const connection = connectRoom({
  room: 'class', url: () => 'ws://localhost/ws/listen',
  onState() {}, onEvent: item => captions.push(item), onPause: paused => states.push(paused),
  openSocket: () => {
    const socket = { readyState: 1, send() {}, close() { this.onclose?.(); } };
    sockets.push(socket); return socket;
  },
});
await new Promise(resolve => setTimeout(resolve, 0));
const socket = sockets[0];
socket.onopen();
const message = data => socket.onmessage({ data: JSON.stringify(data) });
message({ type: 'hello', paused: true, history: [], latest_cursor: 0, epoch: 1 });
message({ type: 'resumed' });
message({ type: 'paused' });
assert.deepEqual(states, [true, false, true], 'Reconnecting into a paused room must retain privacy state');
assert.equal(captions.length, 0, 'Pause announcements are not captions');
connection.stop();
console.log('Audience pause state survives hello, pause and resume events.');
