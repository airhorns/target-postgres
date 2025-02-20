from collections import deque
import json
import sys
import singer.statediff as statediff


class TargetError(Exception):
    """
    Raise when there is an Exception streaming data to the target.
    """


class StreamTracker:
    """
    Object to track STATE messages coming in from the tap and which streams need to be flushed before they can be safely emitted.
    Because Singer taps don't have a standard way of expressing which streams correspond to which STATEs, the target can only safely
    emit a STATE message once all the records that came in prior to that STATE in the stream. Because target-postgres buffers
    the records in BufferedSingerStreams, the STATE messages need to be delayed until all the records that came before them have been
    saved to the database from their buffers.
    """

    def __init__(self, target, emit_states):
        self.target = target
        self.emit_states = emit_states

        self.streams = {}
        self.stream_add_watermarks = {}
        self.stream_flush_watermarks = {}

        self.state_queue = deque()  # contains tuples of (state, message_counter)
        self.message_counter = 0
        self.last_emitted_state = None

    def register_stream(self, stream, buffered_stream):
        self.streams[stream] = buffered_stream
        self.stream_add_watermarks[stream] = 0
        self.stream_flush_watermarks[stream] = 0

    def flush_streams(self, force=False):
        for (stream, stream_buffer) in self.streams.items():
            if force or stream_buffer.buffer_full:
                self.target.write_batch(stream_buffer)
                stream_buffer.flush_buffer()
                self.stream_flush_watermarks[stream] = self.stream_add_watermarks[stream]

        self._emit_safe_queued_states(force=force)

    def handle_state_message(self, value):
        if self.emit_states:
            self.state_queue.append((value, self.message_counter))
            self._emit_safe_queued_states()

    def handle_record_message(self, stream, line_data):
        if stream not in self.streams:
            raise TargetError('A record for stream {} was encountered before a corresponding schema'.format(stream))

        self.message_counter += 1
        self.stream_add_watermarks[stream] = self.message_counter
        self.streams[stream].add_record_message(line_data)

    def _emit_safe_queued_states(self, force=False):
        # State messages that occured before the least recently flushed record are safe to emit.
        # If they occurred after some records that haven't yet been flushed, they aren't safe to emit.
        # Because records arrive at different rates from different streams, we take the earliest unflushed record as the threshold for what
        # STATE messages are safe to emit.
        all_flushed_watermark = min(self.stream_flush_watermarks.values(), default=0)
        emittable_state = None

        while len(self.state_queue) > 0 and (force or self.state_queue[0][1] <= all_flushed_watermark):
            emittable_state = self.state_queue.popleft()[0]

        if emittable_state:
            if len(statediff.diff(emittable_state, self.last_emitted_state or {})) > 0:
                line = json.dumps(emittable_state)
                sys.stdout.write("{}\n".format(line))
                sys.stdout.flush()

            self.last_emitted_state = emittable_state