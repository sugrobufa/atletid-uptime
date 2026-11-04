import copy
import io
import ssl
import unittest
import urllib.error

import monitor

UP = {'ok': True, 'reason': 'http_200_ok', 'attempts': 1}
DOWN = {'ok': False, 'reason': 'timeout', 'attempts': 2}


class Store:
    def __init__(self, state=None):
        self.state = copy.deepcopy(state or monitor.initial_state())
        self.saves, self.fail_save = 0, False

    def load(self):
        return copy.deepcopy(self.state)

    def save(self, state, now):
        if self.fail_save:
            raise monitor.MonitorError('github_http_409')
        self.saves += 1
        state['last_persisted_at'] = now
        self.state = copy.deepcopy(state)


class Response(io.BytesIO):
    status = 200

    def geturl(self):
        return monitor.TARGET


class ExternalMonitorTests(unittest.TestCase):
    def test_healthy_baseline_and_unchanged_runs_stay_quiet(self):
        store, sent = Store(), []
        monitor.run(store, lambda t: sent.append(t), UP, 100)
        monitor.run(store, lambda t: sent.append(t), UP, 400)
        self.assertEqual(sent, [])
        self.assertEqual(store.saves, 1)

    def test_failure_recovery_once_across_restarts(self):
        store, sent = Store(), []
        def send(text):
            sent.append(text)
            self.assertEqual(store.state['events'][-1]['status'], 'pending')
            return len(sent)
        monitor.run(store, send, UP, 100)
        monitor.run(store, send, DOWN, 400)
        monitor.run(store, send, DOWN, 700)
        monitor.run(store, send, UP, 1000)
        monitor.run(store, send, UP, 1300)
        self.assertEqual(len(sent), 2)
        self.assertIn('недоступен', sent[0])
        self.assertIn('восстановлена', sent[1])

    def test_first_failure_is_actionable(self):
        store, sent = Store(), []
        report = monitor.run(store, lambda t: sent.append(t) or 10, DOWN, 100)
        self.assertEqual(report['delivered'], 1)

    def test_no_send_if_durable_state_save_fails(self):
        store, sent = Store(), []
        store.fail_save = True
        with self.assertRaises(monitor.MonitorError):
            monitor.run(store, lambda t: sent.append(t), DOWN, 100)
        self.assertEqual(sent, [])

    def test_pending_retry_survives_restart_and_recovery_order(self):
        store, sent = Store(), []
        def fail(_):
            raise monitor.DeliveryError('telegram_http_429', 600)
        report = monitor.run(store, fail, DOWN, 100)
        self.assertEqual(report['pendingDeliveries'], 1)
        monitor.run(store, lambda t: sent.append(t) or len(sent), UP, 400)
        self.assertEqual(sent, [])
        report = monitor.run(store, lambda t: sent.append(t) or len(sent), UP, 701)
        self.assertTrue(report['ok'])
        self.assertEqual(len(sent), 2)
        self.assertIn('недоступен', sent[0])
        self.assertIn('восстановлена', sent[1])

    def test_activation_test_is_labelled_and_idempotent(self):
        store, sent = Store(), []
        def send(text):
            sent.append(text)
            return len(sent)
        first = monitor.run(store, send, UP, 100, self_test=True)
        second = monitor.run(store, send, UP, 400, self_test=True)
        self.assertTrue(first['selfTestConfirmed'] and second['selfTestConfirmed'])
        self.assertEqual(len(sent), 2)
        self.assertTrue(all('ТЕСТ' in message for message in sent))
        self.assertEqual(store.state['status'], 'up')

    def test_daily_activity_without_normal_telegram_messages(self):
        store, sent = Store(), []
        monitor.run(store, lambda t: sent.append(t), UP, 100)
        monitor.run(store, lambda t: sent.append(t), UP, 100 + monitor.HEARTBEAT_INTERVAL)
        self.assertEqual(store.saves, 2)
        self.assertEqual(sent, [])

    def test_probe_retries_transient_failure_before_alert(self):
        values, sleeps = iter([DOWN, UP]), []
        result = monitor.probe(check=lambda: next(values), sleep=sleeps.append)
        self.assertTrue(result['ok'])
        self.assertEqual(result['attempts'], 2)
        self.assertEqual(sleeps, [20])

    def test_probe_accepts_only_true_boolean_json_health(self):
        for body, expected in [(b'{"ok":true}', True), (b'{"ok":1}', False),
                               (b'{"ok":"true"}', False), (b'[]', False),
                               (b'<html>up</html>', False)]:
            with self.subTest(body=body):
                self.assertEqual(monitor.probe_once(opener=lambda *a, **k: Response(body))['ok'], expected)

    def test_probe_tls_and_http_failure_are_sanitized(self):
        for error, reason in [(ssl.SSLError('private text'), 'tls_error'),
                              (urllib.error.HTTPError(monitor.TARGET, 503, 'private', {}, None), 'http_503')]:
            def fail(*args, **kwargs):
                raise error
            self.assertEqual(monitor.probe_once(opener=fail)['reason'], reason)

    def test_telegram_requires_confirmed_message_id(self):
        good = Response(b'{"ok":true,"result":{"message_id":123}}')
        self.assertEqual(monitor.telegram_send('SECRET', 'PRIVATE', 'test', opener=lambda *a, **k: good), 123)
        with self.assertRaisesRegex(monitor.DeliveryError, '^telegram_unconfirmed$'):
            monitor.telegram_send('SECRET', 'PRIVATE', 'test', opener=lambda *a, **k: Response(b'{"ok":false}'))

    def test_unknown_telegram_result_does_not_expose_token(self):
        def fail(*args, **kwargs):
            raise urllib.error.URLError('https://api.telegram.org/botSECRET/sendMessage')
        with self.assertRaises(monitor.DeliveryError) as result:
            monitor.telegram_send('SECRET', 'PRIVATE', 'test', opener=fail)
        self.assertNotIn('SECRET', str(result.exception))

    def test_corrupt_or_wrong_target_state_is_not_reset(self):
        state = monitor.initial_state()
        state['target'] = 'https://example.com'
        with self.assertRaises(monitor.MonitorError):
            monitor.validate_state(state)


if __name__ == '__main__':
    unittest.main()
