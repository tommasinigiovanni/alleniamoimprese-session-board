"""Keep the offline model in a supervised child, without loading it into Flask."""
import atexit
import json
import os
from pathlib import Path
import secrets
import selectors
import signal
import subprocess
import threading
import time

MAX_RESPONSE = 16000 * 4 + 1024
MAX_REQUEST = 8192


class WorkerFailure(ValueError):
    def __init__(self, code='unavailable'):
        self.code = code
        super().__init__(code)


class WarmWorker:
    def __init__(self, worker, *, timeout=150, idle_timeout=600):
        self.worker, self.timeout, self.idle_timeout = Path(worker), timeout, idle_timeout
        self._owner = os.getpid()
        self._lock = threading.Lock()
        self._process = self._key = self._timer = None
        self._generation = 0
        self._preparing = False
        self._last_used = 0

    def _check_owner(self):
        if self._owner == os.getpid():
            return
        # A fork inherits pipes, not ownership of the parent's model process.
        if self._process is not None:
            for stream in (self._process.stdin, self._process.stdout):
                stream.close()
            self._process.returncode = 0
        self._owner = os.getpid()
        self._lock = threading.Lock()
        self._process = self._key = self._timer = None
        self._preparing = False
        self._generation += 1

    def _cancel_idle(self):
        self._generation += 1
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None

    def _stop(self):
        self._cancel_idle()
        process, self._process, self._key = self._process, None, None
        if process is None:
            return
        self._dispose(process)

    @staticmethod
    def _dispose(process):
        try:
            if process.poll() is None:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            process.wait(timeout=2)
        finally:
            for stream in (process.stdin, process.stdout):
                stream.close()

    def close(self):
        self._check_owner()
        # Parent-death protection also terminates the child during forced exits.
        if self._lock.acquire(timeout=2):
            try:
                self._stop()
            finally:
                self._lock.release()

    @staticmethod
    def _remaining(deadline):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise WorkerFailure('timeout')
        return remaining

    def _receive(self, deadline):
        data = bytearray()
        with selectors.DefaultSelector() as selector:
            selector.register(self._process.stdout, selectors.EVENT_READ)
            while True:
                if not selector.select(self._remaining(deadline)):
                    raise WorkerFailure('timeout')
                chunk = os.read(self._process.stdout.fileno(), min(8192, MAX_RESPONSE + 1 - len(data)))
                if not chunk:
                    raise WorkerFailure()
                data.extend(chunk)
                if len(data) > MAX_RESPONSE:
                    raise WorkerFailure()
                if b'\n' in data:
                    line, extra = data.split(b'\n', 1)
                    if extra:
                        raise WorkerFailure()
                    value = json.loads(line)
                    if not isinstance(value, dict):
                        raise WorkerFailure()
                    return value

    def _send(self, value, deadline):
        data = memoryview(json.dumps(value, ensure_ascii=False).encode() + b'\n')
        if len(data) > MAX_REQUEST:
            raise WorkerFailure()
        with selectors.DefaultSelector() as selector:
            selector.register(self._process.stdin, selectors.EVENT_WRITE)
            while data:
                if not selector.select(self._remaining(deadline)):
                    raise WorkerFailure('timeout')
                count = os.write(self._process.stdin.fileno(), data)
                if count <= 0:
                    raise WorkerFailure()
                data = data[count:]

    def _spawn(self, command, environment, deadline):
        # Linux PDEATHSIG follows the creating thread. Keep that thread alive
        # for the child's whole lifetime, including between HTTP requests.
        ready, handoff = threading.Event(), threading.Lock()
        state = {'abandoned': False}

        def own_child():
            process = None
            try:
                process = subprocess.Popen(command, stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, cwd='/',
                    env=environment, start_new_session=True, bufsize=0)
            except Exception:
                pass  # The caller returns a static error without runtime paths.
            with handoff:
                abandoned = state['abandoned']
                if not abandoned:
                    state['process'] = process
                ready.set()
            if process is not None:
                if abandoned:
                    self._dispose(process)
                else:
                    process.wait()

        self._remaining(deadline)
        threading.Thread(target=own_child, daemon=True).start()
        ready.wait(max(0, deadline - time.monotonic()))
        with handoff:
            self._process = state.get('process')
            if self._process is None:
                state['abandoned'] = True
                raise WorkerFailure('unavailable' if ready.is_set() else 'timeout')
        self._remaining(deadline)

    def _ensure(self, settings, deadline):
        key = tuple(settings[name] for name in ('python', 'model', 'language'))
        if self._process is not None and self._process.poll() is None and self._key == key:
            return
        self._stop()
        environment = {
            'PATH': '/usr/local/bin:/usr/bin:/bin', 'LANG': 'C.UTF-8',
            'HF_HUB_OFFLINE': '1', 'HF_HUB_DISABLE_TELEMETRY': '1', 'TRANSFORMERS_OFFLINE': '1',
            'PYTHONNOUSERSITE': '1', 'PYTHONDONTWRITEBYTECODE': '1',
            'OMP_NUM_THREADS': '2', 'OPENBLAS_NUM_THREADS': '2', 'MKL_NUM_THREADS': '2',
            'NUMEXPR_NUM_THREADS': '2', 'TOKENIZERS_PARALLELISM': 'false',
        }
        self._spawn(
            [settings['python'], '-B', str(self.worker), '--model', settings['model'],
             '--language', settings['language'], '--serve', '--parent-pid', str(self._owner)],
            environment, deadline)
        os.set_blocking(self._process.stdin.fileno(), False)
        os.set_blocking(self._process.stdout.fileno(), False)
        if self._receive(deadline) != {'ready': True}:
            raise WorkerFailure()
        self._key = key

    def _arm_idle(self):
        self._cancel_idle()
        self._last_used = time.monotonic()
        self._timer = threading.Timer(self.idle_timeout, self._expire, (self._generation, self._owner))
        self._timer.daemon = True
        self._timer.start()

    def _expire(self, generation, owner):
        if owner != os.getpid():
            return
        with self._lock:
            if generation == self._generation and time.monotonic() - self._last_used >= self.idle_timeout:
                self._stop()

    def prepare(self, settings):
        self._check_owner()
        if not self._lock.acquire(blocking=False):
            return
        try:
            if self._preparing:
                return
            self._preparing = True
            configuration = dict(settings)
            thread = threading.Thread(target=self._warm, args=(configuration,), daemon=True)
            thread.start()
        finally:
            self._lock.release()

    def _warm(self, settings):
        deadline = time.monotonic() + self.timeout
        with self._lock:
            try:
                self._cancel_idle()
                self._ensure(settings, deadline)
                self._arm_idle()
            except (WorkerFailure, OSError, ValueError, subprocess.SubprocessError):
                self._stop()
            finally:
                self._preparing = False

    def request(self, settings, path):
        self._check_owner()
        deadline = time.monotonic() + self.timeout
        if not self._lock.acquire(timeout=self._remaining(deadline)):
            raise WorkerFailure('timeout')
        try:
            self._remaining(deadline)
            self._cancel_idle()
            self._ensure(settings, deadline)
            request_id = secrets.token_hex(16)
            self._send({'id': request_id, 'audio': str(path)}, deadline)
            result = self._receive(deadline)
            if result.pop('id', None) != request_id or set(result) not in ({'text'}, {'error'}):
                raise WorkerFailure()
            if 'error' in result:
                if not isinstance(result['error'], str) or result['error'] not in {'invalid_audio', 'too_long', 'no_speech', 'text_too_long'}:
                    raise WorkerFailure()
            else:
                text = result['text']
                if (not isinstance(text, str) or not text.strip() or len(text) > 16000
                        or any(ord(char) < 32 and char != '\n' or ord(char) == 127 for char in text)):
                    raise WorkerFailure()
            self._arm_idle()
            return result
        except WorkerFailure:
            self._stop()
            raise
        except (OSError, ValueError, subprocess.SubprocessError):
            self._stop()
            raise WorkerFailure() from None
        finally:
            self._lock.release()


worker = WarmWorker(Path(__file__).with_name('audio_worker.py'))
atexit.register(worker.close)
