"""Ворота транспорта: письмо берут в работу только после сигнала живому адресату.

22.09.2026 письмо ветви A к Астре прожило в ящике десять секунд: узкий агент
успел забрать его раньше, чем доставщик пришёл сообщить адресату. Доставщик в
это же время сутки стучался в архивную нить и писал отказ в состояние, которое
читать должен был тот, до кого отказ и не доходил.

Отсюда правило (D-CODEX-0025, выбор варианта В): разрешение обрабатывать письмо
даёт ДОКАЗАННЫЙ сигнал — долговечная запись доставщика об этой точной версии
`(имя, полный sha256)`, ушедшей в действующую живую нить с успешным `queued`.
Нет доказательства — fail closed, письмо не трогаем. Обхода по времени нет:
молчание транспорта не становится разрешением, сколько бы ни прошло минут.
"""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import time
import uuid

ЗАСЕЧКА = 3               # неуспешных проходов подряд до объявления аварии
ПОВТОР_СЕКУНД = 30 * 60   # не чаще раза в полчаса, пока транспорт лежит
ПРИГОДНЫ = ('queued',)    # только успешно поставленное в очередь живой нити


def живая_нить(thread_id, codex_home):
    """Жива ли нить: есть файл сессии и нет её же в архиве."""
    if not thread_id:
        return False, 'missing_thread_id'
    дом = Path(codex_home).expanduser()
    архив, живые = дом / 'archived_sessions', дом / 'sessions'
    if архив.is_dir() and any(архив.glob('*%s*.jsonl' % thread_id)):
        return False, 'archived'
    if живые.is_dir() and any(живые.rglob('*%s*.jsonl' % thread_id)):
        return True, 'live'
    return False, 'unknown_thread'


def доказательство_сигнала(name, sha256, *, watcher_state, expected_thread):
    """Долговечное доказательство, что адресату сообщили об ЭТОЙ версии.

    Совпадения одного имени недостаточно: имя без sha — это другая версия
    письма, а сигнал о другой версии разрешения не даёт.
    """
    путь = Path(watcher_state)
    if not путь.exists():
        return {'ok': False, 'reason': 'no_state'}
    try:
        состояние = json.loads(путь.read_text())
        партии = состояние['batches']
    except (ValueError, OSError, KeyError, TypeError):
        return {'ok': False, 'reason': 'corrupt_state'}
    # Состояние может быть испорчено не только синтаксисом: «batches» строкой
    # или числом — такая же порча, и она обязана давать отказ, а не падение.
    if not isinstance(партии, list):
        return {'ok': False, 'reason': 'corrupt_state'}
    партии = [x for x in партии if isinstance(x, dict)]
    нашли_имя = False
    for партия in партии:
        файлы = партия.get('files') or []
        точные = [f for f in файлы
                  if isinstance(f, dict) and f.get('name') == name]
        if точные:
            нашли_имя = True
        точные = [f for f in точные if f.get('sha256') == sha256]
        if not точные:
            continue
        if партия.get('status') not in ПРИГОДНЫ:
            continue
        if партия.get('thread_id') != expected_thread:
            continue
        return {'ok': True, 'batch': партия.get('id'), 'attempt': партия.get('attempt_id'),
                'thread_id': партия.get('thread_id'), 'status': партия.get('status')}
    return {'ok': False, 'reason': 'version_not_signalled' if нашли_имя else 'no_signal'}


def открыты(name, sha256, cfg):
    """Можно ли брать это письмо в работу. Любая неясность — закрыто."""
    ворота = cfg.get('delivery_gate') or {}
    if not ворота:
        return {'ok': False, 'reason': 'gate_not_configured'}
    try:
        настройка = json.loads(Path(ворота['watcher_config']).read_text())
    except (ValueError, OSError, KeyError, TypeError):
        return {'ok': False, 'reason': 'no_watcher_config'}
    нить = настройка.get('thread_id')
    жива, состояние_нити = живая_нить(нить, ворота.get('codex_home')
                                      or настройка.get('codex_home', '~/.codex'))
    if not жива:
        return {'ok': False, 'reason': 'thread_' + состояние_нити, 'thread_id': нить}
    путь = ворота.get('watcher_state') or (Path(настройка['state_directory']) / 'watch.json')
    ответ = доказательство_сигнала(name, sha256, watcher_state=путь, expected_thread=нить)
    ответ.setdefault('thread_id', нить)
    return ответ


ТРАНСПОРТНЫЕ = {'no_state', 'corrupt_state', 'no_signal', 'version_not_signalled',
                'thread_archived', 'thread_unknown_thread', 'thread_missing_thread_id',
                'no_watcher_config', 'gate_not_configured'}


def _уведомить_нативно(текст, cfg, runner=subprocess.run):
    """Всплывание macOS. Отдельный путь: его отказ не отменяет запись."""
    try:
        r = runner(['/usr/bin/osascript', '-e',
                    'display notification %s with title "Почта: транспорт"'
                    % json.dumps(текст[:200])],
                   capture_output=True, text=True, timeout=20)
        return {'ok': r.returncode == 0, 'error': (r.stderr or '').strip()[:200]}
    except Exception as сбой:
        return {'ok': False, 'error': repr(сбой)[:200]}


def _записать_владельцу(инцидент, cfg):
    """Долговечный путь: файл в owner-outbox, который разбирает owner_watch."""
    try:
        папка = Path(cfg['owner_outbox'])
        папка.mkdir(parents=True, exist_ok=True)
        путь = папка / ('transport-%s.json' % инцидент['incident_id'])
        if not путь.exists():
            tmp = путь.with_suffix('.tmp')
            tmp.write_text(json.dumps(
                {'schema_version': 1, 'for_human': True, 'kind': 'transport_failed',
                 'subject': 'Почта: сигналы живому адресату не доходят',
                 'body': инцидент['body'], 'incident_id': инцидент['incident_id'],
                 'source_name': инцидент.get('name', '-'),
                 'source_sha256': инцидент.get('sha256', '-'),
                 'created_at': инцидент['at']}, ensure_ascii=False, indent=2),
                encoding='utf-8')
            os.replace(tmp, путь)
        return {'ok': True, 'path': str(путь)}
    except Exception as сбой:
        return {'ok': False, 'error': repr(сбой)[:200]}


def проход(cfg, ответ, *, name=None, sha256=None, now=None, runner=subprocess.run):
    """Учесть один проход и, если транспорт лежит три подряд, объявить аварию.

    Авария доставляется ДВУМЯ независимыми путями. Отказ одного не отменяет
    второго: сегодня мы уже видели, как единственный путь уведомления сам же
    и оказался сломанным.
    """
    now = time.time() if now is None else float(now)
    путь = Path(cfg['state_directory']) / 'transport-gate.json'
    путь.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        сост = json.loads(путь.read_text())
    except (ValueError, OSError):
        сост = {}
    сост.setdefault('consecutive', 0)
    if ответ.get('ok'):
        закрыт = None
        if сост.get('incident_id'):
            закрыт = сост['incident_id']
            сост['closed_at'] = now
        сост.update(consecutive=0, incident_id=None, last_reason=None)
        путь.write_text(json.dumps(сост, ensure_ascii=False, indent=2), encoding='utf-8')
        return {'incident': None, 'closed': закрыт}
    if ответ.get('reason') not in ТРАНСПОРТНЫЕ:
        return {'incident': None, 'closed': None}
    сост['consecutive'] += 1
    сост['last_reason'] = ответ.get('reason')
    инцидент = None
    пора = (сост['consecutive'] >= ЗАСЕЧКА
            and (not сост.get('announced_at')
                 or now - сост['announced_at'] >= ПОВТОР_СЕКУНД))
    if пора:
        if not сост.get('incident_id'):
            сост['incident_id'] = hashlib.sha256(
                ('%s|%s' % (ответ.get('reason'), now)).encode()).hexdigest()[:16]
        инцидент = {'incident_id': сост['incident_id'], 'at': now,
                    'reason': ответ.get('reason'), 'name': name, 'sha256': sha256,
                    'body': ('Доставщик не подтвердил сигнал живому адресату: %s. '
                             'Нить %s. Письма остаются в ящике, обработка остановлена '
                             '(fail closed). Проходов подряд: %d.'
                             % (ответ.get('reason'), ответ.get('thread_id'),
                                сост['consecutive']))}
        инцидент['durable'] = _записать_владельцу(инцидент, cfg)
        инцидент['native'] = _уведомить_нативно(инцидент['body'], cfg, runner)
        сост['announced_at'] = now
        сост['last_paths'] = {'durable': инцидент['durable']['ok'],
                              'native': инцидент['native']['ok']}
    путь.write_text(json.dumps(сост, ensure_ascii=False, indent=2), encoding='utf-8')
    return {'incident': инцидент, 'closed': None, 'consecutive': сост['consecutive']}
