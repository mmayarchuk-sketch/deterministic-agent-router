"""One launchd pass: deliver owner escalations, then process one agent letter."""
import json
from pathlib import Path
import fcntl

import mailroom
import owner_watch

BASE = Path(__file__).resolve().parent


def main():
    cfg = json.loads((BASE / 'mailroom.json').read_text())
    state = Path(cfg['state_directory'])
    state.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (state / 'daemon.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return
        owner = owner_watch.run(cfg)
        agent = mailroom.run(cfg)
        if owner['status'] != 'idle' or agent['status'] != 'idle':
            print(json.dumps({'owner': owner, 'agent': agent}, ensure_ascii=False), flush=True)


if __name__ == '__main__':
    main()
