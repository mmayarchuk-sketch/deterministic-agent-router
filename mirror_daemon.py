"""Один проход зеркала по расписанию: входящие своей нити, по одному письму.

Отдельный процесс и отдельный замок, чтобы проходы не наслаивались: один
проход поднимает модель и может идти дольше минуты, а расписание ждать не
умеет. Молчит, когда делать нечего: строка печатается только при исходе.
"""
import argparse
import fcntl
import json
from pathlib import Path

import mailroom

BASE = Path(__file__).resolve().parent
DEFAULT_CONFIG = BASE / 'mailroom-mirror.json'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, default=DEFAULT_CONFIG)
    args = parser.parse_args()
    cfg = json.loads(args.config.read_text())
    state = Path(cfg['state_directory'])
    state.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (state / 'mirror.lock').open('a') as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return                      # предыдущий проход ещё идёт
        итог = mailroom.run(cfg)
        if итог.get('status') != 'idle':
            print(json.dumps(итог, ensure_ascii=False), flush=True)


if __name__ == '__main__':
    main()
