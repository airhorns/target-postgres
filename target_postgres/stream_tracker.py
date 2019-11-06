from collections import deque
import json
import singer
import singer.statediff as statediff
import sys

from target_postgres.exceptions import TargetError


LOGGER = singer.get_logger()


class StreamTracker:
    """
    Object to track the BufferedStream objects for each incoming stream to the target, and the STATE messages coming in. This object understands which streams need to be flushed before STATE messages can be safely emitted and does so.

    Because Singer taps don't have a standard way of expressing which streams correspond to which STATEs, the target can only safely
    emit a STATE message once all the records that came in prior to that STATE in the stream. Because target-postgres buffers
    the records in BufferedSingerStreams, the STATE messages need to be delayed until all the records that came before them have been
    saved to the database from their buffers.
    """

    def __init__(self, target, emit_states, max_watermark_lag=1000000):
        self.target = target
        self.emit_states = emit_states
        self.max_watermark_lag = max_watermark_lag

        self.streams = {}

        # dict of {'<stream_name>': number}, where the number is the message counter of the most recently received record for that stream. Will contain a value for all registered streams.
        self.stream_add_watermarks = {}

        # dict of {'<stream_name>': number}, where the number is the message counter of the most recently flushed record for that stream. Will contain a value for all registered streams.
        self.stream_flush_watermarks = {}

        self.streams_added_to = set()  # list of stream names which have seen records
        self.state_queue = deque()  # contains dicts of {'state': <state blob>, 'watermark': number}
        self.message_counter = 0
        self.last_emitted_state = None

    def register_stream(self, stream, buffered_stream):
        self.streams[stream] = buffered_stream
        self.stream_flush_watermarks[stream] = 0

    def flush_stream(self, stream):
        self._write_batch_and_update_watermarks(stream)
        self._emit_safe_queued_states()

    def flush_streams_if_required(self, force=False):
        streams_to_flush = set()
        for (stream, stream_buffer) in self.streams.items():
            if force or stream_buffer.buffer_full:
                streams_to_flush.add(stream)

        # Flush all streams if the watermark is really far behind to latch progress even if not all the
        # buffers are full. This is necessary when a tap is emitting a lot of records in one stream and
        # very few in another stream such that one buffer is filling up and flushing frequently, but the
        # other is not. That other buffer keeps the safe to emit watermark low until it fills, which it
        # might never, so in order to emit any state messages at all (before the whole tap is complete)
        # we need to eagerly flush all the buffers when the watermark is really far behind.
        if len(self.state_queue) > 0:
            watermark_lag = self.message_counter - self._safe_flush_threshold()
            if watermark_lag > self.max_watermark_lag:
                LOGGER.info("Eagerly flushing all buffers as max watermark lag has been encountered")
                force = True
                for stream in self.streams.keys():
                    streams_to_flush.add(stream)

        for stream in streams_to_flush:
            self._write_batch_and_update_watermarks(stream)

        self._emit_safe_queued_states(force=force)

    def handle_state_message(self, line_data):
        if self.emit_states:
            self.state_queue.append({'state': line_data['value'], 'watermark': self.message_counter})
            self._emit_safe_queued_states()

    def handle_record_message(self, stream, line_data):
        if stream not in self.streams:
            raise TargetError('A record for stream {} was encountered before a corresponding schema'.format(stream))

        self.message_counter += 1
        self.streams_added_to.add(stream)
        self.stream_add_watermarks[stream] = self.message_counter
        self.streams[stream].add_record_message(line_data)

    def _write_batch_and_update_watermarks(self, stream):
        stream_buffer = self.streams[stream]
        self.target.write_batch(stream_buffer)
        stream_buffer.flush_buffer()
        self.stream_flush_watermarks[stream] = self.stream_add_watermarks.get(stream, 0)

    def _safe_flush_threshold(self):
        # State messages that occured before the least recently flushed record are safe to emit.
        # If they occurred after some records that haven't yet been flushed, they aren't safe to emit.
        # Because records arrive at different rates from different streams, we take the earliest unflushed record
        # as the threshold for what STATE messages are safe to emit. We ignore the threshold of 0 for streams that
        # have been registered (via a SCHEMA message) but where no records have arrived yet.
        valid_flush_watermarks = []
        for stream, watermark in self.stream_flush_watermarks.items():
            if stream in self.streams_added_to:
                valid_flush_watermarks.append(watermark)
        return min(valid_flush_watermarks, default=0)

    def _emit_safe_queued_states(self, force=False):
        safe_flush_threshold = self._safe_flush_threshold()
        emittable_state = None

        while len(self.state_queue) > 0 and (force or self.state_queue[0]['watermark'] <= safe_flush_threshold):
            emittable_state = self.state_queue.popleft()['state']

        if emittable_state:
            if len(statediff.diff(emittable_state, self.last_emitted_state or {})) > 0:
                line = json.dumps(emittable_state)
                sys.stdout.write("{}\n".format(line))
                sys.stdout.flush()

            self.last_emitted_state = emittable_state
