"""One model per worker process, with bounded waits and explicit shutdown."""
from __future__ import annotations
import json
import os
import queue
import subprocess
import sys
import threading
import time
from collections import deque
from pathlib import Path
from app.asr import AsrResult, ResidentAsr
from app.asr_tuning import NativeTuning, default_log_path, rotating_logger
from app.gpu_env import shared_memory_from, worker_env

RESTART_BACKOFF_S = (1.0, 5.0, 30.0)
RESTART_WINDOW_S = 300.0
MAX_RESTARTS = 3


class NativeResidentAsr(ResidentAsr):
    """round3 C2: a crashed or timed-out worker is restarted automatically on the next segment,
    after a backoff of 1 -> 5 -> 30 s since the failure. A 4th failure within 5 minutes marks the
    engine ``degraded`` (host page shows red) and stops restarting until ``reset_degraded()``
    or an explicit ``start()``. The segment that failed is dropped and logged by name only."""
    def _discard(self, stream):
        """Drain worker stderr so a full pipe cannot block the model. With a log configured
        (BREEZE_ASR_LOG, default <data>/logs/asr-worker.log) every line goes to a 5 MB x 3
        rotating file: print_timings, system_info, BREEZE_TIMING. The worker prints no text."""
        sink = self.worker_log
        try:
            for line in stream:
                shared = shared_memory_from(line)
                if shared is not None:
                    self.ggml_shared_memory = shared          # round4 #9 / D7
                    if sink is not None:
                        sink.info("BREEZE_GGML shared_memory=%d", shared)
                if sink is not None:
                    sink.info(line.rstrip())
        finally:
            stream.close()

    def __init__(self, model, threads=6, startup_timeout_s=180, inference_timeout_s=120,
                 audio_context=0, beam_size=0, best_of=0, tuning: NativeTuning | None = None,
                 log_path=None, env=None):
        super().__init__(model=model, threads=threads, startup_timeout_s=startup_timeout_s,
            inference_timeout_s=inference_timeout_s, audio_context=audio_context,
            beam_size=beam_size, best_of=best_of)
        self.messages = queue.Queue()
        self.reader = None
        self.request_lock = threading.Lock()
        self.tuning = tuning or NativeTuning.from_env(env)
        self.worker_log = rotating_logger(log_path if log_path is not None else default_log_path(env))
        self.last_stats = {}
        self.clock = time.monotonic
        self.failures: deque = deque()
        self.degraded = False
        self.restarts = 0
        self._last_failure = None
        self._current_clip = ""
        self.ggml_shared_memory: int | None = None

    def _read_messages(self, stream):
        try:
            for line in stream:
                if line.startswith('BREEZE_RESULT '):
                    self.messages.put(json.loads(line[len('BREEZE_RESULT '):]))
        except (ValueError, OSError):
            pass
        finally:
            self.messages.put({'ok': False, 'worker_exited': True, 'error': '本機辨識程序已結束，請重新啟動 App。'})
            stream.close()

    def _receive(self, timeout):
        try:
            return self.messages.get(timeout=timeout)
        except queue.Empty:
            self.close()
            if self.worker_log is not None and self._current_clip:
                self.worker_log.info("BREEZE_TIMEOUT clip=%s after %.0fs; segment dropped", self._current_clip, timeout)
            return {'ok': False, 'error': '本機辨識逾時，這一段略過；辨識程序會自動重新啟動。'}

    def start(self):
        if self.health():
            return AsrResult(ok=True, loaded_once=True)
        self.close()
        self.messages = queue.Queue()
        root = Path(__file__).resolve().parents[1]
        command = [sys.executable, '-m', 'app.native_worker', '--model', str(self.model.resolve()),
            '--threads', str(self.threads), '--context', str(self.audio_context),
            '--beam', str(self.beam_size), '--best', str(self.best_of)] + self.tuning.argv()
        try:
            self.proc = subprocess.Popen(command, cwd=root, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, text=True, encoding='utf-8', errors='replace', bufsize=1,
                env=worker_env(),        # round4 #9: inherit os.environ + VK layer guard
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
            self.reader = threading.Thread(target=self._read_messages, args=(self.proc.stdout,), daemon=True)
            self.reader.start()
            # Native logs contain diagnostics; discard them after draining so a
            # full pipe cannot block the model. User audio/text is not logged.
            self.output_thread = threading.Thread(target=self._discard, args=(self.proc.stderr,), daemon=True)
            self.output_thread.start()
            response = self._receive(self.startup_timeout_s)
            if not response.get('ready'):
                raise RuntimeError(response.get('error', '模型未就緒'))
            self.ready = True
            self.loads += 1
            self.last_error = ''
            return AsrResult(ok=True, loaded_once=self.loads == 1)
        except (OSError, RuntimeError) as exc:
            self.close()
            self.last_error = str(exc)
            return AsrResult(ok=False, error=self.last_error)

    def health(self):
        return bool(self.ready and self.proc is not None and self.proc.poll() is None)

    # ------------------------------------------------------------------ auto restart (C2)
    def _note_failure(self):
        now = self.clock()
        self._last_failure = now
        self.failures.append(now)
        while self.failures and now - self.failures[0] > RESTART_WINDOW_S:
            self.failures.popleft()
        if len(self.failures) > MAX_RESTARTS:
            self.degraded = True

    def reset_degraded(self):
        self.degraded = False
        self.failures.clear()
        self._last_failure = float("-inf")     # restart on the very next segment

    def restart_status(self) -> dict:
        return {"asr_degraded": self.degraded, "asr_restarts": self.restarts,
                "asr_recent_failures": len(self.failures), "ggml_shared_memory": self.ggml_shared_memory}

    def _try_restart(self) -> AsrResult | None:
        """None = worker healthy again. Otherwise the error for this (dropped) segment."""
        if self.loads == 0:
            return AsrResult(ok=False, error=self.last_error or '本機模型未就緒，請重新啟動 App。')
        if self.degraded:
            return AsrResult(ok=False, error='本機辨識程序 5 分鐘內失敗太多次，已停止自動重啟；請重新啟動 App。')
        if self._last_failure is None:
            self._note_failure()           # died between segments: count it now
        n = max(1, len(self.failures))
        wait = RESTART_BACKOFF_S[min(n, len(RESTART_BACKOFF_S)) - 1]
        left = self._last_failure + wait - self.clock()
        if left > 0:
            return AsrResult(ok=False, error=f'本機辨識程序重新啟動中（{left:.0f} 秒後），這一段略過。')
        loads = self.loads
        started = self.start()
        if not started.ok:
            self._note_failure()
            return AsrResult(ok=False, error=f'本機辨識程序重新啟動失敗：{started.error}')
        self.loads = loads                 # a restart is not a first load
        self.restarts += 1
        self._last_failure = None
        return None

    def transcribe(self, wav, prompt, language='zh'):
        with self.request_lock:
            if not self.health():
                failed = self._try_restart()
                if failed is not None:
                    return failed
            self._current_clip = Path(wav).name
            self.calls += 1
            try:
                self.proc.stdin.write(json.dumps({'path': str(Path(wav).resolve()), 'prompt': prompt, 'language': language}, ensure_ascii=True) + '\n')
                self.proc.stdin.flush()
                response = self._receive(self.inference_timeout_s)
                ok = response.get('ok', False)
                # Windows may deliver pipe EOF before proc.poll() sees the exit.
                # Treat the reader's explicit EOF as fatal before the next clip.
                if not ok and response.get('worker_exited'):
                    self.close()
                if not ok and not self.health():
                    self._note_failure()       # timeout or worker exit: next segment restarts it
                text = response.get('text', '')
                self.last_stats = response.get('stats') or {}
                return AsrResult(ok=ok, text=text, blank=bool(ok and not text.strip()),
                    error=response.get('error', ''), loaded_once=self.loads == 1)
            except (OSError, ValueError):
                self.close()
                self._note_failure()
                return AsrResult(ok=False, error='本機辨識程序中斷，這一段略過；辨識程序會自動重新啟動。')

    def close(self):
        proc = self.proc
        self.proc = None
        self.ready = False
        if proc is not None:
            try:
                if proc.stdin and not proc.stdin.closed:
                    proc.stdin.close()
                proc.wait(timeout=2)
            except (OSError, subprocess.TimeoutExpired):
                proc.kill()
                proc.wait(timeout=5)
        if self.reader:
            self.reader.join(timeout=2)
            self.reader = None
        if self.output_thread:
            self.output_thread.join(timeout=2)
            self.output_thread = None
