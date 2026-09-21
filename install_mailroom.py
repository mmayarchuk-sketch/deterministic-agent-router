"""Install Mailroom with the existing queue watcher retained as rollback."""
import argparse
import json
import os
from pathlib import Path
import plistlib
import subprocess

from outbox_watch import thread_state

BASE = Path(__file__).resolve().parent
DOMAIN = f'gui/{os.getuid()}'
LABEL = 'com.biosingularity.codex-mailroom'
OLD_LABEL = 'com.biosingularity.codex-outbox'
PLIST = Path.home() / 'Library/LaunchAgents' / f'{LABEL}.plist'
OLD_PLIST = Path.home() / 'Library/LaunchAgents' / f'{OLD_LABEL}.plist'
CONFIG = BASE / 'mailroom.json'


def loaded(label):
    return subprocess.run(['/bin/launchctl', 'print', f'{DOMAIN}/{label}'],
                          capture_output=True).returncode == 0


def bootout(label):
    if loaded(label):
        subprocess.run(['/bin/launchctl', 'bootout', f'{DOMAIN}/{label}'], check=True)


def bootstrap(plist):
    subprocess.run(['/bin/launchctl', 'bootstrap', DOMAIN, str(plist)], check=True)


def update_config(mode):
    cfg = json.loads(CONFIG.read_text())
    thread_id = os.environ.get('CODEX_THREAD_ID') or os.environ.get('CODEX_SESSION_ID')
    if mode == 'production' and not thread_id:
        raise RuntimeError('Run installation from the interactive Codex task')
    if thread_id:
        cfg['thread_id'] = thread_id
    cfg['mode'] = mode
    if mode == 'production' and not thread_state(cfg)['ready']:
        raise RuntimeError('Current Codex task is not a live queue target')
    tmp = CONFIG.with_suffix('.json.tmp')
    tmp.write_text(json.dumps(cfg, ensure_ascii=False, indent=2) + '\n')
    os.replace(tmp, CONFIG)
    return cfg


def install():
    cfg = update_config('production')
    state = Path(cfg['state_directory'])
    state.mkdir(parents=True, exist_ok=True, mode=0o700)
    PLIST.parent.mkdir(parents=True, exist_ok=True)
    job = {
        'Label': LABEL,
        'ProgramArguments': [str(BASE / '.venv/bin/python'), str(BASE / 'mailroom_daemon.py')],
        'WorkingDirectory': str(BASE), 'StartInterval': 60, 'RunAtLoad': True,
        'ProcessType': 'Background',
        'EnvironmentVariables': {'PATH': cfg['path']},
        'StandardOutPath': str(state / 'launchd.log'),
        'StandardErrorPath': str(state / 'launchd.err'),
    }
    tmp = PLIST.with_suffix('.plist.tmp')
    tmp.write_bytes(plistlib.dumps(job))
    tmp.chmod(0o644)
    os.replace(tmp, PLIST)
    subprocess.run(['/usr/bin/plutil', '-lint', str(PLIST)], check=True)
    bootout(LABEL)
    old_was_loaded = loaded(OLD_LABEL)
    if old_was_loaded:
        bootout(OLD_LABEL)
    try:
        bootstrap(PLIST)
    except Exception:
        update_config('shadow')
        if old_was_loaded and OLD_PLIST.exists() and not loaded(OLD_LABEL):
            bootstrap(OLD_PLIST)
        raise
    print(f'Installed {LABEL}; old watcher plist retained at {OLD_PLIST} for rollback.')


def uninstall():
    bootout(LABEL)
    update_config('shadow')
    if OLD_PLIST.exists() and not loaded(OLD_LABEL):
        bootstrap(OLD_PLIST)
    print(f'Disabled {LABEL}; restored {OLD_LABEL}.')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['install', 'uninstall', 'status'])
    args = parser.parse_args()
    if args.action == 'install':
        install()
    elif args.action == 'uninstall':
        uninstall()
    else:
        print(json.dumps({'mailroom_loaded': loaded(LABEL),
                          'rollback_watcher_loaded': loaded(OLD_LABEL),
                          'config': json.loads(CONFIG.read_text())}, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
