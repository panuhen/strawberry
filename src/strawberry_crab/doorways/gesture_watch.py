"""Doorway: hand gestures through the webcam (WIRING.md §25). Opt-in: `[gestures] enabled = true`.

    camera (OpenCV)  ->  MediaPipe's gesture recogniser (CPU)  ->  gesture_track.Tracker  ->  POST /gesture, /hand

Strawberry owns the camera, and this process is the one place that opens it. A frame lives in memory for the
one recognition it is read for and is never written, logged or sent; what leaves the process is a gesture's
name, its phase and progress, and a hand's position as a few numbers (gesture_track.hand_message). The brain
decides what a gesture does (gestures.py); this only says what the hand did.

When the camera is open:

    [gestures] enabled = false    never; the process idles, reading its config file now and then
    watch = "always"              while enabled: idle_fps (a few frames a second) until a hand is up in the zone,
                                  then fps; the camera's light is on all the while
    watch = "armed"               only once the brain says so (`strawberry gestures arm`, an approval she shows),
                                  until armed_s after the last hand seen; then it is released

The tray starts it with the other doorways (`strawberry-doorway gesture_watch`); with gestures off it costs a
sleeping process. The tray's Gestures row (or `strawberry gestures on|off`) writes the setting and the watcher
follows the file. MediaPipe and OpenCV are the optional `gestures` dependencies, imported only when gestures
are on; the model is `strawberry gestures fetch`'s.
"""

from __future__ import annotations

import argparse
import http.client
import json
import logging
import signal
import sys
import threading
import time
import urllib.parse
from pathlib import Path
from typing import Any, Callable

from .. import bussecret, gestures
from ..winproc import utf8_streams
from .gesture_track import Hand, Settings, Tracker

log = logging.getLogger("gesture_watch")

WIDTH, HEIGHT = 640, 480     # asked of the camera; MediaPipe's models work on far less
STATE_EVERY_S = 1.0          # GET /gesture this often while no post brings the state back
CONFIG_EVERY_S = 1.0         # look at the config file's mtime this often
NO_FRAMES_S = 3.0            # a camera that gives nothing this long is released and opened again
RETRY_CAMERA_S = 10.0


class CameraError(OSError):
    pass


# --- the camera and the model (the seams the tests replace) -------------------------------


class OpenCvCamera:
    """A camera through OpenCV: RGB frames, one buffered at most (so a slow reader gets a fresh frame)."""

    def __init__(self, capture: Any, name: str) -> None:
        self.capture = capture
        self.name = name

    def read(self) -> Any:
        import cv2

        ok, frame = self.capture.read()
        if not ok or frame is None:
            return None
        return cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)

    def close(self) -> None:
        self.capture.release()


def open_camera(spec: str, fps: float) -> OpenCvCamera:
    """Open `spec` ("" = the first camera, "1" an index, "/dev/video2" a device). The tests replace this
    (tests/conftest.py): no test may open a real camera."""
    import cv2

    target: int | str = int(spec) if spec.strip().isdigit() else (spec.strip() or 0)
    backend = cv2.CAP_V4L2 if sys.platform.startswith("linux") else cv2.CAP_ANY
    capture = cv2.VideoCapture(target, backend)
    if not capture.isOpened():
        capture.release()
        raise CameraError(f"camera {spec or '0'} did not open (in use, or not there)")
    capture.set(cv2.CAP_PROP_FRAME_WIDTH, WIDTH)
    capture.set(cv2.CAP_PROP_FRAME_HEIGHT, HEIGHT)
    capture.set(cv2.CAP_PROP_FPS, max(fps, 5.0))
    capture.set(cv2.CAP_PROP_BUFFERSIZE, 1)
    return OpenCvCamera(capture, str(target))


class Recognizer:
    """MediaPipe's gesture recogniser on the CPU, in video mode: one hand, its 21 landmarks mirrored (x as the user
    sees themselves) and the top gesture category."""

    def __init__(self, model: Path) -> None:
        from mediapipe.tasks.python import vision
        from mediapipe.tasks.python.core.base_options import BaseOptions

        options = vision.GestureRecognizerOptions(
            base_options=BaseOptions(model_asset_path=str(model), delegate=BaseOptions.Delegate.CPU),
            running_mode=vision.RunningMode.VIDEO, num_hands=1)
        self.recognizer = vision.GestureRecognizer.create_from_options(options)
        self.last_ms = -1

    def recognize(self, rgb: Any, t: float) -> Hand | None:
        import mediapipe as mp

        ms = max(int(t * 1000), self.last_ms + 1)      # video mode wants rising timestamps
        self.last_ms = ms
        result = self.recognizer.recognize_for_video(mp.Image(image_format=mp.ImageFormat.SRGB, data=rgb), ms)
        if not result.hand_landmarks:
            return None
        points = tuple((1.0 - p.x, p.y) for p in result.hand_landmarks[0])
        label, score = "", 0.0
        if result.gestures and result.gestures[0]:
            top = result.gestures[0][0]
            label, score = str(top.category_name or ""), float(top.score or 0.0)
        return Hand(points, label, score)

    def close(self) -> None:
        self.recognizer.close()


# --- talking to the brain ---------------------------------------------------------------


class Poster:
    """POSTs and GETs to the daemon on one kept-alive connection, the bus secret on each (read from its file every
    time, as every first-party client does). None when the daemon did not answer."""

    def __init__(self, url: str, timeout: float = 1.5) -> None:
        parsed = urllib.parse.urlsplit(url)
        self.host, self.port = parsed.hostname or "127.0.0.1", parsed.port or 80
        self.timeout = timeout
        self.connection: http.client.HTTPConnection | None = None
        self.failures = 0

    def request(self, method: str, path: str, payload: dict | None = None) -> tuple[int, dict] | None:
        body = json.dumps(payload).encode() if payload is not None else None
        headers = bussecret.headers({"Content-Type": "application/json"} if body is not None else {})
        for attempt in (1, 2):    # a kept-alive connection the daemon closed: once more on a new one
            try:
                if self.connection is None:
                    self.connection = http.client.HTTPConnection(self.host, self.port, timeout=self.timeout)
                self.connection.request(method, path, body=body, headers=headers)
                response = self.connection.getresponse()
                data = response.read()
                self.failures = 0
                try:
                    reply = json.loads(data or b"{}")
                except json.JSONDecodeError:
                    reply = {}
                return response.status, reply if isinstance(reply, dict) else {}
            except (OSError, http.client.HTTPException) as exc:
                self.close()
                if attempt == 2:
                    self.failures += 1
                    if self.failures % 20 == 1:
                        log.warning("strawberryd unreachable at %s:%s (%s)", self.host, self.port, exc)
        return None

    def close(self) -> None:
        if self.connection is not None:
            self.connection.close()
            self.connection = None


# --- the watcher ------------------------------------------------------------------------


def load_settings(path: Path | None) -> Any:
    """[gestures] from the config file, validated as the daemon validates it (config.load)."""
    from ..config import load

    return load(path, env={}).gestures


class Watcher:
    def __init__(self, daemon: str, config_path: Path | None = None,
                 camera_opener: Callable[[str, float], Any] | None = None,
                 recognizer_factory: Callable[[Path], Any] | None = None, poster: Any = None,
                 clock: Callable[[], float] = time.monotonic, settings: Any = None) -> None:
        self.config_path = config_path
        self.open_camera = camera_opener or (lambda spec, fps: open_camera(spec, fps))
        self.make_recognizer = recognizer_factory or Recognizer
        self.poster = poster or Poster(daemon)
        self.clock = clock
        self.stopping = threading.Event()
        self.settings = settings           # a config.GesturesConfig; None until the file is read
        self.config_seen: tuple[int, int] | None = None
        self.config_checked = -1e9
        self.tracker = Tracker()
        self.camera: Any = None
        self.recognizer: Any = None
        self.camera_failed_at = -1e9
        self.last_frame_at = 0.0
        self.state: dict[str, Any] = {"enabled": False, "armed_s": 0.0, "approval": None,
                                      "wanted": {"gesture": False, "hand": False}}
        self.state_at = -1e9
        self.open_until = 0.0              # watch = "armed": the camera stays open until then
        self.hand_sent_at = -1e9
        self.hand_present = False
        self.said: set[str] = set()        # one-time log lines already written
        self.frames = 0
        self.posts = 0

    # --- settings ---------------------------------------------------------------

    def reload(self, now: float) -> None:
        """Read [gestures] again when the config file changed (the tray's row writes it)."""
        if self.settings is not None and now - self.config_checked < CONFIG_EVERY_S:
            return
        self.config_checked = now
        from ..config import ConfigError, default_path

        path = self.config_path or default_path()
        try:
            stat = path.stat()
            seen = (stat.st_mtime_ns, stat.st_size)
        except OSError:
            seen = (0, -1)
        if seen == self.config_seen and self.settings is not None:
            return
        try:
            fresh = load_settings(path)
        except ConfigError as exc:
            if self.settings is None:
                from ..config import GesturesConfig

                self.settings = GesturesConfig()     # off until the file loads
            self.once(f"config:{seen}", logging.WARNING, f"[gestures] not read ({exc}); keeping what I had")
            self.config_seen = seen
            return
        was = self.settings
        self.settings, self.config_seen = fresh, seen
        self.tracker.settings = Settings.from_config(fresh)
        if was is None or was.enabled != fresh.enabled:
            log.info("gestures %s (watch %s, camera %s)", "on" if fresh.enabled else "off", fresh.watch,
                     fresh.camera or "the first")
        if was is not None and (was.camera != fresh.camera or was.fps != fresh.fps):
            self.release("the camera setting changed")

    def once(self, key: str, level: int, message: str) -> None:
        if key not in self.said:
            self.said.add(key)
            log.log(level, "%s", message)

    # --- the loop ---------------------------------------------------------------

    def run(self) -> None:
        try:
            while not self.stopping.is_set():
                wait = self.tick()
                if wait > 0:
                    self.stopping.wait(wait)
        finally:
            self.release("stopping")
            self.poster.close()

    def tick(self) -> float:
        """One turn: what the camera should do now, and one frame if it is open. Returns how long to wait."""
        started = self.clock()
        self.reload(started)
        cfg = self.settings
        if cfg is None or not cfg.enabled:
            self.release("gestures are off")
            return CONFIG_EVERY_S
        missing = gestures.mediapipe_missing()
        if missing:
            self.once("deps", logging.WARNING, f"gestures are on, but {missing}")
            return 5.0
        if not gestures.model_ready():
            self.once("model", logging.WARNING, f"gestures are on, but the model is missing ({gestures.model_file()}): "
                                                "strawberry gestures fetch")
            return 5.0
        self.refresh_state(started)
        self.tracker.approval = self.state.get("approval")
        armed_s = float(self.state.get("armed_s") or 0.0)
        if armed_s > 0:
            self.tracker.arm(started + armed_s)
            self.open_until = max(self.open_until, started + armed_s)
        if self.tracker.approval is not None:
            self.open_until = max(self.open_until, started + 1.0)
        if cfg.watch == "armed":
            if started >= self.open_until:
                self.release("not armed")
                return 0.5
            self.tracker.arm(self.open_until)   # opened for commands: they count while it is open
        if not self.ensure_camera(started):
            return 1.0
        self.frame(started)
        rate = cfg.fps if self.tracker.active(started) or self.tracker.approval is not None else cfg.idle_fps
        return max(1.0 / rate - (self.clock() - started), 0.0)

    def ensure_camera(self, now: float) -> bool:
        if self.camera is not None:
            return True
        if now - self.camera_failed_at < RETRY_CAMERA_S:
            return False
        try:
            if self.recognizer is None:
                self.recognizer = self.make_recognizer(gestures.model_file())
            self.camera = self.open_camera(self.settings.camera, self.settings.fps)
        except Exception as exc:   # noqa: BLE001 - a busy camera or a broken model: say so, try again later
            self.camera_failed_at = now
            self.once(f"camera:{exc}", logging.WARNING, f"camera not opened: {exc} (trying again every "
                                                      f"{RETRY_CAMERA_S:.0f}s)")
            return False
        self.last_frame_at = now
        log.info("camera open (%s, watch %s)", self.settings.camera or "the first", self.settings.watch)
        return True

    def release(self, why: str) -> None:
        if self.camera is None:
            return
        try:
            self.camera.close()
        finally:
            self.camera = None
        log.info("camera released (%s)", why)
        if self.hand_present:
            self.send_hand({"present": False})
        self.tracker = Tracker(self.tracker.settings)

    def frame(self, now: float) -> None:
        rgb = self.camera.read()
        if rgb is None:
            if now - self.last_frame_at > NO_FRAMES_S:
                log.warning("no frames from the camera for %.0fs; opening it again", now - self.last_frame_at)
                self.release("no frames")
            return
        self.last_frame_at = now
        self.frames += 1
        try:
            hand = self.recognizer.recognize(rgb, now)
        except Exception as exc:   # noqa: BLE001 - a frame the model chokes on: let the camera go, try again later
            del rgb
            log.warning("the recogniser failed (%s); releasing the camera for a while", type(exc).__name__)
            self.release("the recogniser failed")
            self.camera_failed_at = now
            return
        del rgb                                   # the frame goes no further than this
        if hand is not None and self.settings.watch == "armed":
            self.open_until = max(self.open_until, now + self.settings.armed_s)
        for kind, payload in self.tracker.step(now, hand):
            if kind == "gesture":
                self.send_gesture(payload)
            elif self.state.get("wanted", {}).get("hand") or not payload.get("present", True):
                self.send_hand(payload, now)

    # --- posts --------------------------------------------------------------------

    def send_gesture(self, payload: dict[str, Any]) -> None:
        reply = self.poster.request("POST", "/gesture", payload)
        self.posts += 1
        if payload["phase"] == "done":
            outcome = (reply[1].get("action") or reply[1].get("refused") or "nothing") if reply and reply[0] == 200 \
                else f"refused ({reply[0]})" if reply else "no daemon"
            log.info("gesture %s -> %s", payload["name"], outcome)      # a name and a code, nothing about the hand
        self.take_state(reply)

    def send_hand(self, payload: dict[str, Any], now: float | None = None) -> None:
        present = bool(payload.get("present"))
        now = self.clock() if now is None else now
        if present and self.hand_present and now - self.hand_sent_at < 1.0 / self.settings.hand_hz:
            return
        if not present and not self.hand_present:
            return
        self.hand_sent_at, self.hand_present = now, present
        self.take_state(self.poster.request("POST", "/hand", payload))
        self.posts += 1

    def take_state(self, reply: tuple[int, dict] | None) -> None:
        if reply and reply[0] == 200 and isinstance(reply[1].get("state"), dict):
            self.state, self.state_at = reply[1]["state"], self.clock()

    def refresh_state(self, now: float) -> None:
        if now - self.state_at < STATE_EVERY_S:
            return
        reply = self.poster.request("GET", "/gesture")
        self.state_at = now
        if reply and reply[0] == 200:
            self.state = reply[1]
        elif reply:
            self.once(f"state:{reply[0]}", logging.WARNING, f"GET /gesture answered {reply[0]}")


def listen_for_stop(watcher: Watcher) -> None:
    """SIGTERM (the tray, systemd) and, on Windows, the stop event: release the camera and leave."""
    if sys.platform == "win32":
        from .. import winproc

        try:
            winproc.listen_for_stop(watcher.stopping.set)
        except OSError as exc:
            log.warning("no stop event (%s); only TerminateProcess can stop this process", exc)
        return
    signal.signal(signal.SIGTERM, lambda signum, frame: watcher.stopping.set())


def main() -> None:
    from ..config import ConfigError, default_path, load

    parser = argparse.ArgumentParser(description="Strawberry gesture doorway (camera -> MediaPipe -> /gesture)")
    parser.add_argument("--daemon", default="")
    parser.add_argument("--config", type=Path, default=None, help="the config file (default: the XDG one)")
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args()
    utf8_streams()
    logging.basicConfig(level=args.log_level.upper(), format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
                        datefmt="%H:%M:%S")
    daemon = args.daemon
    if not daemon:
        try:
            port = load(args.config or default_path(), env={}).daemon.port
        except ConfigError:
            port = 8770
        daemon = f"http://127.0.0.1:{port}"
    watcher = Watcher(daemon, args.config)
    listen_for_stop(watcher)
    try:
        watcher.run()
    except KeyboardInterrupt:
        pass
    log.info("stopped (%d frames read, %d posts)", watcher.frames, watcher.posts)


if __name__ == "__main__":
    main()
