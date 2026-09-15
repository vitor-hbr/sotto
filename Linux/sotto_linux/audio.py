"""GStreamer capture through the desktop's PulseAudio/PipeWire compatibility service."""

import queue
import time
import array
import math

import gi

gi.require_version("Gst", "1.0")
gi.require_version("GstApp", "1.0")
from gi.repository import Gst, GstApp  # noqa: E402,F401

from .api import APIError


class Microphone:
    def __init__(self, stop, device="", level=None):
        Gst.init(None)
        self.chunks = queue.Queue(maxsize=32)
        self.pending = bytearray()
        self.original_chunks = queue.Queue(maxsize=64)
        self.original_pending = bytearray()
        self.original_format = None
        self.original_frames = 0
        self.keep_original = False
        self.overflow = False
        self.stop = stop
        self.stopped = False
        self.level = level
        self.last_sample = time.monotonic()
        # Split before resampling so retained original audio preserves the
        # source rate and channel layout. Both streams share one source clock.
        self.pipeline = Gst.parse_launch(
            "pulsesrc name=source ! audioconvert ! "
            "audio/x-raw,format=F32LE,layout=interleaved ! tee name=split "
            "split. ! queue ! appsink name=original emit-signals=true sync=false max-buffers=4 drop=false "
            "split. ! queue ! audioconvert ! audioresample ! "
            "audio/x-raw,format=F32LE,rate=16000,channels=1,layout=interleaved ! "
            "appsink name=audio emit-signals=true sync=false max-buffers=4 drop=false")
        self.sink = self.pipeline.get_by_name("audio")
        self.pipeline.get_by_name("original").connect("new-sample", self.on_original)
        self.pipeline.get_by_name("source").get_static_pad("src").add_probe(
            Gst.PadProbeType.BUFFER, lambda *_: Gst.PadProbeReturn.DROP if self.stop.is_set() else Gst.PadProbeReturn.OK)
        if device:
            self.pipeline.get_by_name("source").set_property("device", device)
        self.sink.connect("new-sample", self.on_sample)

    def on_sample(self, sink):
        sample = sink.emit("pull-sample")
        if sample is None:
            return Gst.FlowReturn.ERROR
        buffer = sample.get_buffer()
        try:
            self.chunks.put_nowait(buffer.extract_dup(0, buffer.get_size()))
        except queue.Full:
            self.overflow = True
            return Gst.FlowReturn.ERROR
        return Gst.FlowReturn.OK

    def on_original(self, sink):
        sample = sink.emit("pull-sample")
        if not self.keep_original:
            return Gst.FlowReturn.OK
        caps = sample.get_caps().get_structure(0)
        format = (caps.get_value("rate"), caps.get_value("channels"))
        if self.original_format is not None and self.original_format != format:
            self.overflow = True
            return Gst.FlowReturn.ERROR
        self.original_format = format
        buffer = sample.get_buffer()
        try:
            self.original_chunks.put_nowait(buffer.extract_dup(0, buffer.get_size()))
        except queue.Full:
            self.overflow = True
            return Gst.FlowReturn.ERROR
        return Gst.FlowReturn.OK

    def original_audio(self, inference_frames):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if self.overflow:
                raise APIError("Original audio upload could not keep up with capture.")
            if self.original_format:
                rate, channels = self.original_format
                target = inference_frames * rate // 16000
                size = (target - self.original_frames) * channels * 4
                if len(self.original_pending) >= size:
                    pcm = bytes(self.original_pending[:size])
                    del self.original_pending[:size]
                    self.original_frames = target
                    return pcm, rate, channels, target
            try:
                self.original_pending.extend(self.original_chunks.get(timeout=0.1))
            except queue.Empty:
                if self.stopped:
                    if self.original_format:
                        rate, channels = self.original_format
                        actual = self.original_frames + len(self.original_pending) // (channels * 4)
                        # A resampler can round its final output up by one frame.
                        # Keep actual source samples; never synthesize original audio.
                        if 0 <= inference_frames * rate // 16000 - actual <= math.ceil(rate / 16000):
                            pcm = bytes(self.original_pending)
                            self.original_pending.clear()
                            self.original_frames = actual
                            return pcm, rate, channels, actual
                    break
        raise APIError("Original and inference audio could not be aligned. Recording was not submitted.")

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
                if self.level:
                    samples = array.array("f", chunk)
                    self.level(min(1.0, math.sqrt(sum(x * x for x in samples) / len(samples)) * 4))
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
        if self.stopped:
            return
        self.stop.set()
        self.pipeline.send_event(Gst.Event.new_eos())
        message = self.pipeline.get_bus().timed_pop_filtered(2 * Gst.SECOND, Gst.MessageType.EOS | Gst.MessageType.ERROR)
        self.pipeline.set_state(Gst.State.NULL)
        self.stopped = True
        if message is None or message.type == Gst.MessageType.ERROR:
            raise APIError("The microphone could not finish its audio stream.")

    def close(self):
        self.pipeline.set_state(Gst.State.NULL)
        self.stopped = True
        self.pending.clear()
        self.original_pending.clear()
        while not self.chunks.empty():
            self.chunks.get_nowait()
        while not self.original_chunks.empty():
            self.original_chunks.get_nowait()


def microphones():
    Gst.init(None)
    monitor = Gst.DeviceMonitor()
    monitor.add_filter("Audio/Source", None)
    result = [("", "System default")]
    if not monitor.start():
        return result
    try:
        for device in monitor.get_devices():
            properties = device.get_properties()
            name = properties.get_string("device.name") if properties else None
            if name:
                result.append((name, device.get_display_name()))
    finally:
        monitor.stop()
    return result
