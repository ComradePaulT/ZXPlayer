# SPDX-License-Identifier: GPL-3.0-or-later
"""Tape decoding and sample-clocked ALSA playback for ZX Player.

The libspectrum public API supplies pulses, block boundaries and stop markers.
No tape data is interpreted by the graphical interface.
"""
from __future__ import annotations

import bisect
import ctypes as C
import ctypes.util
import hashlib
import json
import os
from pathlib import Path
import queue
import threading
import time
import wave

import numpy as np

RATE = 44100
VERSION = 3
EXTENSIONS = {'.tap', '.tzx', '.pzx', '.csw', '.wav', '.spc', '.sta', '.ltp'}
DEVICE = 'plughw:CARD=Headphones,DEV=0'


def atomic_json(path, data):
    path = Path(path)
    temp = path.with_suffix(path.suffix + '.tmp')
    temp.write_text(json.dumps(data, indent=2), encoding='utf-8')
    temp.replace(path)


def cache_paths(source, cache):
    source = Path(source)
    stat = source.stat()
    key = hashlib.sha256(f'{VERSION}|{source.resolve()}|{stat.st_size}|{stat.st_mtime_ns}'.encode()).hexdigest()[:24]
    return Path(cache) / (key + '.pcm'), Path(cache) / (key + '.json')


def block_index(blocks, frame):
    return max(0, bisect.bisect_right([b['frame'] for b in blocks], frame) - 1)


class Spectrum:
    def __init__(self):
        name = os.environ.get('ZXPLAYER_LIBSPECTRUM') or ctypes.util.find_library('spectrum')
        if not name:
            raise RuntimeError('libspectrum is missing. Run the installation commands in START-HERE.txt.')
        self.lib = C.CDLL(name)
        def bind(name, result, args):
            fn = getattr(self.lib, 'libspectrum_' + name)
            fn.restype, fn.argtypes = result, args
            return fn
        ptr = C.c_void_p
        self.init = bind('init', C.c_int, [])
        self.alloc = bind('tape_alloc', ptr, [])
        self.free = bind('tape_free', C.c_int, [ptr])
        self.read = bind('tape_read', C.c_int, [ptr, ptr, C.c_size_t, C.c_int, C.c_char_p])
        self.present = bind('tape_present', C.c_int, [ptr])
        self.edge = bind('tape_get_next_edge', C.c_int, [C.POINTER(C.c_uint32), C.POINTER(C.c_int), ptr])
        self.current = bind('tape_current_block', ptr, [ptr])
        self.position = bind('tape_position', C.c_int, [C.POINTER(C.c_int), ptr])
        self.kind = bind('tape_block_type', C.c_int, [ptr])
        self.describe = bind('tape_block_description', C.c_int, [ptr, C.c_size_t, ptr])
        self.flags = {k: C.c_int.in_dll(self.lib, 'LIBSPECTRUM_TAPE_FLAGS_' + k).value
                      for k in ['BLOCK', 'STOP', 'STOP48', 'NO_EDGE', 'LEVEL_LOW', 'LEVEL_HIGH', 'TAPE']}
        if self.init():
            raise RuntimeError('Could not initialise libspectrum.')


class PulseWriter:
    """Accumulate fractional samples rather than rounding each pulse independently."""
    def __init__(self, output, rate=RATE):
        self.output, self.rate = output, rate
        self.frames = self.remainder = 0
        self.buffer = bytearray()

    def pulse(self, tstates, high):
        count, self.remainder = divmod(self.remainder + int(tstates) * self.rate, 3500000)
        if self.frames + count > self.rate * 7200:
            raise RuntimeError('Tape exceeds the two-hour rendering limit (possibly an endless loop).')
        self.frames += count
        sample = b'\xff\x7f' if high else b'\x00\x80'
        while count:
            n = min(count, 32768)
            self.buffer.extend(sample * n)
            count -= n
            if len(self.buffer) >= 65536:
                self.flush()

    def flush(self):
        self.output.write(self.buffer)
        self.buffer.clear()


def render_spectrum(source, destination, spectrum=None):
    api = spectrum or Spectrum()
    data = Path(source).read_bytes()
    if len(data) > 128 * 1024 * 1024:
        raise RuntimeError('Tape image exceeds the 128 MB limit.')
    buf = C.create_string_buffer(data)
    tape = api.alloc()
    if not tape:
        raise RuntimeError('Could not allocate tape decoder.')
    try:
        if api.read(tape, buf, len(data), 0, os.fsencode(source)) or not api.present(tape):
            raise RuntimeError('This tape is damaged or uses a format unsupported by the installed libspectrum.')
        f = api.flags
        ticks, flags, pos = C.c_uint32(), C.c_int(), C.c_int()
        blocks, stops, stops48 = [], [], []
        level, pending, edges = False, 0, 0
        new_block = True
        with open(destination, 'wb') as out:
            writer = PulseWriter(out)
            while True:
                if new_block:
                    block = api.current(tape)
                    if not block:
                        break
                    kind = api.kind(block)
                    # libspectrum's pulse API silently ignores selection menus.
                    # Reject them rather than emitting the wrong branch.
                    if kind in (0x26, 0x27, 0x28):
                        raise RuntimeError('This tape uses a call/selection block. Interactive tape branches are not supported yet.')
                    description = C.create_string_buffer(256)
                    api.describe(description, len(description), block)
                    api.position(C.byref(pos), tape)
                    start = writer.frames
                    label = f'{pos.value + 1:03d}  ' + description.value.decode('utf-8', 'replace')
                    new_block = False
                if api.edge(C.byref(ticks), C.byref(flags), tape):
                    raise RuntimeError('The tape decoder could not read a pulse. No partial audio will be played.')
                bits = flags.value
                if not bits & f['NO_EDGE']:
                    if bits & f['LEVEL_LOW']:
                        level = False
                    elif bits & f['LEVEL_HIGH']:
                        level = True
                    else:
                        level = not level
                pending += ticks.value
                if not bits & f['NO_EDGE']:
                    writer.pulse(pending, level)
                    pending = 0
                if bits & f['BLOCK']:
                    if writer.frames > start:
                        blocks.append({'frame': start, 'label': label})
                    new_block = True
                if bits & f['STOP'] and not bits & f['TAPE']:
                    stops.append(writer.frames)
                if bits & f['STOP48']:
                    stops48.append(writer.frames)
                if bits & f['TAPE']:
                    break
                edges += 1
                if edges > 100_000_000:
                    raise RuntimeError('Tape execution limit reached; possible endless loop.')
            writer.flush()
        if not writer.frames:
            raise RuntimeError('This tape contains no playable audio.')
        if not blocks:
            blocks = [{'frame': 0, 'label': 'Tape audio'}]
        return {'rate': RATE, 'frames': writer.frames, 'blocks': blocks,
                'stops': sorted(set(stops)), 'stops48': sorted(set(stops48)), 'recording': False}
    finally:
        api.free(tape)


def render_wav(source, destination):
    with wave.open(str(source), 'rb') as wav, open(destination, 'wb') as out:
        width, channels, rate = wav.getsampwidth(), wav.getnchannels(), wav.getframerate()
        if wav.getcomptype() != 'NONE' or width not in (1, 2, 3, 4) or channels < 1:
            raise RuntimeError('Use an uncompressed PCM WAV file (8, 16, 24 or 32 bit).')
        if not 8000 <= rate <= 192000 or wav.getnframes() > rate * 7200:
            raise RuntimeError('WAV must be at most two hours, sampled at 8–192 kHz.')
        frames = 0
        while raw := wav.readframes(32768):
            if width == 1:
                values = (np.frombuffer(raw, dtype=np.uint8).astype(np.int16) - 128) * 256
            elif width == 2:
                values = np.frombuffer(raw, dtype='<i2')
            elif width == 3:
                octets = np.frombuffer(raw, dtype=np.uint8).reshape(-1, 3)
                values = ((octets[:, 2].astype(np.int32) << 8) | octets[:, 1]).astype(np.int16)
            else:
                values = (np.frombuffer(raw, dtype='<i4') >> 16).astype(np.int16)
            # Left channel avoids cancellation in recordings with opposite-phase channels.
            mono = values.reshape(-1, channels)[:, 0].astype('<i2')
            out.write(mono.tobytes())
            frames += len(mono)
    if not frames:
        raise RuntimeError('WAV is empty.')
    return {'rate': rate, 'frames': frames, 'blocks': [{'frame': 0, 'label': 'Start of recording'}],
            'stops': [], 'stops48': [], 'recording': True}


def render(source, cache):
    Path(cache).mkdir(parents=True, exist_ok=True)
    pcm, index = cache_paths(source, cache)
    if pcm.exists() and index.exists():
        meta = json.loads(index.read_text())
        if pcm.stat().st_size == meta['frames'] * 2:
            return pcm, meta
    temp = pcm.with_suffix('.partial')
    try:
        meta = render_wav(source, temp) if Path(source).suffix.lower() == '.wav' else render_spectrum(source, temp)
        meta.update(version=VERSION, source=str(Path(source).resolve()))
        temp.replace(pcm)
        atomic_json(index, meta)
        return pcm, meta
    finally:
        temp.unlink(missing_ok=True)


class AlsaDevice:
    def __init__(self, name, rate):
        self.name = name
        self.lib = C.CDLL(ctypes.util.find_library('asound') or 'libasound.so.2')
        self.handle = C.c_void_p()
        signatures = {
            'snd_pcm_open': (C.c_int, [C.POINTER(C.c_void_p), C.c_char_p, C.c_int, C.c_int]),
            'snd_pcm_set_params': (C.c_int, [C.c_void_p, C.c_int, C.c_int, C.c_uint, C.c_uint, C.c_int, C.c_uint]),
            'snd_pcm_writei': (C.c_long, [C.c_void_p, C.c_void_p, C.c_ulong]),
            'snd_pcm_delay': (C.c_int, [C.c_void_p, C.POINTER(C.c_long)]),
            'snd_pcm_prepare': (C.c_int, [C.c_void_p]),
            'snd_pcm_state': (C.c_int, [C.c_void_p]),
            'snd_pcm_start': (C.c_int, [C.c_void_p]),
            'snd_pcm_drop': (C.c_int, [C.c_void_p]),
            'snd_pcm_close': (C.c_int, [C.c_void_p]),
            'snd_strerror': (C.c_char_p, [C.c_int]),
        }
        for fn, (result, args) in signatures.items():
            getattr(self.lib, fn).restype = result
            getattr(self.lib, fn).argtypes = args
        self.check(self.lib.snd_pcm_open(C.byref(self.handle), name.encode(), 0, 1), 'opening output')
        try:
            # S16_LE, RW_INTERLEAVED, mono, nominal 80 ms buffer. Nonblocking writes.
            self.check(self.lib.snd_pcm_set_params(self.handle, 2, 3, 1, rate, 1, 80000), 'setting audio format')
        except Exception:
            self.close()
            raise

    def check(self, code, operation='audio operation'):
        if code < 0:
            detail = ('Driver does not support this operation' if code == -524 else
                      self.lib.snd_strerror(code).decode('utf-8', 'replace'))
            raise RuntimeError(f'{self.name}: {operation}: {detail} (ALSA {code})')

    def write(self, data):
        buf = C.create_string_buffer(data)
        n = self.lib.snd_pcm_writei(self.handle, buf, len(data) // 2)
        if n == -11:  # EAGAIN: wait for the device without blocking control messages.
            return 0
        self.check(n, 'writing samples')
        return n

    def delay(self):
        delay = C.c_long()
        result = self.lib.snd_pcm_delay(self.handle, C.byref(delay))
        # Once the queue has completely drained ALSA may report EPIPE.
        if result == -32:
            return 0
        self.check(result, 'reading playback position')
        return max(0, delay.value)

    def reset(self):
        self.check(self.lib.snd_pcm_drop(self.handle), 'stopping output')
        self.check(self.lib.snd_pcm_prepare(self.handle), 'preparing output')

    def finish_queued(self):
        # A short final segment may never reach ALSA's automatic start threshold.
        if self.lib.snd_pcm_state(self.handle) == 2:  # PREPARED
            self.check(self.lib.snd_pcm_start(self.handle), 'starting short audio segment')

    def close(self):
        if self.handle:
            self.lib.snd_pcm_drop(self.handle)
            self.lib.snd_pcm_close(self.handle)
            self.handle = C.c_void_p()


class AudioPlayer:
    """Only this worker touches ALSA. Position is submitted frames minus device delay."""
    def __init__(self, device=DEVICE, volume=100, model48=True, device_factory=AlsaDevice):
        self.device_name, self.volume, self.model48 = device, volume, model48
        self.factory = device_factory
        self.commands = queue.Queue()
        self.state = {'playing': False, 'frame': 0, 'message': 'Select a tape', 'error': ''}
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def send(self, command, value=None):
        self.commands.put((command, value))

    def close(self):
        self.send('quit')
        self.thread.join(timeout=3)

    def _run(self):
        device = stream = meta = None
        sent = 0
        playing = False
        stops = []
        stop_cursor = 0
        endpoint = 0

        def halt():
            nonlocal sent, playing
            if device and playing:
                sent = max(0, sent - device.delay())
                device.reset()
            playing = False

        def choose_endpoint():
            return stops[stop_cursor] if stop_cursor < len(stops) else meta['frames']

        try:
            while True:
                try:
                    cmd, value = self.commands.get(timeout=0.003 if playing else 0.05)
                except queue.Empty:
                    cmd, value = None, None
                try:
                    if cmd == 'quit':
                        return
                    if cmd == 'load':
                        halt()
                        if device:
                            device.close()
                            device = None
                        if stream:
                            stream.close()
                            stream = None
                        path, meta = value
                        stream = open(path, 'rb')
                        sent = 0
                        stops = sorted(set(meta['stops'] + (meta['stops48'] if self.model48 else [])))
                        stop_cursor = 0
                        self.state = dict(playing=False, frame=0, message='Ready - press Play', error='')
                    elif cmd == 'volume':
                        self.volume = max(0, min(100, int(value)))
                    elif cmd == 'device':
                        halt()
                        if device:
                            device.close()
                        device = None
                        self.device_name = value
                        self.state['message'] = 'Output changed - press Play'
                    elif cmd == 'model48':
                        halt()
                        self.model48 = bool(value)
                        if meta:
                            stops = sorted(set(meta['stops'] + (meta['stops48'] if self.model48 else [])))
                            stop_cursor = bisect.bisect_right(stops, sent)
                    elif cmd == 'seek' and meta:
                        halt()
                        sent = max(0, min(int(value), meta['frames']))
                        stop_cursor = bisect.bisect_left(stops, sent)
                        self.state['message'] = 'Position selected - press Play'
                    elif cmd == 'toggle' and meta:
                        if playing:
                            halt()
                            self.state['message'] = 'Paused'
                        else:
                            if sent >= meta['frames']:
                                sent, stop_cursor = 0, 0
                            if not device:
                                device = self.factory(self.device_name, meta['rate'])
                            endpoint = choose_endpoint()
                            playing = True
                            self.state['error'] = ''
                            self.state['message'] = 'Playing'
                    if playing:
                        queued = device.delay()
                        if sent >= endpoint:
                            if queued:
                                device.finish_queued()
                            if queued == 0:
                                device.reset()
                                playing = False
                                if stop_cursor < len(stops):
                                    stop_cursor += 1
                                    self.state['message'] = 'Tape stop - press Play to continue'
                                else:
                                    self.state['message'] = 'End of tape'
                        else:
                            n = min(1024, endpoint - sent)
                            stream.seek(sent * 2)
                            data = stream.read(n * 2)
                            if len(data) != n * 2:
                                raise RuntimeError('Cached audio is incomplete. Re-select the tape to rebuild it.')
                            if self.volume != 100:
                                data = (np.frombuffer(data, '<i2').astype(np.int32) * self.volume // 100).astype('<i2').tobytes()
                            sent += device.write(data)
                        frame = max(0, sent - device.delay()) if playing else sent
                    else:
                        frame = sent
                    self.state = {**self.state, 'playing': playing, 'frame': frame}
                except Exception as exc:
                    # Do not silently recover an underrun: the loader has lost timing.
                    playing = False
                    if device:
                        device.close()
                        device = None
                    self.state = dict(playing=False, frame=sent, message='Audio stopped',
                                      error=f'{exc}. Retry from the start of this block.')
        finally:
            if device:
                device.close()
            if stream:
                stream.close()
