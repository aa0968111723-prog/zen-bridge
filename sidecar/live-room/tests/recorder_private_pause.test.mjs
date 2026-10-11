import assert from 'node:assert/strict';
import { createCaptureController } from '../app/static/recorder_machine.js';
let uploads = 0, micOpens = 0, stopped = 0, aborted = 0;
const ctl = createCaptureController({
  periodMs: 60000,
  newId: () => 'same-session', roomId: () => 'class',
  openMic: async () => { micOpens++; return { getTracks: () => [{ stop() { stopped++; } }] }; },
  createRecorder: () => {
    const handlers = {};
    return { state: 'inactive', addEventListener(name, callback) { handlers[name] = callback; },
      start() { this.state = 'recording'; }, stop() { this.state = 'inactive';
        handlers.dataavailable?.({ data: { size: 100 } }); handlers.stop?.(); } };
  },
  upload: async () => { uploads++; },
  abortUploads: () => { aborted++; },
});
await ctl.start();
const originalSession = ctl.session;
await ctl.pause();
assert.equal(ctl.state, 'paused');
assert.equal(stopped, 1, 'Privacy pause must release the microphone');
assert.equal(aborted, 1);
assert.equal(uploads, 0, 'The partial private slice must never be uploaded');
await assert.rejects(() => ctl.start(), /已經在聽/);
await ctl.resume();
assert.equal(ctl.session, originalSession, 'Resume continues the existing course');
assert.equal(micOpens, 2, 'Resume acquires a fresh microphone stream');
assert.equal(ctl.lastSeq, 2);
await ctl.stop();
assert.equal(uploads, 1, 'Only the post-resume slice may upload');
await ctl.start();
await ctl.pause();
await ctl.stop();
assert.equal(ctl.state, 'idle');
assert.equal(uploads, 1, 'Stopping from private pause must not flush a secret slice');
console.log('Privacy pause closes capture, drops audio and safely resumes the same course.');
