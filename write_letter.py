"""Записать письмо в ящик ОДНИМ атомарным приёмом.

22.09.2026 письмо `b081` ушло к рецензенту одной шапкой: я написала файл в
два приёма, а съёмщик забрал его между ними. Проверка устойчивости у него
честная — между двумя командами файл был устойчив по-настоящему. Правило
«писать целиком» разговором не обеспечивается, поэтому оно здесь: тело
читается из stdin, файл собирается в памяти и появляется в ящике готовым.
"""
import argparse
from datetime import datetime, timezone
import json
import re
import sys

import mail_channel as channel


def compose(letter_id, sender, recipient, subject, body, reply_to='-', needs_reply=True):
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,80}', letter_id or ''):
        raise ValueError('идентификатор: 1–80 букв, цифр, дефисов и подчёркиваний')
    if not (subject or '').strip() or '\n' in subject:
        raise ValueError('тема — одна строка и не пустая')
    if not (body or '').strip():
        raise ValueError('тело пустое: письмо без тела письмом не считается')
    now = datetime.now(timezone.utc)
    header = {'id': letter_id, 'from': sender, 'to': recipient, 'subject': subject,
              'created_utc': now.isoformat(timespec='seconds').replace('+00:00', 'Z'),
              'reply_to': reply_to, 'needs_reply': bool(needs_reply)}
    текст = ('---\n'
             + '\n'.join('%s: %s' % (k, json.dumps(v, ensure_ascii=False))
                         for k, v in header.items())
             + '\n---\n\n' + body.replace('\r\n', '\n').rstrip('\n') + '\n')
    slug = re.sub(r'[^a-z0-9]+', '-', subject.lower()).strip('-')[:32] or 'message'
    имя = '%s--%s--%s.md' % (now.strftime('%Y-%m-%dT%H-%M-%SZ'), slug, letter_id)
    return имя, текст


def write(mailbox, letter_id, subject, body, reply_to='-', needs_reply=True):
    sender, recipient = ('claude', 'codex') if mailbox == 'outbox' else ('codex', 'claude')
    имя, текст = compose(letter_id, sender, recipient, subject, body, reply_to,
                         needs_reply)
    with channel.locked():
        путь = channel.safe_file(channel._mailbox(mailbox), имя)
        if путь.exists():
            raise ValueError('письмо с таким именем уже лежит в ящике: %s' % имя)
        # один atomic_write: в ящике файл появляется сразу целым
        channel.atomic_write(путь, текст)
    return путь


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--id', required=True)
    p.add_argument('--subject', required=True)
    p.add_argument('--reply-to', default='-')
    p.add_argument('--mailbox', default='outbox')
    p.add_argument('--no-reply-needed', action='store_true')
    a = p.parse_args()
    путь = write(a.mailbox, a.id, a.subject, sys.stdin.read(), a.reply_to,
                 not a.no_reply_needed)
    print(путь)


if __name__ == '__main__':
    main()
