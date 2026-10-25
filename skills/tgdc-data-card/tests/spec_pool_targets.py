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
