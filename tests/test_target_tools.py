from copy import deepcopy
import json

from unittest.mock import patch
import pytest

from target_postgres import singer_stream
from target_postgres import target_tools
from target_postgres.sql_base import SQLInterface

from fixtures import CONFIG, CatStream, InvalidCatStream


class Target(SQLInterface):
    IDENTIFIER_FIELD_LENGTH = 50

    def __init__(self):
        self.calls = {'write_batch': [], 'activate_version': []}

    def write_batch(self, stream_buffer):
        self.calls['write_batch'].append({'records_count': len(stream_buffer.peek_buffer())})
        return None

    def activate_version(self, stream_buffer, version):
        self.calls['activate_version'] += 1
        return None


def filtered_output(capsys):
    out, _ = capsys.readouterr()
    return list(filter(None, out.split('\n')))


def test_usage_stats():
    config = deepcopy(CONFIG)
    assert config['disable_collection']

    with patch.object(target_tools,
                      '_async_send_usage_stats') as mock:
        target_tools.stream_to_target([], None, config=config)

        assert mock.call_count == 0

        config['disable_collection'] = False

        target_tools.stream_to_target([], None, config=config)

        assert mock.call_count == 1


def test_loading__invalid__records():
    with pytest.raises(singer_stream.SingerStreamError, match=r'.*'):
        target_tools.stream_to_target(InvalidCatStream(1), None, config=CONFIG)


def test_loading__invalid__records__disable():
    config = deepcopy(CONFIG)
    config['invalid_records_detect'] = False

    target = Target()

    target_tools.stream_to_target(InvalidCatStream(100), target, config=config)

    ## Since all `cat`s records were invalid, we could not persist them, hence, no calls made to `write_batch`
    assert len(target.calls['write_batch']) == 1
    assert target.calls['write_batch'][0]['records_count'] == 0


def test_loading__invalid__records__threshold():
    config = deepcopy(CONFIG)
    config['invalid_records_threshold'] = 10

    target = Target()

    with pytest.raises(singer_stream.SingerStreamError, match=r'.*.10*'):
        target_tools.stream_to_target(InvalidCatStream(20), target, config=config)

    assert len(target.calls['write_batch']) == 0


def test_state__capture(capsys):
    stream = [
        json.dumps({'type': 'STATE', 'value': { 'test': 'state-1' }}),
        json.dumps({'type': 'STATE', 'value': { 'test': 'state-2' }})]

    target_tools.stream_to_target(stream, Target())
    output = filtered_output(capsys)

    assert len(output) == 2
    assert json.loads(output[0])['test'] == 'state-1'
    assert json.loads(output[1])['test'] == 'state-2'


def test_state__capture__can__be__disabled(capsys):
    stream = [
        json.dumps({'type': 'STATE', 'value': { 'test': 'state-1' }}),
        json.dumps({'type': 'STATE', 'value': { 'test': 'state-2' }})]

    target_tools.stream_to_target(stream, Target(), {'state_support': False})
    output = filtered_output(capsys)

    assert len(output) == 0


def test_state__emits__only__messages__when__all__records__before__have__been__flushed(capsys):
    config = CONFIG.copy()
    config['max_batch_rows'] = 20
    config['batch_detection_threshold'] = 1
    rows = list(CatStream(100))
    target = Target()

    def test_stream():
        yield rows[0]
        for row in rows[slice(1, 5)]:
            yield row
        yield json.dumps({'type': 'STATE', 'value': { 'test': 'state-1' }})
        for row in rows[slice(6, 10)]:
            yield row
        yield json.dumps({'type': 'STATE', 'value': { 'test': 'state-2' }})
        for row in rows[slice(11, 15)]:
            yield row
        yield json.dumps({'type': 'STATE', 'value': { 'test': 'state-3' }})

        # After some state messages but before the batch size has been hit no state messages should have been emitted
        assert len(target.calls['write_batch']) == 0
        output = filtered_output(capsys)
        assert output == []

        for row in rows[slice(16, 25)]:
            yield row
        yield json.dumps({'type': 'STATE', 'value': { 'test': 'state-4' }})

        # After the batch size has been hit and a write_batch call was made, the most recent safe to emit state should have been emitted
        assert len(target.calls['write_batch']) == 1
        output = filtered_output(capsys)
        assert len(output) == 1
        assert json.loads(output[0])['test'] == 'state-3'

        for row in rows[slice(26, 31)]:
            yield row

    target_tools.stream_to_target(test_stream(), target, config=config)

    # The final state message should have been outputted after the last records were loaded
    output = filtered_output(capsys)
    assert len(output) == 1
    assert json.loads(output[0])['test'] == 'state-4'


def test_state__emits__most__recent__state__when__final__flush__occurs(capsys):
    config = CONFIG.copy()
    config['max_batch_rows'] = 20
    config['batch_detection_threshold'] = 1
    rows = list(CatStream(5))
    rows.append(json.dumps({'type': 'STATE', 'value': { 'test': 'state-1' }}))

    target_tools.stream_to_target(rows, Target(), config=config)

    # The final state message should have been outputted after the last records were loaded despite not reaching
    # one full flushable batch
    output = filtered_output(capsys)
    assert len(output) == 1
    assert json.loads(output[0])['test'] == 'state-1'


class DogStream(CatStream):
    stream = 'dogs'
    schema = CatStream.schema.copy()


DogStream.schema['stream'] = 'dogs'


def test_state__doesnt__emit__when__only__one__of__several__streams__is__flushing(capsys):
    config = CONFIG.copy()
    config['max_batch_rows'] = 20
    config['batch_detection_threshold'] = 1
    cat_rows = list(CatStream(100))
    dog_rows = list(DogStream(50))
    target = Target()

    # Simulate one stream that yields a lot of records with another that yields few records and ensure both need to be flushed
    # before any state messages are emitted
    def test_stream():
        yield cat_rows[0]
        yield dog_rows[0]
        for row in cat_rows[slice(1, 5)]:
            yield row
        for row in dog_rows[slice(1, 5)]:
            yield row
        yield json.dumps({'type': 'STATE', 'value': { 'test': 'state-1' }})

        for row in cat_rows[slice(6, 45)]:
            yield row
        yield json.dumps({'type': 'STATE', 'value': { 'test': 'state-2' }})

        for row in cat_rows[slice(46, 65)]:
            yield row
        yield json.dumps({'type': 'STATE', 'value': { 'test': 'state-3' }})

        # After some state messages but before the batch size has been hit for both streams no state messages should have been emitted
        assert len(target.calls['write_batch']) == 3
        output = filtered_output(capsys)
        assert output == []

        for row in dog_rows[slice(6, 25)]:
            yield row
        yield json.dumps({'type': 'STATE', 'value': { 'test': 'state-4' }})

        # After the batch size has been hit and a write_batch call was made, the most recent safe to emit state should have been emitted
        assert len(target.calls['write_batch']) == 4
        output = filtered_output(capsys)
        assert len(output) == 1
        assert json.loads(output[0])['test'] == 'state-2'

    target_tools.stream_to_target(test_stream(), target, config=config)

    # The final state message should have been outputted after the last dog records were loaded despite not reaching one full flushable batch
    output = filtered_output(capsys)
    assert len(output) == 1
    assert json.loads(output[0])['test'] == 'state-4'