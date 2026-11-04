#!/usr/bin/env python3
"""Independent HTTPS/Telegram monitor; no access to the VPS or application data."""
from __future__ import annotations

import argparse
import base64
import datetime as dt
import json
import os
import socket
import ssl
import sys
import time
import urllib.error
import urllib.request
import uuid

TARGET = 'https://atletid.ru/api/health'
STATE_BRANCH = 'monitor-state'
HEARTBEAT_INTERVAL = 86400
RETRY_INTERVALS = (60, 300, 900, 1800)
TEST_ID = 'activation-v1'
MSK = dt.timezone(dt.timedelta(hours=3))


class MonitorError(Exception):
    """Only a fixed, non-sensitive code may be printed."""


class DeliveryError(MonitorError):
    def __init__(self, code, retry_after=0):
        super().__init__(code)
        self.retry_after = max(0, min(int(retry_after or 0), 86400))


def iso(now):
    return dt.datetime.fromtimestamp(now, dt.timezone.utc).isoformat()


def display_time(now):
    return dt.datetime.fromtimestamp(now, MSK).strftime('%d.%m.%Y %H:%M МСК')


def initial_state():
    return {'schema': 1, 'target': TARGET, 'status': None, 'events': [],
            'last_persisted_at': 0, 'self_tests': {}}


def validate_state(state):
    if (not isinstance(state, dict) or state.get('schema') != 1
            or state.get('target') != TARGET or state.get('status') not in (None, 'up', 'down')
            or not isinstance(state.get('events'), list)
            or not isinstance(state.get('self_tests'), dict)):
        raise MonitorError('invalid_monitor_state')
    if len(state['events']) > 1000:
        raise MonitorError('monitor_state_too_large')
    return state


def probe_once(url=TARGET, opener=urllib.request.urlopen):
    request = urllib.request.Request(url, headers={
        'User-Agent': 'AtletID-external-monitor/1.0',
        'Cache-Control': 'no-cache', 'Accept': 'application/json'})
    try:
        with opener(request, timeout=12) as response:
            if response.status != 200:
                return {'ok': False, 'reason': 'http_' + str(response.status)}
            if response.geturl() != url:
                return {'ok': False, 'reason': 'unexpected_redirect'}
            raw = response.read(65537)
            if len(raw) > 65536:
                return {'ok': False, 'reason': 'response_too_large'}
            body = json.loads(raw)
            if not isinstance(body, dict) or body.get('ok') is not True:
                return {'ok': False, 'reason': 'health_not_ok'}
            return {'ok': True, 'reason': 'http_200_ok'}
    except urllib.error.HTTPError as error:
        return {'ok': False, 'reason': 'http_' + str(error.code)}
    except (TimeoutError, socket.timeout):
        return {'ok': False, 'reason': 'timeout'}
    except ssl.SSLError:
        return {'ok': False, 'reason': 'tls_error'}
    except urllib.error.URLError as error:
        return {'ok': False, 'reason': 'timeout' if isinstance(error.reason, TimeoutError)
                else 'network_or_tls_error'}
    except (ValueError, UnicodeError):
        return {'ok': False, 'reason': 'invalid_json'}


def probe(check=probe_once, sleep=time.sleep):
    first = check()
    if first['ok']:
        return {**first, 'attempts': 1}
    sleep(20)
    return {**check(), 'attempts': 2}


class GithubStore:
    def __init__(self, repository, token):
        self.repository, self.token, self.sha = repository, token, None

    def request(self, method, path, payload=None, missing_ok=False):
        data = None if payload is None else json.dumps(payload).encode()
        request = urllib.request.Request('https://api.github.com/repos/' + self.repository + path,
            method=method, data=data, headers={
                'Authorization': 'Bearer ' + self.token,
                'Accept': 'application/vnd.github+json', 'Content-Type': 'application/json',
                'X-GitHub-Api-Version': '2022-11-28', 'User-Agent': 'AtletID-external-monitor'})
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                return json.load(response)
        except urllib.error.HTTPError as error:
            if missing_ok and error.code == 404:
                return None
            raise MonitorError('github_http_' + str(error.code)) from None
        except (OSError, ValueError):
            raise MonitorError('github_network_or_invalid_response') from None

    def load(self):
        data = self.request('GET', '/contents/state.json?ref=' + STATE_BRANCH, missing_ok=True)
        if data is None:
            # A missing state is expected only on a new, existing state branch.
            # API/branch errors must not reset notification history.
            self.request('GET', '/git/ref/heads/' + STATE_BRANCH)
            return initial_state()
        self.sha = data['sha']
        try:
            return validate_state(json.loads(base64.b64decode(data['content'])))
        except (ValueError, KeyError):
            raise MonitorError('invalid_monitor_state') from None

    def save(self, state, now):
        state['last_persisted_at'] = now
        payload = {'message': 'monitor: persist status and notification outbox',
                   'branch': STATE_BRANCH,
                   'content': base64.b64encode((json.dumps(state, ensure_ascii=False, indent=2)
                                               + '\n').encode()).decode()}
        if self.sha:
            payload['sha'] = self.sha
        data = self.request('PUT', '/contents/state.json', payload)
        self.sha = data['content']['sha']


def queue_event(state, event_id, kind, text, now):
    if any(event['id'] == event_id for event in state['events']):
        return False
    state['events'].append({'id': event_id, 'kind': kind, 'text': text,
                            'created_at': iso(now), 'status': 'pending',
                            'attempts': 0, 'next_attempt': now})
    return True


def observe(state, result, now):
    status, previous = 'up' if result['ok'] else 'down', state['status']
    state['last_check_at'], state['last_probe'] = iso(now), result
    if status == previous:
        return False
    state['status'], state['last_transition_at'] = status, iso(now)
    if previous is None and status == 'up':
        return True
    if status == 'down':
        text = ('🚨 AtletID: сайт или VPS недоступен извне\n' + display_time(now)
                + '\nДве проверки с паузой 20 секунд не прошли.\n'
                + TARGET + '\nПричина: ' + result['reason']
                + '\nПроверка выполняется на GitHub, отдельно от VPS.')
    else:
        text = ('✅ AtletID: доступность восстановлена\n' + display_time(now)
                + '\n' + TARGET + ' отвечает HTTP 200, ok=true.\nПроверка с GitHub.')
    queue_event(state, uuid.uuid4().hex, status, text, now)
    return True


def queue_self_test(state, now):
    if TEST_ID in state['self_tests']:
        return False
    state['self_tests'][TEST_ID] = {'created_at': iso(now)}
    queue_event(state, 'self-test:' + TEST_ID + ':down', 'test',
                '🧪 ТЕСТ внешнего мониторинга AtletID\nУсловный сбой VPS.\n'
                'Сообщение отправлено с GitHub, независимо от VPS.\n'
                'Рабочий сервер продолжает работать.', now)
    queue_event(state, 'self-test:' + TEST_ID + ':up', 'test',
                '🧪 ТЕСТ внешнего мониторинга AtletID\nУсловное восстановление.\n'
                'Проверка уведомлений о сбое и восстановлении завершена.', now)
    return True


def telegram_send(token, chat, text, opener=urllib.request.urlopen):
    request = urllib.request.Request('https://api.telegram.org/bot' + token + '/sendMessage',
        data=json.dumps({'chat_id': chat, 'text': text, 'disable_web_page_preview': True}).encode(),
        headers={'Content-Type': 'application/json'})
    try:
        with opener(request, timeout=15) as response:
            data = json.load(response)
    except urllib.error.HTTPError as error:
        try:
            body = json.load(error)
        except (ValueError, OSError):
            body = {}
        raise DeliveryError('telegram_http_' + str(error.code),
                            body.get('parameters', {}).get('retry_after', 0)) from None
    except (OSError, ValueError):
        raise DeliveryError('telegram_network_or_unknown_result') from None
    message_id = data.get('result', {}).get('message_id')
    if data.get('ok') is not True or not isinstance(message_id, int):
        raise DeliveryError('telegram_unconfirmed')
    return message_id


def run(store, sender, result, now, self_test=False):
    state = store.load()
    changed = observe(state, result, now)
    if self_test:
        changed = queue_self_test(state, now) or changed
    pending = [event for event in state['events'] if event['status'] != 'sent']
    sent = [event for event in state['events'] if event['status'] == 'sent']
    if len(sent) > 100:
        state['events'] = sent[-100:] + pending
        changed = True
    if len(pending) > 100:
        raise MonitorError('notification_backlog_too_large')
    if changed or now - state.get('last_persisted_at', 0) >= HEARTBEAT_INTERVAL:
        store.save(state, now)
    delivered = 0
    for event in state['events']:
        if event['status'] == 'sent':
            continue
        # Preserve order: a recovery never overtakes its failed alert.
        if event['next_attempt'] > now:
            break
        event['attempts'] += 1
        event['next_attempt'] = now + RETRY_INTERVALS[min(event['attempts'] - 1, 3)]
        event['last_attempt_at'] = iso(now)
        # Persist the retry claim before sending. A dead runner retries later.
        store.save(state, now)
        try:
            message_id = sender(event['text'])
        except DeliveryError as error:
            event['last_error'] = str(error)
            event['next_attempt'] = max(event['next_attempt'], now + error.retry_after)
            store.save(state, now)
            break
        event.update(status='sent', message_id=message_id, sent_at=iso(now))
        event.pop('last_error', None)
        store.save(state, now)
        delivered += 1
    pending = sum(event['status'] != 'sent' for event in state['events'])
    return {'ok': pending == 0, 'checkedAt': iso(now), 'status': state['status'],
            'probe': result, 'delivered': delivered, 'pendingDeliveries': pending,
            'selfTestConfirmed': all(any(e['id'] == 'self-test:' + TEST_ID + ':' + suffix
                                         and e['status'] == 'sent' for e in state['events'])
                                     for suffix in ('down', 'up'))}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--mode', choices=('monitor', 'self-test'), default='monitor')
    args = parser.parse_args()
    try:
        token, chat = os.environ['TELEGRAM_BOT_TOKEN'], os.environ['TELEGRAM_CHAT_ID']
        if not token or not chat:
            raise MonitorError('missing_telegram_secrets')
        store = GithubStore(os.environ['GITHUB_REPOSITORY'], os.environ['GH_TOKEN'])
        report = run(store, lambda text: telegram_send(token, chat, text), probe(), time.time(),
                     self_test=args.mode == 'self-test')
        print(json.dumps(report))
        summary_path = os.environ.get('GITHUB_STEP_SUMMARY')
        if summary_path:
            with open(summary_path, 'a', encoding='utf-8') as output:
                output.write('External AtletID monitor: **' + report['status'] + '**\n\n'
                             + 'Checked: ' + report['checkedAt'] + '\n\n'
                             + 'Pending Telegram deliveries: ' + str(report['pendingDeliveries']) + '\n')
        return 0 if report['ok'] else 1
    except Exception as error:
        # SDK error strings can contain Telegram URLs and tokens.
        code = str(error) if isinstance(error, MonitorError) else type(error).__name__
        print(json.dumps({'ok': False, 'errorCode': code}))
        return 1


if __name__ == '__main__':
    sys.exit(main())
