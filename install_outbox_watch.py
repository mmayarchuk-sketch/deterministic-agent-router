"""Install or remove the user's 60-second launchd mailbox check (no model timer)."""
import argparse
import json
import os
from pathlib import Path
import plistlib
import subprocess

BASE = Path(__file__).resolve().parent
LABEL = 'com.biosingularity.codex-outbox'
PLIST = Path.home() / 'Library/LaunchAgents' / f'{LABEL}.plist'
DOMAIN = f'gui/{os.getuid()}'


def bind_current_thread():
    """Bind installation to the Codex task that authorized it."""
    thread_id = os.environ.get('CODEX_THREAD_ID') or os.environ.get('CODEX_SESSION_ID')
    if not thread_id:
        raise RuntimeError('CODEX_THREAD_ID is missing; run installation from the target Codex task')
    config_path = BASE / 'outbox-watch.json'
    cfg = json.loads(config_path.read_text())
    cfg.update(thread_id=thread_id, codex_home=str(Path.home() / '.codex'),
               validate_thread=True)
    tmp = config_path.with_suffix('.json.tmp')
    tmp.write_text(json.dumps(cfg, ensure_ascii=False, indent=2) + '\n')
    os.replace(tmp, config_path)
    return thread_id


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['install', 'uninstall', 'status'])
    args = parser.parse_args()
    if args.action == 'status':
        subprocess.run(['/bin/launchctl', 'print', f'{DOMAIN}/{LABEL}'], check=True)
        return
    if args.action == 'uninstall':
        subprocess.run(['/bin/launchctl', 'bootout', f'{DOMAIN}/{LABEL}'], check=False)
        PLIST.unlink(missing_ok=True)
        return
    thread_id = bind_current_thread()
    state = BASE / 'state/outbox-watch'
    state.mkdir(parents=True, exist_ok=True, mode=0o700)
    PLIST.parent.mkdir(parents=True, exist_ok=True)
    job = {
        'Label': LABEL,
        'ProgramArguments': [str(BASE / '.venv/bin/python'), str(BASE / 'outbox_watch.py'),
                             '--config', str(BASE / 'outbox-watch.json')],
        'WorkingDirectory': str(BASE),
        'StartInterval': 60,
        'RunAtLoad': True,
        'ProcessType': 'Background',
        'EnvironmentVariables': {'PATH': '/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin'},
        'StandardOutPath': str(state / 'launchd.log'),
        'StandardErrorPath': str(state / 'launchd.err'),
    }
    existing = subprocess.run(['/bin/launchctl', 'print', f'{DOMAIN}/{LABEL}'],
                              capture_output=True)
    if existing.returncode == 0:
        subprocess.run(['/bin/launchctl', 'bootout', f'{DOMAIN}/{LABEL}'], check=True)
    tmp = PLIST.with_suffix('.plist.tmp')
    tmp.write_bytes(plistlib.dumps(job))
    tmp.chmod(0o644)
    os.replace(tmp, PLIST)
    subprocess.run(['/usr/bin/plutil', '-lint', str(PLIST)], check=True)
    subprocess.run(['/bin/launchctl', 'bootstrap', DOMAIN, str(PLIST)], check=True)
    print(f'Installed {LABEL}: local scan every 60 seconds; model only for new mail; '
          f'thread {thread_id}.')


if __name__ == '__main__':
    main()
