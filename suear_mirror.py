#!/usr/bin/env python3
# Author: Sean Pesce

# The following project was used as a reference implementation for the HTTP-based MPJEG stream:
# https://github.com/damiencorpataux/pymjpeg

# Use the following shell command to create a self-signed TLS certificate and private key:
#    openssl req -new -newkey rsa:4096 -x509 -sha256 -days 365 -nodes -out cert.crt -keyout private.key


import html
import http.server
import json
import os
import queue
import select
import socket
import ssl
import sys
import threading
import time
import traceback

from io import BytesIO

import suear_struct
from suear_util import mount_offset_degrees, ping, roll_degrees, slope_degrees


DEBUG = bool(os.environ.get('SUEAR_DEBUG'))
UI_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'soulear_ui.html')
PHOTO_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'photos')


def log(msg):
    """Diagnostic line on stderr (unbuffered when launched with PYTHONUNBUFFERED=1)"""
    print(f'[{time.strftime("%H:%M:%S")}] {msg}', file=sys.stderr, flush=True)


def trim_jpeg(data):
    """
    Copy of the frame with the zero padding the firmware appends after the JPEG's
    EOI marker removed (the copy matters: frame buffers are recycled)
    """
    buf = bytes(data)
    eoi = buf.rfind(b'\xff\xd9')
    return buf[:eoi + 2] if eoi >= 1 else buf


HEARTBEAT_PORT = 10007   # camera -> client: unsolicited status push (type 0x0009)
_heartbeat_started = False


def start_heartbeat_listener():
    """
    Bind UDP 10007 and watch what the camera pushes at us. While streaming it
    sends a 17-byte status/heartbeat roughly once a second; anything else that
    shows up here (a different type, or a payload that is not the usual
    heartbeat) is the only hint we get of a hardware button press.
    """
    global _heartbeat_started
    if _heartbeat_started:
        return
    _heartbeat_started = True
    threading.Thread(target=_heartbeat_loop, daemon=True, name='suear-heartbeat').start()


def _heartbeat_loop():
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(('', HEARTBEAT_PORT))
        sock.settimeout(1.0)
    except OSError as e:
        log(f'WARN: cannot listen on UDP {HEARTBEAT_PORT}: {e}')
        return
    log(f'listening for camera pushes on UDP {HEARTBEAT_PORT}')
    header_sz = suear_struct.SuearUdpMsg_0xffeeffee.sizeof()
    last_change_log = 0.0
    while True:
        try:
            data, addr = sock.recvfrom(4096)
        except socket.timeout:
            continue
        except OSError:
            break
        if len(data) < header_sz or data[:4] != b'\xee\xff\xee\xff':
            StreamState.rx_event(bytes(data))
            log(f'EVENT (no header) from {addr[0]}:{addr[1]}: {bytes(data[:64]).hex()}')
            continue
        msg = suear_struct.SuearUdpMsg_0xffeeffee.from_bytes(data[:header_sz])
        payload = bytes(data[header_sz:header_sz + msg.length]) or bytes(data[header_sz:])
        if msg.type == 0x0009:
            if StreamState.rx_heartbeat(payload) and time.time() - last_change_log > 10:
                last_change_log = time.time()
                log(f'heartbeat changed: {payload.hex()}')
        else:
            StreamState.rx_event(payload)
            log(f'EVENT type=0x{msg.type:04x} from {addr[0]}:{addr[1]}: {payload.hex()}')


def jpeg_size(buf):
    """Width/height straight from the SOF marker - no decoder needed"""
    i, n = 2, len(buf)
    while i + 9 < n:
        if buf[i] != 0xFF:
            i += 1
            continue
        marker = buf[i + 1]
        if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
            i += 2
            continue
        seglen = int.from_bytes(buf[i + 2:i + 4], 'big')
        if seglen < 2:
            return None
        if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
            return (int.from_bytes(buf[i + 7:i + 9], 'big'),
                    int.from_bytes(buf[i + 5:i + 7], 'big'))
        i += 2 + seglen
    return None


class FrameHub:
    """
    Fan-out for finished JPEG frames: one reader thread publishes, every consumer
    (the HTTP stream, the recorder, ...) gets its own bounded queue. A slow disk
    or a stalled browser therefore can never hold up the picture for the others,
    and a slow consumer simply falls back to the newest frame instead of lagging.
    """

    def __init__(self, maxsize=8):
        self.maxsize = maxsize
        self._lock = threading.Lock()
        self._subs = []

    def subscribe(self):
        q = queue.Queue(maxsize=self.maxsize)
        with self._lock:
            self._subs.append(q)
        return q

    def unsubscribe(self, q):
        with self._lock:
            try:
                self._subs.remove(q)
            except ValueError:
                pass

    def publish(self, item):
        with self._lock:
            subs = list(self._subs)
        for q in subs:
            try:
                q.put_nowait(item)
            except queue.Full:
                try:
                    q.get_nowait()
                    q.put_nowait(item)
                except (queue.Empty, queue.Full):
                    pass


class Recorder:
    """
    Writes the live JPEG stream to recordings/*.mjpeg - a plain concatenation of
    JPEGs that VLC, mpv and ffplay all play back directly (and that ffmpeg can
    turn into an mp4). It runs on its own reader thread, so a recording keeps
    going while the camera sits on its charger and no browser tab is open.
    """

    RECORD_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'recordings')
    NO_VIDEO_AFTER_S = 60.0

    def __init__(self):
        self._lock = threading.Lock()
        self._thread = None
        self._stop = threading.Event()
        self.path = None
        self.frames = 0
        self.started_at = 0.0
        self.error = None

    @property
    def active(self):
        with self._lock:
            return self._thread is not None and self._thread.is_alive()

    def start(self, client):
        if self.active:
            return self.status()
        # Bring the stream up *before* taking our lock: open_video() takes the
        # client's stream lock, and stop_streaming() takes ours - nested the
        # other way round that pair deadlocks
        client.connect()
        client.open_video()
        started = False
        with self._lock:
            if not (self._thread is not None and self._thread.is_alive()):
                os.makedirs(self.RECORD_DIR, exist_ok=True)
                self.path = os.path.join(self.RECORD_DIR,
                                         time.strftime('%Y%m%d_%H%M%S') + '.mjpeg')
                self.frames = 0
                self.error = None
                self.started_at = time.time()
                self._stop.clear()
                sub = client.frame_hub.subscribe()
                t = threading.Thread(target=self._loop, args=(client, sub),
                                     daemon=True, name='suear-recorder')
                self._thread = t
                t.start()
                started = True
        if started:
            log(f'recording -> {self.path}')
        return self.status()

    def stop(self):
        with self._lock:
            t = self._thread
            if t is None or not t.is_alive():
                self._thread = None
                t = None
            else:
                self._stop.set()
        if t is None:
            return self.status()
        t.join(timeout=5.0)
        with self._lock:
            if not t.is_alive():
                self._thread = None
        return self.status()

    def _loop(self, client, sub):
        last_frame = time.time()
        try:
            with open(self.path, 'wb') as fh:
                while not self._stop.is_set():
                    try:
                        item = sub.get(timeout=1.0)
                    except queue.Empty:
                        if time.time() - last_frame > self.__class__.NO_VIDEO_AFTER_S:
                            self.error = f'no video for {int(self.__class__.NO_VIDEO_AFTER_S)}s'
                            break
                        continue
                    last_frame = time.time()
                    fh.write(item[0])
                    with self._lock:
                        self.frames += 1
                    if self.frames % 60 == 0:
                        fh.flush()
        except Exception as e:
            self.error = f'{type(e).__name__}: {e}'
            log(f'ERROR: recorder: {self.error}')
        finally:
            client.frame_hub.unsubscribe(sub)
            log(f'recording stopped after {self.frames} frames -> {os.path.basename(self.path)}')

    def status(self):
        with self._lock:
            active = self._thread is not None and self._thread.is_alive()
            return {
                'active': active,
                'path': self.path,
                'frames': self.frames,
                'seconds': int(time.time() - self.started_at) if (active and self.started_at) else 0,
                'error': self.error,
            }


RECORDER = Recorder()


class StreamState:
    """
    Shared, thread-safe view of the health of the live stream plus the last
    orientation (accelerometer) sample seen in the UDP stream chunk headers.
    """
    _lock = threading.Lock()
    # Same lock, plus a condition so the SSE endpoint can wait for the next
    # orientation sample instead of polling for it
    _cond = threading.Condition(_lock)
    _seq = 0                 # bumped on every new orientation sample (SSE change token)
    frames = 0
    chunks = 0
    discards = 0
    errors = 0
    last_rx = 0.0
    last_frame_at = 0.0
    accel = 0
    accel_at = 0.0
    roll = None            # roll in degrees, already corrected by the mount offset
    raw_roll = None        # roll as reported by the sensor
    slope = None
    last_jpeg = None       # most recent complete frame, for /snapshot
    width = None           # resolution announced in the frame header
    height = None
    jpeg_width = None      # resolution actually found in the JPEG's SOF marker
    jpeg_height = None
    _dim_mismatch = False
    heartbeat = None       # payload of the last 17-byte status push (port 10007)
    heartbeat_at = 0.0
    heartbeat_changes = 0  # how often that payload changed (button candidates)
    last_event = None      # any datagram that is not the regular heartbeat
    last_event_at = 0.0
    packet_types = {}

    @classmethod
    def rx_chunk(cls, packet_type, accel):
        with cls._lock:
            cls.chunks += 1
            cls.last_rx = time.time()
            cls.accel = int(accel)
            cls.accel_at = cls.last_rx
            cls.packet_types[int(packet_type)] = cls.packet_types.get(int(packet_type), 0) + 1

    @classmethod
    def set_orientation(cls, raw_roll, slope, mount_offset):
        with cls._cond:
            cls.raw_roll = raw_roll
            cls.slope = slope
            if raw_roll is None:
                return
            new_roll = (raw_roll + mount_offset) % 360.0
            # Every chunk repeats the last sample; only wake the SSE clients when
            # the angle really moved (the sensor is ~0.44° per step anyway)
            if cls.roll is not None and abs(new_roll - cls.roll) < 0.005:
                cls.roll = new_roll
                return
            cls.roll = new_roll
            cls._seq += 1
            cls._cond.notify_all()

    @classmethod
    def rx_frame(cls, jpeg=None, width=None, height=None):
        with cls._lock:
            cls.frames += 1
            cls.last_frame_at = time.time()
            if width:
                cls.width = int(width)
            if height:
                cls.height = int(height)
            if jpeg is not None:
                cls.last_jpeg = trim_jpeg(jpeg)
                size = jpeg_size(cls.last_jpeg)
                if size:
                    cls.jpeg_width, cls.jpeg_height = size
                    if width and height and size != (int(width), int(height)):
                        if not cls._dim_mismatch:
                            cls._dim_mismatch = True
                            log(f'WARN: frame header says {width}x{height} '
                                f'but the JPEG is {size[0]}x{size[1]}')

    @classmethod
    def count_discard(cls, n=1):
        with cls._lock:
            cls.discards += n

    @classmethod
    def count_error(cls, n=1):
        with cls._lock:
            cls.errors += n

    @classmethod
    def rx_heartbeat(cls, payload):
        with cls._lock:
            changed = payload != cls.heartbeat
            cls.heartbeat = payload
            cls.heartbeat_at = time.time()
            if changed:
                cls.heartbeat_changes += 1
            return changed

    @classmethod
    def rx_event(cls, payload):
        with cls._lock:
            cls.last_event = payload
            cls.last_event_at = time.time()

    @classmethod
    def snapshot(cls):
        with cls._lock:
            return {
                'frames': cls.frames,
                'chunks': cls.chunks,
                'discards': cls.discards,
                'errors': cls.errors,
                'last_rx': cls.last_rx,
                'last_frame_at': cls.last_frame_at,
                'accel': cls.accel,
                'accel_at': cls.accel_at,
                'roll': cls.roll,
                'raw_roll': cls.raw_roll,
                'slope': cls.slope,
                'width': cls.width,
                'height': cls.height,
                'jpeg_width': cls.jpeg_width,
                'jpeg_height': cls.jpeg_height,
                'heartbeat': cls.heartbeat,
                'heartbeat_at': cls.heartbeat_at,
                'heartbeat_changes': cls.heartbeat_changes,
                'last_event': cls.last_event,
                'last_event_at': cls.last_event_at,
                'packet_types': dict(cls.packet_types),
            }

    @classmethod
    def latest_jpeg(cls):
        with cls._lock:
            return cls.last_jpeg


class LedUnsupported(IOError):
    """The camera answered, but it has no (working) ring light"""


class HttpHandler(http.server.BaseHTTPRequestHandler):
    BOUNDARY = b'--SP-LaputanMachine--'
    SUEAR_CLIENT = None
    RENDER_RATE = 0  # One frame is rendered locally (with MatPlotLib) for every RENDER_RATE frames sent to the HTTP client (Set to <1 to never render locally)
    PROTOCOL = 'http'
    PORT = 45100
    
    
    @classmethod
    def HEADERS_BASE(cls):
        headers = {
            'Cache-Control': 'no-store, no-cache, must-revalidate, pre-check=0, post-check=0, max-age=0',
            'Content-Type': f'multipart/x-mixed-replace;boundary={cls.BOUNDARY.decode("ascii")}',
            #'Connection': 'close',
            'Pragma': 'no-cache',
            'Access-Control-Allow-Origin': '*',  # CORS
        }
        return headers
    
    
    @classmethod
    def HEADERS_IMAGE(cls, length):
        headers = {
            'X-Timestamp': time.time(),
            'Content-Length': str(int(length)),
            'Content-Type': 'image/jpeg',
        }
        return headers
    
    
    API_PATHS = ('/', '/stream', '/events', '/device', '/position', '/stats', '/snapshot',
                 '/photo', '/record', '/cameracfg', '/led', '/battery', '/model', '/vendor',
                 '/version', '/ssid', '/capacity', '/charging', '/serial')
    STREAM_CLIENTS = 0          # how many browsers are currently pulling /stream
    _stream_lock = threading.Lock()

    def do_GET(self):
        path, _, query = self.path.partition('?')
        params = {}
        for pair in query.split('&'):
            if not pair:
                continue
            key, _, value = pair.partition('=')
            params[key] = value
        self._params = params

        if path not in self.__class__.API_PATHS:
            return self._send(b'Not found', 'text/plain; charset=utf-8', 404)

        if path == '/stream':
            return self._serve_stream()

        if path == '/events':
            return self._serve_events()

        try:
            self._serve_api(path)
        except Exception as e:
            # A switched-off camera has to degrade into readable JSON, never a
            # stack trace in the browser
            StreamState.count_error()
            log(f'ERROR: {path} failed: {type(e).__name__}: {e}')
            self._json({'error': f'{type(e).__name__}: {e}', 'online': False}, 503)


    def do_POST(self):
        # Commands are query based (/led?on=1, ...), so POST routes like GET;
        # any request body is swallowed to keep the connection in sync
        length = int(self.headers.get('Content-Length') or 0)
        if length > 0:
            self.rfile.read(length)
        self.do_GET()


    def _send(self, data, ctype='text/plain; charset=utf-8', code=200, extra=None):
        if isinstance(data, str):
            data = data.encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(data)))
        self.send_header('Cache-Control', 'no-store, no-cache, must-revalidate')
        self.send_header('Connection', 'close')
        if extra:
            for k, v in extra.items():
                self.send_header(k, v)
        self.end_headers()
        try:
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            # The browser closed the tab while we were answering - that is
            # routine, not an error worth a traceback
            pass


    def _json(self, obj, code=200):
        self._send(json.dumps(obj), 'application/json; charset=utf-8', code)


    @staticmethod
    def save_photo(jpeg, source='ui'):
        """Write a JPEG into photos/ with timestamp + roll angle in the name"""
        os.makedirs(PHOTO_DIR, exist_ok=True)
        roll = StreamState.snapshot()['roll']
        name = time.strftime('%Y%m%d_%H%M%S')
        if source:
            name += '_' + str(source)
        if roll is not None:
            name += f'_{int(round(roll)) % 360:03d}deg'
        fpath = os.path.join(PHOTO_DIR, name + '.jpg')
        n = 1
        while os.path.exists(fpath):
            fpath = os.path.join(PHOTO_DIR, f'{name}_{n}.jpg')
            n += 1
        with open(fpath, 'wb') as f:
            f.write(jpeg)
        log(f'photo saved: {fpath} ({len(jpeg)} bytes, source={source})')
        return {'file': fpath, 'name': os.path.basename(fpath), 'bytes': len(jpeg), 'roll': roll}


    def _serve_events(self):
        """Server-Sent Events: orientation pushed the moment a chunk arrives, so
        the rotation has no polling latency (the old 120ms poll made the picture
        lag behind the hand)"""
        self.send_response(200)
        for k, v in self.__class__.HEADERS_BASE().items():
            if k in ('Content-Type', 'Cache-Control'):
                continue
            self.send_header(k, v)
        self.send_header('Content-Type', 'text/event-stream; charset=utf-8')
        self.send_header('Cache-Control', 'no-cache')
        self.send_header('Connection', 'keep-alive')
        self.end_headers()
        cond = StreamState._cond
        last_seq = -1
        try:
            while True:
                with cond:
                    if StreamState._seq == last_seq:
                        cond.wait(1.0)
                    changed = StreamState._seq != last_seq
                    last_seq = StreamState._seq
                    payload = {
                        'roll': StreamState.roll,
                        'raw_roll': StreamState.raw_roll,
                        'slope': StreamState.slope,
                        'seq': last_seq,
                    } if changed else None
                # Never write while holding the lock: a slow client would then
                # stall the receiver thread that produces these samples
                if payload is None:
                    self.wfile.write(b': ping\n\n')
                else:
                    self.wfile.write(b'data: ' + json.dumps(payload).encode('utf-8') + b'\n\n')
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, ssl.SSLError, OSError):
            return


    def _serve_api(self, path):
        suear_client = self.__class__.SUEAR_CLIENT
        if suear_client is None:
            return self._json({'error': 'Suear client unavailable', 'online': False}, 503)

        if path == '/':
            with open(UI_FILE, 'rb') as f:
                return self._send(f.read(), 'text/html; charset=utf-8')

        if path == '/device':
            st = suear_client.status()
            st['online'] = True
            return self._json(st)

        if path == '/position':
            snap = StreamState.snapshot()
            now = time.time()
            return self._json({
                'roll': snap['roll'],
                'raw_roll': snap['raw_roll'],
                'slope': snap['slope'],
                'accel': snap['accel'],
                'age_ms': int((now - snap['accel_at']) * 1000) if snap['accel_at'] else -1,
            })

        if path == '/stats':
            snap = StreamState.snapshot()
            now = time.time()
            return self._json({
                'frames': snap['frames'],
                'chunks': snap['chunks'],
                'discards': snap['discards'],
                'errors': snap['errors'],
                'ms_since_rx': int((now - snap['last_rx']) * 1000) if snap['last_rx'] else -1,
                'ms_since_frame': int((now - snap['last_frame_at']) * 1000) if snap['last_frame_at'] else -1,
                'width': snap['width'],
                'height': snap['height'],
                'jpeg_width': snap['jpeg_width'],
                'jpeg_height': snap['jpeg_height'],
                'heartbeat': snap['heartbeat'].hex() if snap['heartbeat'] else None,
                'hb_age_ms': int((now - snap['heartbeat_at']) * 1000) if snap['heartbeat_at'] else -1,
                'hb_changes': snap['heartbeat_changes'],
                'event': snap['last_event'].hex() if snap['last_event'] else None,
                'packet_types': snap['packet_types'],
                'recording': RECORDER.status(),
            })

        if path == '/snapshot':
            jpeg = StreamState.latest_jpeg()
            if not jpeg:
                return self._json({'error': 'no frame captured yet'}, 503)
            return self._send(jpeg, 'image/jpeg')

        if path == '/record':
            if 'on' in self._params:
                want = self._params['on'] not in ('0', 'false', 'off', '')
                if want:
                    try:
                        RECORDER.start(suear_client)
                    except Exception as e:
                        StreamState.count_error()
                        log(f'ERROR: cannot start recording: {type(e).__name__}: {e}')
                        return self._json({'active': False, 'error': f'{type(e).__name__}: {e}'}, 503)
                else:
                    RECORDER.stop()
            return self._json(RECORDER.status())

        if path == '/cameracfg':
            # Read-only: dump of the camera configuration block (0x000d) so an
            # unknown flag - a heater/heat setting for instance - can be found
            try:
                payload = bytes.fromhex(self._params.get('hex', '') or '')
                data, err = suear_client.get_camera_config(payload)
            except Exception as e:
                return self._json({'error': f'{type(e).__name__}: {e}', 'online': False}, 503)
            return self._json({'err': err, 'len': len(data), 'hex': data.hex(),
                               'payload_len': len(payload)})

        if path == '/photo':
            # Save the frame on the server (what the camera button uses), so the
            # shot survives even if no browser is watching
            jpeg = StreamState.latest_jpeg()
            if not jpeg:
                return self._json({'error': 'no frame captured yet'}, 503)
            return self._json(self.save_photo(jpeg, source=self._params.get('source', 'ui')))

        if path == '/led':
            on = None
            if 'on' in self._params:
                on = self._params['on'] not in ('0', 'false', 'off', '')
            try:
                state = suear_client.led(on)
            except LedUnsupported as e:
                # Not every model exposes the ring light - report that instead
                # of pretending the camera went offline
                log(f'WARN: LED control unavailable: {e}')
                return self._json({'supported': False, 'error': str(e)})
            state['supported'] = True
            return self._json(state)

        legacy = {
            '/battery': lambda: str(suear_client.battery_level),
            '/model': lambda: str(suear_client.model),
            '/vendor': lambda: str(suear_client.vendor),
            '/version': lambda: str(suear_client.version),
            '/ssid': lambda: str(suear_client.ssid),
            '/capacity': lambda: str(suear_client.capacity),
            '/charging': lambda: str(int(suear_client.is_charging)),
            '/serial': lambda: str(suear_client.serial_num),
        }
        if path in legacy:
            return self._send(html.escape(legacy[path]()))

        return self._send(b'Not found', 'text/plain; charset=utf-8', 404)


    def _serve_stream(self):
        suear_client = self.__class__.SUEAR_CLIENT
        if suear_client is None:
            return self._send(b'Error: Suear client unavailable', 'text/plain; charset=utf-8', 503)

        # Set up *before* promising a 200: a switched-off camera has to produce a
        # readable error instead of a multipart response that never carries a frame
        try:
            if suear_client.device_info_cached is None:
                # One round trip so the accelerometer mount offset is known before
                # the first frame arrives
                suear_client.status()
            suear_client.connect()
            suear_client.open_video()
        except Exception as e:
            StreamState.count_error()
            log(f'ERROR: cannot start stream: {type(e).__name__}: {e}')
            return self._json({'error': f'{type(e).__name__}: {e}', 'online': False}, 503)

        self.send_response(200)
        for k, v in self.__class__.HEADERS_BASE().items():
            self.send_header(k, v)
        log(f'client {self.client_address[0]}: stream started')
        cls = self.__class__
        with cls._stream_lock:
            cls.STREAM_CLIENTS += 1
        sub = suear_client.frame_hub.subscribe()
        try:
            # The reader thread owns the UDP sockets; this handler only drains
            # its own queue, so a second tab (or the recorder) cannot make the
            # first one miss frames.
            while True:
                try:
                    jpeg = sub.get(timeout=1.0)[0]
                except queue.Empty:
                    if suear_client._reader is None or not suear_client._reader.is_alive():
                        raise OSError('frame reader stopped')
                    continue

                self.end_headers()
                self.wfile.write(self.__class__.BOUNDARY)
                self.end_headers()
                img_headers = self.__class__.HEADERS_IMAGE(len(jpeg))
                for k, v in img_headers.items():
                    self.send_header(k, v)
                self.end_headers()
                self.wfile.write(jpeg)
        except (BrokenPipeError, ConnectionResetError, ssl.SSLError, OSError) as e:
            log(f'client {self.client_address[0]}: stream ended ({type(e).__name__}: {e})')
        except Exception:
            StreamState.count_error()
            log(f'client {self.client_address[0]}: stream handler failed:\n' + traceback.format_exc())
        finally:
            suear_client.frame_hub.unsubscribe(sub)
            with cls._stream_lock:
                cls.STREAM_CLIENTS -= 1
                if cls.STREAM_CLIENTS <= 0:
                    cls.STREAM_CLIENTS = 0
                    if RECORDER.active:
                        log('last viewer gone - recording keeps the stream alive')
                    else:
                        suear_client.stop_streaming()
                        log('last viewer gone - stopped reading video')
        return


class JpgFrame:
    BUF_SZ = 131072
    
    def __init__(self, index=None, width=None, height=None, first_chunk_idx=None):
        self._buf = bytearray(self.__class__.BUF_SZ)
        if None not in (index, width, height, first_chunk_idx):
            self.init(index, width, height, first_chunk_idx)
        return
    
    
    def init(self, index, width, height, first_chunk_idx):
        self.index = int(index)
        self.width = int(width)
        self.height = int(height)
        self.first_chunk_idx = first_chunk_idx
        self.total = None
        self.complete = False  # True when all chunks have been acquired
        self.chunk_sz = None   # All but the final chunk have the same size
        self.acquired_sz = 0   # Total number of bytes acquired
        self._data = memoryview(self._buf)
    
    
    def add_chunk(self, idx, data, final=0):
        assert not self.complete, 'Attempt to add a chunk to a completed frame'
        if not final:
            if self.chunk_sz is not None:
                assert self.chunk_sz == len(data), f'Chunk size mismatch:  {self.chunk_sz=}  {len(data)=}'
            self.chunk_sz = len(data)
        elif self.chunk_sz is None:
            # Received last chunk before any other chunk... just allow the bad data?
            self.chunk_sz = len(data)
        
        # Chunk index is only 8 bits; 255 rolls over to 0, so we correct this:
        if idx < self.first_chunk_idx:
            idx += 256

        start = self.chunk_sz * (idx - self.first_chunk_idx)
        self._data[start:start+len(data)] = data
        self.acquired_sz += len(data)
        if final:
            self.total = int(final)
        if self.total and self.acquired_sz > self.chunk_sz * (self.total-1):
            self.complete = True
        return
    
    
    @property
    def data(self):
        assert self.complete, 'Attempt to reassemble incomplete frame'
        return self._data[:self.acquired_sz]
    
    
    def render(self, title=None):
        import matplotlib.pyplot
        img = matplotlib.pyplot.imread(BytesIO(self.data), format='jpeg')
        if title is None:
            title = f'Frame {self.index}'
        matplotlib.pyplot.title(title)
        matplotlib.pyplot.imshow(img)
        matplotlib.pyplot.show(block=False)
        matplotlib.pyplot.pause(0.001)



class SuearClient:
    DEFAULT_SERVER = '192.168.1.1'
    COMMAND_PORT = 10005  # UDP
    STREAM_INIT_PORT = 10006  # UDP
    # Which local port the device pushes video chunks to is apparently
    # hardcoded per firmware build, not negotiated in the protocol - some
    # X6-family units use 22785, others 22789 (see issue #9). Rather than
    # guess, listen on all known ports and use whichever one actually
    # receives data.
    STREAM_RECV_PORTS = (22785, 22789)
    FRAME_CHUNK_SZ = 1456
    UDP_READ_SZ = 8192
    FRAME_QUEUE_MAX = 8
    # If no stream data arrives for this many seconds, get_frame()
    # automatically disconnects and reconnects instead of hanging forever
    # (the original blocking recv_into() had no timeout at all, so any
    # pause in the device's output - a Wi-Fi hiccup, a brief stall - would
    # block indefinitely with no way to recover short of restarting the
    # script).
    STALL_RECONNECT_AFTER_S = 8.0
    # The vendor app re-sends "open video" after one silent second; it doubles
    # as the camera's keepalive, so we do the same before rebuilding anything
    STALL_KEEPALIVE_AFTER_S = 1.5
    SELECT_TIMEOUT_S = 1.0
    COMMAND_TIMEOUT_S = 5.0
    
    def __init__(self, server=DEFAULT_SERVER, cmd_send_index=0):
        self.server = socket.gethostbyname(server)  # Server host name or IP address
        self.cmd_send_index = int(cmd_send_index) & 0xffff  # Incremented with each message sent to the server (2 bytes)
        self._license = None
        self._camera_config = None
        self._device_info = None
        self._connected = False
        self.command_sock = None
        self.stream_socks = []
        self._last_recv_at = 0.0
        self._last_open_video_at = 0.0
        self._mount_offset = 0.0
        # Serialises request/reply pairs: the battery poll, the LED control and
        # the video keepalive all share one UDP socket and one sequence counter
        self._cmd_lock = threading.Lock()
        # Serialises start/stop of the video push: opening and re-binding the local
        # video sockets from two threads at once made the second bind fail with
        # EADDRINUSE ("Address already in use") and killed the viewer
        self._stream_state_lock = threading.RLock()
        self.frame_hub = FrameHub()
        self._reader = None
        self.stream_buf = memoryview(bytearray(self.__class__.UDP_READ_SZ))
        self.streaming = False
        self.frame_queue = queue.Queue()
        self.frame_dict = {}
        self.frame_reserve = []
        self.frame_reserve_idx = 0
        self._seen_dims = set()
        for i in range(self.__class__.FRAME_QUEUE_MAX):
            self.frame_reserve.append(JpgFrame())
        return
    
    
    def connect(self):
        if self._connected and self.command_sock is not None:
            return
        print(f'Connecting to {self.server}')
        if not ping(self.server):
            raise IOError(f'[ERROR] No ICMP response from {self.server}')
        self.command_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        # Never block forever on a device that stopped answering - a hanging
        # recvfrom() would freeze whichever HTTP request triggered it
        self.command_sock.settimeout(self.__class__.COMMAND_TIMEOUT_S)

        self._connected = True
        start_heartbeat_listener()
        threading.Thread(target=self._control_drain, daemon=True,
                         name='suear-control-drain').start()


    def _control_drain(self):
        """
        Read the control socket even when no command is in flight, so a push from
        the device (the camera's button, a state change) is logged instead of
        rotting in the socket buffer. Serialised with send_command via _cmd_lock,
        so it can never steal a reply that another thread is waiting for.
        """
        while self._connected and self.command_sock is not None:
            time.sleep(0.5)
            sock = self.command_sock
            if sock is None or sock._closed:
                break
            try:
                with self._cmd_lock:
                    try:
                        sock.settimeout(0.05)
                        data, addr = sock.recvfrom(0x1000)
                    except (socket.timeout, BlockingIOError):
                        continue
                    except OSError:
                        break
                    finally:
                        try:
                            sock.settimeout(self.__class__.COMMAND_TIMEOUT_S)
                        except OSError:
                            pass
                header_sz = suear_struct.SuearUdpMsg_0xffeeffee.sizeof()
                msg = suear_struct.SuearUdpMsg_0xffeeffee.from_bytes(data[:header_sz]) \
                    if len(data) >= header_sz else None
                desc = (f'id={msg.id} type=0x{msg.type:04x} err={msg.err_code} '
                        f'len={msg.length}' if msg is not None else 'no header')
                log(f'PUSH from {addr[0]}:{addr[1]}: {desc} {bytes(data[:64]).hex()}')
            except Exception:
                log('ERROR: control drain:\n' + traceback.format_exc())
    
    
    def disconnect(self):
        self.streaming = False
        if self.command_sock is not None:
            if not self.command_sock._closed:
                self.command_sock.close()
            self.command_sock = None
        for sock in self.stream_socks:
            if not sock._closed:
                sock.close()
        self.stream_socks = []
        self._connected = False
    
    
    def stream_to_matplotlib(self):
        # Don't use this function
        self.connect()
        self.streaming = True
        self.stream_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        server_address = (self.server, self.__class__.STREAM_INIT_PORT)
        data = self.__class__.READ_STREAM_REQUEST
        sent = self.stream_sock.sendto(data, server_address)
        assert sent == len(data), f'UDP message was {len(data)} bytes but only {sent} were sent'
        while self.streaming:
            frame = self.get_frame()
            if frame is None:
                continue
            
            frame.render()
            #print(f'Reconstructed frame: {frame.index}')
            #time.sleep(0.016)  # ~60FPS
        
        self.streaming = False
        if self.stream_sock is not None and not self.stream_sock._closed:
            # @TODO: Send EndStream message
            self.stream_sock.close()
        self.stream_sock = None
        return


    def get_frame(self):
        if not self.streaming:
            return None

        frame = None

        while self.stream_socks and self.streaming:
            try:
                ready, _, _ = select.select(self.stream_socks, [], [], self.__class__.SELECT_TIMEOUT_S)
            except (OSError, ValueError) as e:
                # A stream socket got closed underneath us while another thread was
                # rebuilding the stream; the loop condition re-checks streaming/socks
                time.sleep(0.02)
                continue

            if not ready:
                # No data on any stream socket within the select window.
                # A single empty select() is normal; past the keepalive threshold we
                # re-send "open video" (exactly what the vendor app does - that
                # doubles as the camera's heartbeat), and past the stall threshold we
                # rebuild the whole stream.
                silent_for = time.time() - self._last_recv_at
                if silent_for > self.__class__.STALL_KEEPALIVE_AFTER_S:
                    now = time.time()
                    if now - self._last_open_video_at >= self.__class__.STALL_KEEPALIVE_AFTER_S:
                        self._last_open_video_at = now
                        try:
                            self.send_open_video()
                            log(f'WARN: no video for {silent_for:.1f}s - re-sent open-video keepalive')
                        except Exception:
                            StreamState.count_error()
                            log('ERROR: keepalive failed:\n' + traceback.format_exc())
                if silent_for > self.__class__.STALL_RECONNECT_AFTER_S:
                    log(f'WARN: no stream data for over {self.__class__.STALL_RECONNECT_AFTER_S}s - reconnecting to {self.server}')
                    try:
                        with self._stream_state_lock:
                            self.disconnect()
                            self.connect()
                            self.open_video()
                        log('WARN: stream re-established')
                    except Exception:
                        StreamState.count_error()
                        log('ERROR: reconnect attempt failed:\n' + traceback.format_exc())
                        # Keep the stream marked active and listening again, otherwise the
                        # HTTP handler would exit its loop and kill the connection for good
                        try:
                            with self._stream_state_lock:
                                self._bind_stream_socks()
                                self.streaming = True
                        except Exception:
                            log('ERROR: could not re-bind stream sockets:\n' + traceback.format_exc())
                        time.sleep(1)
                    return None
                continue

            for sock in ready:
                try:
                    nread = sock.recv_into(self.stream_buf)
                except OSError:
                    break
                self._last_recv_at = time.time()
                buf = self.stream_buf[:nread]
                offs = 0

                # Parse response for multiple messages
                while True:
                    read_sz = suear_struct.SuearUdpMsg_StreamChunk.sizeof()
                    data = buf[offs:offs+read_sz]
                    offs += read_sz

                    if len(data) < read_sz:
                        if len(data) > 0:
                            log(f'short UDP message: {len(data)} < {read_sz} bytes: {bytes(data)}')
                        break

                    msg = suear_struct.SuearUdpMsg_StreamChunk.from_bytes(data)
                    StreamState.rx_chunk(msg.packet_type, msg.accel)
                    if msg.has_accel:
                        StreamState.set_orientation(
                            roll_degrees(msg.accel),
                            slope_degrees(msg.accel),
                            self._mount_offset,
                        )
                    # Type 6 packets carry 12 extra bytes of 6-axis IMU data in the
                    # header, so the JPEG payload starts further along
                    offs += msg.header_size - suear_struct.SuearUdpMsg_StreamChunk.HEADER_V1
                    data = buf[offs:offs + self.__class__.FRAME_CHUNK_SZ]
                    offs += len(data)
                    if not data:
                        break

                    if msg.n_frame in self.frame_dict:
                        parse_frame = self.frame_dict[msg.n_frame]
                    else:
                        # Evict unfinished frames until a free slot exists. Pop by
                        # identity: the queued object may have been re-inited with a
                        # different index, in which case popping by index would remove
                        # nothing and spin here forever (the "stream freezes" bug).
                        while len(self.frame_dict) >= len(self.frame_reserve):
                            StreamState.count_discard()
                            if DEBUG:
                                log(f'Discarding frame ({len(self.frame_dict)} in flight)')
                            try:
                                stale = self.frame_queue.get_nowait()
                            except queue.Empty:
                                break
                            for key in [k for k, v in self.frame_dict.items() if v is stale]:
                                self.frame_dict.pop(key, None)
                        parse_frame = None
                        for _ in range(len(self.frame_reserve)):
                            cand = self.frame_reserve[self.frame_reserve_idx]
                            self.frame_reserve_idx = (self.frame_reserve_idx + 1) % len(self.frame_reserve)
                            if not any(v is cand for v in self.frame_dict.values()):
                                parse_frame = cand
                                break
                        if parse_frame is None:
                            # Should be unreachable (dict is smaller than the pool), but
                            # never reinitialise a frame that is still in flight
                            StreamState.count_error()
                            log('ERROR: no free frame slot')
                            continue
                        dims = (int(msg.res_width), int(msg.res_height))
                        if dims not in self._seen_dims:
                            self._seen_dims.add(dims)
                            log(f'frame header announces {dims[0]}x{dims[1]}')
                        parse_frame.init(msg.n_frame, msg.res_width, msg.res_height, msg.n_chunk)
                        self.frame_dict[msg.n_frame] = parse_frame
                        self.frame_queue.put(parse_frame)

                    try:
                        parse_frame.add_chunk(msg.n_chunk, data, msg.total_chunks)
                    except Exception:
                        # A malformed/reordered chunk must not take the reader down
                        StreamState.count_error()
                        if DEBUG:
                            log('ERROR: add_chunk failed:\n' + traceback.format_exc())
                        self.frame_dict.pop(msg.n_frame, None)
                        continue

                    # If a frame enters the "complete" state, pop frames from the queue (and delete them from
                    # the dict) until the popped frame is the completed frame
                    if parse_frame.complete:
                        while True:
                            try:
                                tmp_frame = self.frame_queue.get_nowait()
                            except queue.Empty:
                                break
                            self.frame_dict.pop(tmp_frame.index, None)
                            if parse_frame.index == tmp_frame.index:
                                break
                        StreamState.rx_frame(jpeg=parse_frame.data,
                                             width=parse_frame.width,
                                             height=parse_frame.height)
                        frame = parse_frame
                        return frame

        return frame
    
    
    def mirror_http(self, cert_fpath=None, privkey_fpath=None):
        port = 45100
        HttpHandler.SUEAR_CLIENT = self
        HttpHandler.PORT = port
        server_address = ('0.0.0.0', port)
        httpd = http.server.ThreadingHTTPServer(server_address, HttpHandler)
        if None not in (cert_fpath, privkey_fpath):
            HttpHandler.PROTOCOL = 'https'
            context = ssl.SSLContext(ssl.PROTOCOL_TLS)
            context.load_cert_chain(certfile=cert_fpath, keyfile=privkey_fpath, password='')
            httpd.socket = context.wrap_socket(httpd.socket, server_side=True)
        print(f'Serving {HttpHandler.PROTOCOL.upper()} on {HttpHandler.PROTOCOL.lower()}://{server_address[0]}:{server_address[1]}')
        httpd.serve_forever()
    
    
    @property
    def connected(self):
        return self._connected
    
    
    def increment(self):
        """
        Increment and return the command-send-index while restricting it to two bytes
        """
        self.cmd_send_index = int(self.cmd_send_index + 1) & 0xffff
        return self.cmd_send_index

    
    def send_command(self, msg, connecting=False, port=None, sock=None):
        if type(msg) not in (bytes, suear_struct.SuearUdpMsg_0xffeeffee,):
            raise TypeError(f'Bad request message type: {type(msg)}')
        
        if type(msg) == bytes:
            data = b''
            if msg.startswith(b'\xee\xff\xee\xff'):
                data = msg[suear_struct.SuearUdpMsg_0xffeeffee.sizeof():]
                msg = suear_struct.SuearUdpMsg_0xffeeffee.from_bytes(msg)
            else:
                raise ValueError(f'Invalid UDP message magic bytes: {msg[:100]}')
            
            msg.data = data
            msg.length = len(data)
        
        if not (connecting or self.connected):
            self.connect()

        with self._cmd_lock:
            msg.id = self.increment()
            if not port:
                port = self.__class__.COMMAND_PORT
            if not sock:
                sock = self.command_sock
            #print(f'\n[Client -> {self.server}:{port}]\n{msg.type_name} {msg}\n{msg.data}\n')

            server_address = (self.server, port)
            sock.sendto(bytes(msg), server_address)
            while True:
                try:
                    response_data, server = sock.recvfrom(0x1000)#msg.sizeof())
                except socket.timeout:
                    raise IOError(f'Timeout waiting for reply to {getattr(msg, "type_name", "command")} from {self.server}')
                assert server[0] == self.server, f'Response from unknown host {server[0]}'
                response = msg.__class__.from_bytes(response_data[:msg.__class__.sizeof()])
                if response.type == msg.type:
                    if response.id != msg.id:
                        log(f'late reply: wanted id={msg.id}, got id={response.id} '
                            f'for type=0x{response.type:04x}')
                    break
                # Not the reply we are waiting for: either a late answer to an
                # earlier retry, or a push from the device (e.g. a button press).
                # Never drop those silently - they are the only clue we get.
                log(f'unsolicited UDP from {server[0]}:{server[1]} while waiting for '
                    f'id={msg.id} type=0x{msg.type:04x}: got id={response.id} '
                    f'type=0x{response.type:04x} err={response.err_code} '
                    f'len={len(response_data)} {bytes(response_data[:64]).hex()}')
            response_data = response_data[msg.__class__.sizeof():]
            if response.length > 0:
                # response.data, server = sock.recvfrom(response.length)
                # assert server[0] == self.server, f'Response from unknown host {server[0]}'
                response.data = response_data
                response_data = response_data[response.length:]
            assert len(response_data) == 0, f'Encountered extraneous UDP message data: {response_data}'
            #print(f'[{self.server}:{port} -> Client]\n{response.type_name} {response}\n{response.data}\n\n')
            return response
    

    def send_open_video(self):
        """
        Tell the device to start pushing JPEG frames to us. Sent again as a
        keepalive whenever the stream goes quiet (the vendor app does the same).
        """
        stream_init_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            msg = b'\xee\xff\xee\xff\x00\x00\x04\x00\x01\x00\x00\x00'
            response = self.send_command(msg, port=self.__class__.STREAM_INIT_PORT, sock=stream_init_sock)
            assert response.err_code == 0, f'UDP message error code {response.err_code}'
        finally:
            stream_init_sock.close()
        self._last_open_video_at = time.time()
        return response


    def stop_streaming(self):
        """
        Nobody is watching: stop reading so half-built frames do not pile up and
        every discard is not counted as damage. The reader thread parks instead
        of exiting, so the next viewer (or the recorder) just resumes it.
        """
        with self._stream_state_lock:
            self.streaming = False
            self.frame_dict.clear()
            try:
                while True:
                    self.frame_queue.get_nowait()
            except queue.Empty:
                pass


    def open_video(self):
        """
        When this is called, the device starts sending JPEG frames to the client
        """
        with self._stream_state_lock:
            if self.streaming:
                return

            response = self.send_open_video()

            self._bind_stream_socks()

            self._last_recv_at = time.time()
            self.streaming = True
            self._start_reader()

            return response


    def _start_reader(self):
        """
        One dedicated thread pulls frames off the UDP sockets. Every consumer
        (browser tabs, the recorder) then just watches the hub, so the picture
        keeps flowing even when no tab is attached - which is exactly what a
        recording needs. The thread is started once and parks while nobody
        wants video, so start/stop never has to race with its lifetime.
        """
        t = self._reader
        if t is not None and t.is_alive():
            return
        t = threading.Thread(target=self._reader_loop, daemon=True, name='suear-reader')
        self._reader = t
        t.start()


    def _reader_loop(self):
        log('frame reader started')
        while True:
            if not self.streaming:
                time.sleep(0.05)
                continue
            try:
                frame = self.get_frame()
            except Exception:
                StreamState.count_error()
                log('ERROR: frame reader:\n' + traceback.format_exc())
                time.sleep(0.2)
                continue
            if frame is None:
                time.sleep(0.01)
                continue
            self.frame_hub.publish((trim_jpeg(frame.data), frame.width, frame.height, frame.index))


    def _bind_stream_socks(self):
        """
        (Re)open the local UDP sockets the device pushes video chunks to
        """
        with self._stream_state_lock:
            for sock in self.stream_socks:
                if not sock._closed:
                    sock.close()
            self.stream_socks = []
            for port in self.__class__.STREAM_RECV_PORTS:
                sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                sock.bind(('0.0.0.0', port))
                self.stream_socks.append(sock)
        return


    @property
    def license(self):
        if not self._license:
            msg = b'\xee\xff\xee\xff\x00\x00\x02\x00\x01\x00\x00\x00'
            response = self.send_command(msg)
            self._license = suear_struct.SuearLicenseInfo.from_bytes(response.data)
        return self._license


    @property
    def camera_config(self):
        if not self._camera_config:
            msg = b'\xee\xff\xee\xff\x00\x00\x0c\x00\x01\x00\x00\x00'
            response = self.send_command(msg)
            self._camera_config = response.data
        return self._camera_config


    def get_camera_config(self, payload=b''):
        """
        Read-only dump of command 0x000d (GetCameraConfig). Kept raw on purpose:
        until the field layout is known, a hex dump is worth more than guesses.
        """
        msg = (b'\xee\xff\xee\xff\x00\x00\x0d\x00\x01\x00'
               + len(payload).to_bytes(2, 'little') + payload)
        response = self.send_command(msg)
        return response.data, response.err_code


    def device_info(self, update=True):
        if (not self._device_info) or update:
            msg = b'\xee\xff\xee\xff\x00\x00\x01\x00\x01\x00\x00\x00'
            response = self.send_command(msg)
            self._device_info = suear_struct.SuearDeviceInfo.from_bytes(response.data)
            # Sensor is mounted upside down relative to the lens on some models
            self._mount_offset = mount_offset_degrees(self._device_info.product_id)
        return self._device_info


    @property
    def battery_level(self):
        return int(self.device_info(update=True).battery)


    @property
    def is_charging(self):
        return self.device_info(update=True).is_charging
    
    
    @property
    def vendor(self):
        return self.device_info(update=False).vendor


    @property
    def model(self):
        return self.device_info(update=False).product_id


    @property
    def version(self):
        return self.device_info(update=False).fw_version


    @property
    def ssid(self):
        return self.device_info(update=False).ssid
    

    @property
    def serial_num(self):
        return self.license.serial_num


    @property
    def capacity(self):
        return self.device_info(update=False).capacity


    @property
    def device_info_cached(self):
        """Device info without talking to the camera (None until first read)"""
        return self._device_info


    def status(self, refresh=True):
        """
        One device-info round trip for everything the UI needs (the individual
        properties would each trigger their own request)
        """
        info = self.device_info(update=refresh)
        return {
            'vendor': info.vendor,
            'model': info.product_id,
            'version': info.fw_version,
            'ssid': info.ssid,
            'battery': int(info.battery),
            'charging': bool(info.is_charging),
            'mount_offset': self._mount_offset,
        }


    def led(self, on=None):
        """
        Ring light control (command 0x000A). Call without arguments to read the
        current state, pass True/False to switch it. Payload is
        u8 led id | u8 status | u8 brightness, with 0x10 set for "write".
        """
        if on is None:
            payload = b'\x01\x00\x00'
        else:
            payload = b'\x11' + bytes([1 if on else 0, 100 if on else 0])
        msg = (b'\xee\xff\xee\xff\x00\x00\x0a\x00\x01\x00'
               + len(payload).to_bytes(2, 'little') + payload)
        response = self.send_command(msg)
        if response.err_code != 0 or len(response.data) < 3:
            raise LedUnsupported(f'err={response.err_code}, data={bytes(response.data)!r}')
        return {'on': bool(response.data[1]), 'brightness': int(response.data[2])}
        
        
        
if __name__ == '__main__':
    no_ssl_flag = '--no-ssl'
    if len(sys.argv) < 2 or (len(sys.argv) < 3 and no_ssl_flag not in sys.argv):
        print(f'\nUsage:\n\t{sys.argv[0]} {no_ssl_flag}\n\t{sys.argv[0]} <PEM certificate file> <private key file>\n')
        sys.exit()

    cert_fpath = None
    privkey_fpath = None
    if no_ssl_flag not in sys.argv:
        cert_fpath = sys.argv[1]
        privkey_fpath = sys.argv[2]

    client = SuearClient()
    try:
        st = client.status()
        print(f'Device: {st["vendor"]} {st["model"]} {st["version"]}  (Serial number: {client.serial_num})')
        print(f'Battery: {st["battery"]}% ({"C" if st["charging"] else "Not c"}harging), '
              f'mount offset {st["mount_offset"]}°')
    except Exception as e:
        # A camera that is switched off must not keep the viewer from starting
        print(f'WARNING: device not reachable ({type(e).__name__}: {e}) - '
              'serving anyway, status is refreshed once it comes online')

    client.mirror_http(cert_fpath, privkey_fpath)
