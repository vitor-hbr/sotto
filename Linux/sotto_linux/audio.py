"""GStreamer capture through the desktop's PulseAudio/PipeWire compatibility service."""

import queue
import time

import gi

gi.require_version("Gst", "1.0")
gi.require_version("GstApp", "1.0")
from gi.repository import Gst, GstApp  # noqa: E402,F401

from .api import APIError


class Microphone:
    def __init__(self, stop):
        Gst.init(None)
        self.chunks = queue.Queue(maxsize=32)
        self.pending = bytearray()
        self.overflow = False
        self.stop = stop
        self.stopped = False
        self.last_sample = time.monotonic()
        # Negotiate the capture format explicitly: original and inference share
        # the same mono 16 kHz interval, with no lossy encoding or local files.
        self.pipeline = Gst.parse_launch(
            "pulsesrc ! audioconvert ! audioresample ! "
            "audio/x-raw,format=F32LE,rate=16000,channels=1,layout=interleaved ! "
            "appsink name=audio emit-signals=true sync=false max-buffers=4 drop=false")
        self.sink = self.pipeline.get_by_name("audio")
        self.sink.connect("new-sample", self.on_sample)

    def on_sample(self, sink):
        sample = sink.emit("pull-sample")
        if sample is None:
            return Gst.FlowReturn.ERROR
        if self.stop.is_set():
            return Gst.FlowReturn.OK
        buffer = sample.get_buffer()
        try:
            self.chunks.put_nowait(buffer.extract_dup(0, buffer.get_size()))
        except queue.Full:
            self.overflow = True
            return Gst.FlowReturn.ERROR
        return Gst.FlowReturn.OK

    def start(self):
        if self.pipeline.set_state(Gst.State.PLAYING) == Gst.StateChangeReturn.FAILURE:
            raise APIError("Microphone could not start. Check GNOME sound settings.")

    def read(self):
        if self.overflow:
            raise APIError("Audio upload could not keep up. Recording stopped; check the server connection.")
        message = self.pipeline.get_bus().pop_filtered(Gst.MessageType.ERROR | Gst.MessageType.EOS)
        if message is not None:
            raise APIError("Microphone disconnected or stopped. Check GNOME sound settings.")
        try:
            chunk = self.chunks.get(timeout=0.1)
            self.last_sample = time.monotonic()
            self.pending.extend(chunk)
            # Quarter-second uploads stay well below the server's 4096-chunk
            # limit even when the sound server supplies 10 ms buffers.
            if len(self.pending) >= 16000:
                chunk = bytes(self.pending[:16000])
                del self.pending[:16000]
                return chunk
            return None
        except queue.Empty:
            if self.stopped:
                chunk = bytes(self.pending)
                self.pending.clear()
                return chunk
            if time.monotonic() - self.last_sample > 5:
                raise APIError("No microphone audio received for five seconds.")
            return None

    def finish(self):
        self.pipeline.set_state(Gst.State.NULL)
        self.stopped = True

    def close(self):
        self.finish()
        self.pending.clear()
        while not self.chunks.empty():
            self.chunks.get_nowait()
