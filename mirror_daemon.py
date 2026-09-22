"""Один проход зеркала по расписанию: входящие своей нити, по одному письму.

Отдельный процесс и отдельный замок, чтобы проходы не наслаивались: один
проход поднимает модель и может идти дольше минуты, а расписание ждать не
умеет. Молчит, когда делать нечего: строка печатается только при исходе.
"""
import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import time

import mailroom

BASE = Path(__file__).resolve().parent
DEFAULT_CONFIG = BASE / 'mailroom-mirror.json'


def отметка(state, cfg, итог, начало):
    """След живости: проход был, вот когда, кем и чем кончился.

    Без него «ошибок нет» неотличимо от «ничего не запускается» — ровно та
    подмена признака, на которой мой наблюдатель уже врал в сентябре.
    Пишется КАЖДЫЙ проход, включая холостой.
    """
    путь = Path(cfg.get('config_path', '')) if cfg.get('config_path') else None
    запись = {'at_utc': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
              'pid': os.getpid(), 'seconds': round(time.time() - начало, 2),
              'status': итог.get('status'), 'scope': итог.get('scope'),
              'mode': cfg.get('mode'), 'eligible_names': len((cfg.get('eligible') or {}).get('names') or []),
              'process_from_utc': cfg.get('process_from_utc'),
              'config_sha256': путь and hashlib.sha256(путь.read_bytes()).hexdigest()}
    if итог.get('status') not in ('idle', None):
        запись['name'] = итог.get('name')
    (state / 'heartbeat.json').write_text(
        json.dumps(запись, ensure_ascii=False, indent=2), encoding='utf-8')
    with (state / 'heartbeat.log').open('a', encoding='utf-8') as f:
        f.write(json.dumps(запись, ensure_ascii=False) + '\n')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, default=DEFAULT_CONFIG)
    args = parser.parse_args()
    cfg = json.loads(args.config.read_text())
    cfg['config_path'] = str(args.config.resolve())
    state = Path(cfg['state_directory'])
    state.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (state / 'mirror.lock').open('a') as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return                      # предыдущий проход ещё идёт
        начало = time.time()
        try:
            итог = mailroom.run(cfg)
        except Exception as сбой:
            отметка(state, cfg, {'status': 'error', 'error': repr(сбой)[:300]}, начало)
            raise
        отметка(state, cfg, итог, начало)
        if итог.get('status') != 'idle':
            print(json.dumps(итог, ensure_ascii=False), flush=True)


if __name__ == '__main__':
    main()
