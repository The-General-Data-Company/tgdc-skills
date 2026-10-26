"""Worker targets that misbehave on purpose, for the pool tests."""
import os
import time
from multiprocessing import reduction


def misbehaving_worker(connection, settings):
    while True:
        try:
            task = connection.recv()
        except EOFError:
            break
        if task is None:
            break
        for _ in range(task['handles']):
            os.close(reduction.recv_handle(connection))
        name = task['name']
        if name.startswith('hang_after_hash'):
            connection.send(('hash', 'abc123'))
            time.sleep(600)
        elif name.startswith('hang_before_hash'):
            time.sleep(600)
        elif name.startswith('crash'):
            connection.send(('hash', 'def456'))
            os._exit(1)
        else:
            connection.send(('hash', 'ok'))
            connection.send(('done', {'status': 'ok', 'sha': 'ok'}))


def hold_a_tool(work, tool):
    """Child process: start a tool through a Reader, as a worker would, then wait to be terminated."""
    import io
    import spec_archives
    spec_archives.install_termination_handler()
    reader = spec_archives.Reader(work, tools={'7zz': tool})
    reader.open_archive('7z', 'x.7z', io.BytesIO(b'7z\xbc\xaf\x27\x1c' + b'\x00' * 100), 1, '', compressed=106)
    time.sleep(60)


def _good_reply(connection, task, extra=None):
    for _ in range(task['handles']):
        os.close(reduction.recv_handle(connection))
    connection.send(('hash', 'ok'))
    connection.send(('done', {'status': 'ok', 'sha': 'ok', **(extra or {})}))


def dies_at_first_start(connection, settings):
    """The first worker process that starts dies at once; later ones behave."""
    marker = settings['marker']
    if not os.path.exists(marker):
        open(marker, 'w').close()
        os._exit(1)
    while True:
        try:
            task = connection.recv()
        except EOFError:
            break
        if task is None:
            break
        _good_reply(connection, task)


def always_dies(connection, settings):
    os._exit(1)


def temp_worker(connection, settings):
    """Writes into the temporary folder the parent made for the task."""
    while True:
        try:
            task = connection.recv()
        except EOFError:
            break
        if task is None:
            break
        for _ in range(task['handles']):
            os.close(reduction.recv_handle(connection))
        name = task['name']
        temp = task.get('temp')
        if temp:
            with open(os.path.join(temp, 'scratch'), 'w') as handle:
                handle.write('x')
        if name.startswith('hang'):
            connection.send(('hash', 'abc'))
            time.sleep(600)
        elif name.startswith('crash'):
            connection.send(('hash', 'def'))
            os._exit(1)
        else:
            connection.send(('hash', 'ok'))
            connection.send(('done', {'status': 'ok', 'sha': 'ok', 'wrote': bool(temp)}))


def members_worker(connection, settings):
    while True:
        try:
            task = connection.recv()
        except EOFError:
            break
        if task is None:
            break
        for _ in range(task['handles']):
            os.close(reduction.recv_handle(connection))
        connection.send(('hash', 'ok'))
        connection.send(('members', (0, [{'n': 'one'}, {'n': 'two'}])))
        connection.send(('members', (2, [{'n': 'three'}])))
        connection.send(('done', {'status': 'ok', 'sha': 'ok', 'arch': {'kind': 'zip', 'n': 3}}))
