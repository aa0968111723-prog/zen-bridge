"""One model per worker process, with bounded waits and explicit shutdown."""
from __future__ import annotations
import json
import os
import queue
import subprocess
import sys
import threading
from pathlib import Path
from app.asr import AsrResult, ResidentAsr

class NativeResidentAsr(ResidentAsr):
    @staticmethod
    def _discard(stream):
        try:
            for _ in stream:
                pass
        finally:
            stream.close()

    def __init__(self, model, threads=6, startup_timeout_s=180, inference_timeout_s=120,
                 audio_context=0, beam_size=0, best_of=0):
        super().__init__(model=model, threads=threads, startup_timeout_s=startup_timeout_s,
            inference_timeout_s=inference_timeout_s, audio_context=audio_context,
            beam_size=beam_size, best_of=best_of)
        self.messages = queue.Queue()
        self.reader = None
        self.request_lock = threading.Lock()

    def _read_messages(self, stream):
        try:
            for line in stream:
                if line.startswith('BREEZE_RESULT '):
                    self.messages.put(json.loads(line[len('BREEZE_RESULT '):]))
        except (ValueError, OSError):
            pass
        finally:
            self.messages.put({'ok': False, 'error': '本機辨識程序已結束，請重新啟動 App。'})
            stream.close()

    def _receive(self, timeout):
        try:
            return self.messages.get(timeout=timeout)
        except queue.Empty:
            self.close()
            return {'ok': False, 'error': '本機辨識逾時，已停止該程序。請重新啟動 App。'}

    def start(self):
        if self.health():
            return AsrResult(ok=True, loaded_once=True)
        self.close()
        self.messages = queue.Queue()
        root = Path(__file__).resolve().parents[1]
        command = [sys.executable, '-m', 'app.native_worker', '--model', str(self.model.resolve()),
            '--threads', str(self.threads), '--context', str(self.audio_context),
            '--beam', str(self.beam_size), '--best', str(self.best_of)]
        try:
            self.proc = subprocess.Popen(command, cwd=root, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, text=True, encoding='utf-8', errors='replace', bufsize=1,
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

    def transcribe(self, wav, prompt):
        with self.request_lock:
            if not self.health():
                return AsrResult(ok=False, error=self.last_error or '本機模型未就緒，請重新啟動 App。')
            self.calls += 1
            try:
                self.proc.stdin.write(json.dumps({'path': str(Path(wav).resolve()), 'prompt': prompt}, ensure_ascii=True) + '\n')
                self.proc.stdin.flush()
                response = self._receive(self.inference_timeout_s)
                return AsrResult(ok=response.get('ok', False), text=response.get('text', ''),
                    error=response.get('error', ''), loaded_once=self.loads == 1)
            except (OSError, ValueError):
                self.close()
                return AsrResult(ok=False, error='本機辨識程序中斷，請重新啟動 App。')

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
