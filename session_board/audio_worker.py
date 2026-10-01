"""Standalone offline dictation worker. No application or state imports."""
import argparse
from contextlib import contextmanager, redirect_stderr, redirect_stdout
import json
import math
import os
from pathlib import Path
import resource
import select
import signal
import sys

MAX_SECONDS = 120
SAMPLE_RATE = 16000
MAX_SAMPLES = MAX_SECONDS * SAMPLE_RATE
MAX_TEXT = 16000
MAX_REQUEST = 8192
IDLE_SECONDS = 600
_FORMATS = 'matroska,webm,mov,mp4,m4a,3gp,3g2,mj2,ogg,wav'
_EXPECTED_ERRORS = frozenset({'invalid_audio', 'too_long', 'no_speech', 'text_too_long'})


class WorkerError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def _limits():
    resource.setrlimit(resource.RLIMIT_AS, (4 * 1024 ** 3, 4 * 1024 ** 3))
    signal.signal(signal.SIGXCPU, signal.SIG_DFL)
    usage = resource.getrusage(resource.RUSAGE_SELF)
    resource.setrlimit(resource.RLIMIT_CPU,
                       (math.ceil(usage.ru_utime + usage.ru_stime) + 150, resource.RLIM_INFINITY))
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))


def _parent_guard(expected):
    """Do not leave an inference running when the application process dies."""
    import ctypes
    expected = os.getppid() if expected is None else expected
    libc = ctypes.CDLL(None, use_errno=True)
    if expected <= 1 or libc.prctl(1, signal.SIGKILL, 0, 0, 0) != 0 or os.getppid() != expected:
        raise WorkerError('unavailable')


@contextmanager
def _quiet_runtime():
    # The worker's stdout is an exclusive JSON-lines channel. Third-party
    # diagnostics may contain input details, so neither output stream is kept.
    with open(os.devnull, 'w') as sink, redirect_stdout(sink), redirect_stderr(sink):
        yield


def _emit(value):
    print(json.dumps(value, ensure_ascii=False), flush=True)


def decode_audio(path):
    """Bound PCM samples before inference; never follow a media playlist."""
    import av
    import numpy as np
    chunks, total = [], 0

    def append(frame):
        nonlocal total
        total += frame.samples
        if total > MAX_SAMPLES:
            raise WorkerError('too_long')
        chunks.append(frame.to_ndarray().reshape(-1))

    try:
        with open(path, 'rb') as source:
            with av.open(source, mode='r', options={'protocol_whitelist': 'pipe',
                                                   'format_whitelist': _FORMATS}) as container:
                if not set(container.format.name.split(',')).issubset(set(_FORMATS.split(','))):
                    raise WorkerError('invalid_audio')
                if len(container.streams.audio) != 1:
                    raise WorkerError('invalid_audio')
                resampler = av.AudioResampler(format='s16', layout='mono', rate=SAMPLE_RATE)
                for frame in container.decode(container.streams.audio[0]):
                    if not frame.sample_rate or frame.samples > frame.sample_rate * MAX_SECONDS:
                        raise WorkerError('too_long')
                    for decoded in resampler.resample(frame):
                        append(decoded)
                for decoded in resampler.resample(None):
                    append(decoded)
        if not total:
            raise WorkerError('invalid_audio')
        return np.concatenate(chunks).astype(np.float32) / 32768.0
    except WorkerError:
        raise
    except Exception:
        raise WorkerError('invalid_audio') from None


def _load_model(model_path):
    from faster_whisper import WhisperModel
    root = Path(model_path)
    # Prevent faster-whisper's tokenizer fallback from reaching the network.
    if not root.is_absolute() or not all((root / name).is_file()
                                        for name in ('model.bin', 'config.json', 'tokenizer.json')):
        raise WorkerError('unavailable')
    return WhisperModel(str(root), device='cpu', compute_type='int8', cpu_threads=2,
                        num_workers=1, local_files_only=True)


def _transcribe_model(model, audio, language):
    segments, _ = model.transcribe(audio, language=None if language == 'auto' else language,
                                    vad_filter=True, beam_size=1, best_of=1,
                                    condition_on_previous_text=False)
    parts, length = [], 0
    for segment in segments:
        text = segment.text.strip()
        length += len(text) + 1
        if length > MAX_TEXT:
            raise WorkerError('text_too_long')
        parts.append(text)
    text = ' '.join(parts).strip()
    if not text:
        raise WorkerError('no_speech')
    return text


def transcribe(model_path, audio, language):
    return _transcribe_model(_load_model(model_path), audio, language)


def _request_pairs(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise WorkerError('unavailable')
        value[key] = item
    return value


def _requests():
    """Read bounded byte frames without buffered-stdin/select deadlocks."""
    pending = bytearray()
    descriptor = sys.stdin.fileno()
    while True:
        newline = pending.find(b'\n')
        if newline >= 0:
            if newline + 1 > MAX_REQUEST:
                raise WorkerError('unavailable')
            raw = bytes(pending[:newline])
            del pending[:newline + 1]
            value = json.loads(raw.decode('utf-8'), object_pairs_hook=_request_pairs)
            if (not isinstance(value, dict) or set(value) != {'id', 'audio'}
                    or not isinstance(value['id'], str) or not 1 <= len(value['id']) <= 128
                    or any(ord(char) < 32 for char in value['id'])
                    or not isinstance(value['audio'], str) or not value['audio']
                    or '\0' in value['audio'] or not Path(value['audio']).is_absolute()):
                raise WorkerError('unavailable')
            yield value
            continue
        if len(pending) >= MAX_REQUEST:
            raise WorkerError('unavailable')
        if not select.select([descriptor], [], [], IDLE_SECONDS)[0]:
            if pending:
                raise WorkerError('unavailable')
            return
        chunk = os.read(descriptor, min(4096, MAX_REQUEST - len(pending)))
        if not chunk:
            if pending:
                raise WorkerError('unavailable')
            return
        pending.extend(chunk)


def _serve_request(model, request, language):
    # Keep decoded samples, segment generators and text scoped to one call.
    # The model has no transcript history (condition_on_previous_text=False).
    try:
        _limits()
        with _quiet_runtime():
            audio = decode_audio(request['audio'])
            text = _transcribe_model(model, audio, language)
        _emit({'id': request['id'], 'text': text})
        return True
    except WorkerError as error:
        expected = error.code in _EXPECTED_ERRORS
        _emit({'id': request['id'], 'error': error.code if expected else 'unavailable'})
        return expected
    except Exception:
        _emit({'id': request['id'], 'error': 'unavailable'})
        return False


def _serve(model_path, language):
    with _quiet_runtime():
        model = _load_model(model_path)
    _emit({'ready': True})
    for request in _requests():
        if not _serve_request(model, request, language):
            return 2
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument('--model', required=True)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--audio')
    mode.add_argument('--serve', action='store_true')
    parser.add_argument('--parent-pid', type=int)
    parser.add_argument('--language', default='it')
    args = parser.parse_args(argv)
    try:
        if args.serve:
            _parent_guard(args.parent_pid)
        _limits()
        os.environ.update(HF_HUB_OFFLINE='1', HF_HUB_DISABLE_TELEMETRY='1',
                          TRANSFORMERS_OFFLINE='1', TOKENIZERS_PARALLELISM='false')
        if args.serve:
            return _serve(args.model, args.language)
        with _quiet_runtime():
            audio = decode_audio(args.audio)
            text = transcribe(args.model, audio, args.language)
        _emit({'text': text})
        return 0
    except WorkerError as error:
        _emit({'error': error.code if error.code in _EXPECTED_ERRORS else 'unavailable'})
    except Exception:
        _emit({'error': 'unavailable'})
    return 2


if __name__ == '__main__':
    sys.exit(main())
