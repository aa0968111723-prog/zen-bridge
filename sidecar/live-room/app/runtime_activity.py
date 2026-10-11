"""Track direct desktop inference as well as the live caption pipeline."""
from contextlib import contextmanager
import threading
import time

_lock=threading.Lock()
_active={'transcribe':0,'translate':0}
_last=time.monotonic()


@contextmanager
def work(kind):
    global _last
    if kind not in _active:
        raise ValueError('Unknown desktop activity')
    with _lock:
        _active[kind]+=1
        _last=time.monotonic()
    try:
        yield
    finally:
        with _lock:
            _active[kind]-=1
            _last=time.monotonic()


def snapshot():
    with _lock:
        return {'desktop_inflight':_active['transcribe'],
                'desktop_translate_busy':_active['translate'],
                'desktop_idle_s':max(0.0,time.monotonic()-_last)}
