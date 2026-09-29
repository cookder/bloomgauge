"""Notification settings, alert detectors, channel fan-out and the Mac relay queue."""

import json
import threading
import time
import unittest
import urllib.error
from unittest.mock import Mock

import test_remote
from alerts import Alerts, event_key, without_amounts
from history import History
from live_earnings import EarningsPulse
from notify_settings import DEFAULTS, NotificationSettings, merged, quiet_now

ACCOUNT, DEVICE = 'acct', 'mac-1'
NOON = time.mktime((2026, 9, 28, 12, 0, 0, 0, 0, -1))


def fake_push(ready=True, accept=True):
    push = Mock()
    push.enqueue_notice.return_value = accept
    push.enqueue_switch.return_value = accept
    push.has_event.return_value = False
    push.ready.return_value = ready
    return push


class Fixture(unittest.TestCase):
    def setUp(self):
        self.h = History(':memory:')
        self.addCleanup(self.h.close)
        EarningsPulse(self.h, None)  # pulse_rates
        self.settings = NotificationSettings(self.h)
        self.push = fake_push()
        self.alerts = Alerts(self.h, self.settings, self.push, clock=lambda: NOON)

    def mac_items(self):
        return self.alerts.poll(ACCOUNT, 'granted', NOON)['items']

    def save(self, **change):
        return self.settings.save(change)

    def rates(self, start, end, rate, account=ACCOUNT, session=1):
        with self.h.lock:
            self.h.db.executemany(
                'INSERT OR REPLACE INTO pulse_rates VALUES(?,?,?,?,?)',
                [(account, session, at, rate, rate) for at in range(int(start), int(end))],
            )
            self.h.db.commit()


class SettingsTests(Fixture):
    def test_defaults_and_partial_saves(self):
        self.assertEqual(self.settings.get(), DEFAULTS)
        out = self.save(
            kinds={'demandSpike': {'phone': True}},
            earnings={'usdPerHour': 0.25},
            quietHours={'enabled': True},
        )
        self.assertEqual(out['kinds']['demandSpike'], {'mac': False, 'phone': True})
        self.assertEqual(out['earnings'], {'usdPerHour': 0.25, 'minutes': 30})
        self.assertTrue(out['quietHours']['enabled'])
        self.assertEqual(NotificationSettings(self.h).get(), out)  # persisted

    def test_invalid_values_are_rejected_with_plain_messages(self):
        for change in (
            {'kinds': {'nope': {'mac': True}}},
            {'kinds': {'problems': {'email': True}}},
            {'kinds': {'problems': {'mac': 1}}},
            {'earnings': {'usdPerHour': 0}},
            {'earnings': {'usdPerHour': float('nan')}},
            {'earnings': {'usdPerHour': 11}},
            {'earnings': {'minutes': 33}},
            {'earnings': {'minutes': 30.0}},
            {'earnings': {'minutes': 300}},
            {'quietHours': {'start': '24:00'}},
            {'quietHours': {'start': '7:00'}},
            {'quietHours': {'end': '22:00'}},
            {'phoneAmounts': 'yes'},
            {'extra': 1},
            [],
        ):
            with self.assertRaises(ValueError, msg=change):
                merged(DEFAULTS, change)
        self.assertEqual(self.settings.get(), DEFAULTS)

    def test_a_damaged_saved_part_keeps_its_default_only(self):
        self.h.cache(
            'notification-settings',
            {'kinds': 'broken', 'earnings': {'usdPerHour': 0.5, 'minutes': 60}},
        )
        out = self.settings.get()
        self.assertEqual(out['kinds'], DEFAULTS['kinds'])
        self.assertEqual(out['earnings'], {'usdPerHour': 0.5, 'minutes': 60})

    def test_quiet_hours_cross_midnight_in_local_time(self):
        s = merged(DEFAULTS, {'quietHours': {'enabled': True, 'start': '22:00', 'end': '07:00'}})
        at = lambda h, m=0: time.mktime((2026, 9, 28, h, m, 0, 0, 0, -1))
        self.assertTrue(quiet_now(s, at(23)))
        self.assertTrue(quiet_now(s, at(6, 59)))
        self.assertFalse(quiet_now(s, at(7)))
        self.assertFalse(quiet_now(s, at(12)))
        day = merged(s, {'quietHours': {'start': '12:00', 'end': '13:00'}})
        self.assertTrue(quiet_now(day, at(12, 30)))
        self.assertFalse(quiet_now(day, at(13)))
        self.assertFalse(quiet_now(merged(s, {'quietHours': {'enabled': False}}), at(23)))

    def test_network_news_phone_switch_is_the_catalog_push_setting(self):
        catalog = Mock()
        catalog.push_status.return_value = {'enabled': False}
        settings = NotificationSettings(self.h, catalog)
        settings.save({'kinds': {'networkNews': {'mac': True}}})
        catalog.set_push.assert_called_once_with({'action': 'push', 'enabled': True})
        catalog.push_status.return_value = {'enabled': True}
        self.assertEqual(settings.get()['kinds']['networkNews'], {'mac': True, 'phone': False})
        # Turned off on the network news card: both channels read off.
        catalog.push_status.return_value = {'enabled': False}
        self.assertEqual(settings.get()['kinds']['networkNews'], {'mac': False, 'phone': False})
        # Turned on there (nothing chosen here): that is the phone switch.
        self.h.cache('notification-settings', DEFAULTS)
        catalog.push_status.return_value = {'enabled': True}
        self.assertEqual(settings.get()['kinds']['networkNews'], {'mac': False, 'phone': True})


class FanOutTests(Fixture):
    def test_both_channels_once_per_event(self):
        self.assertTrue(self.alerts.send(ACCOUNT, 'problems', 'e1', 'Title', 'Body'))
        self.assertTrue(self.alerts.send(ACCOUNT, 'problems', 'e1', 'Title', 'Body'))
        self.push.enqueue_notice.assert_called_once_with(ACCOUNT, 'e1', 'Title', 'Body', screen='test')
        items = self.mac_items()
        self.assertEqual([(i['title'], i['body'], i['kind']) for i in items], [('Title', 'Body', 'problems')])
        self.assertEqual(self.alerts.recent(ACCOUNT)[0]['channels'], ['mac', 'phone'])

    def test_turned_off_and_quiet_hours_are_not_sent(self):
        self.save(kinds={'modelSwitched': {'mac': False, 'phone': False}})
        self.assertEqual(self.alerts.send(ACCOUNT, 'modelSwitched', 'e1', 'T', 'B'), 'off')
        self.assertTrue(self.alerts.channel('modelSwitched').enqueue_notice(ACCOUNT, 'e1', 'T', 'B'))
        self.save(quietHours={'enabled': True, 'start': '11:00', 'end': '13:00'})
        self.assertEqual(self.alerts.send(ACCOUNT, 'earningsHigh', 'e2', 'T', 'B'), 'quiet')
        # Held: sources retry while the alert is fresh.
        self.assertFalse(self.alerts.channel('earningsHigh').enqueue_notice(ACCOUNT, 'e2', 'T', 'B'))
        self.push.enqueue_notice.assert_not_called()
        self.assertEqual(self.mac_items(), [])
        self.assertEqual(self.alerts.recent(ACCOUNT), [])
        # Problems still come through quiet hours.
        self.assertEqual(self.alerts.send(ACCOUNT, 'problems', 'e3', 'T', 'B'), 'sent')
        self.assertEqual(len(self.mac_items()), 1)
        self.save(quietHours={'enabled': False})
        self.assertEqual(self.alerts.send(ACCOUNT, 'earningsHigh', 'e2', 'T', 'B'), 'sent')

    def test_phone_shows_amounts_unless_turned_off(self):
        self.assertIs(DEFAULTS['phoneAmounts'], True)  # Andrew, Sep 28
        self.alerts.send(ACCOUNT, 'earningsHigh', 'e1', 'T', '$0.40/h', phone_body='above')
        self.assertEqual(self.push.enqueue_notice.call_args.args[3], '$0.40/h')
        self.save(phoneAmounts=False)
        self.alerts.send(ACCOUNT, 'earningsHigh', 'e2', 'T', '$0.50/h', phone_body='above')
        self.assertEqual(self.push.enqueue_notice.call_args.args[3], 'above')
        self.assertEqual(self.mac_items()[-1]['body'], '$0.50/h')  # the Mac always shows them

    def test_a_busy_phone_queue_is_retried_without_a_second_mac_copy(self):
        self.push.enqueue_notice.return_value = False
        self.assertEqual(self.alerts.send(ACCOUNT, 'problems', 'e1', 'T', 'B'), 'retry')
        self.push.enqueue_notice.return_value = True
        self.assertEqual(self.alerts.send(ACCOUNT, 'problems', 'e1', 'T', 'B'), 'sent')
        self.assertEqual(self.push.enqueue_notice.call_count, 2)
        self.assertEqual(len(self.mac_items()), 1)
        self.assertEqual(self.alerts.recent(ACCOUNT)[0]['channels'], ['mac', 'phone'])

    def test_phone_only_waits_for_a_phone_that_can_receive(self):
        self.save(kinds={'problems': {'mac': False}})
        self.push.ready.return_value = False
        self.assertEqual(self.alerts.send(ACCOUNT, 'problems', 'e1', 'T', 'B'), 'nobody')
        self.push.enqueue_notice.assert_not_called()
        self.push.ready.return_value = True
        self.assertEqual(self.alerts.send(ACCOUNT, 'problems', 'e1', 'T', 'B'), 'sent')
        # With the Mac on, a phone that cannot receive does not hold the alert back.
        self.save(kinds={'problems': {'mac': True}})
        self.push.ready.return_value = False
        self.assertEqual(self.alerts.send(ACCOUNT, 'problems', 'e2', 'T', 'B'), 'sent')

    def test_other_notices_lose_their_amounts_on_the_phone(self):
        self.save(phoneAmounts=False)  # amount-free phone text (the setting turned off); on by default
        body = 'Excursions paused. The last week earned $0.42 less than staying home. Turn them on in Manage.'
        self.alerts.send(ACCOUNT, 'problems', 'e1', 'T', body)
        self.assertEqual(
            self.push.enqueue_notice.call_args.args[3],
            'Excursions paused. Turn them on in Manage. Open BloomGauge for the amounts.',
        )
        self.assertEqual(self.mac_items()[0]['body'], body)
        self.assertEqual(without_amounts('$1 only.'), 'Open BloomGauge for the details.')
        self.assertEqual(without_amounts('No money here. At all.'), 'No money here. At all.')
        # Network news quotes public network estimates, as before.
        self.save(kinds={'networkNews': {'phone': True}})
        self.alerts.send(ACCOUNT, 'networkNews', 'e2', 'T', 'about $0.40/h on Macs like yours.')
        self.assertEqual(self.push.enqueue_notice.call_args.args[3], 'about $0.40/h on Macs like yours.')

    def test_excursion_switches_are_told_by_switch_considered_where_it_is_on(self):
        channel = self.alerts.channel('modelSwitched')
        self.assertTrue(channel.enqueue_switch(ACCOUNT, 'row1', 'gemma', 'qwen', 'excursion'))
        self.push.enqueue_switch.assert_not_called()
        self.assertEqual(self.mac_items(), [])
        self.save(kinds={'switchConsidered': {'phone': False}})
        self.assertTrue(channel.enqueue_switch(ACCOUNT, 'row2', 'gemma', 'qwen', 'excursion'))
        self.push.enqueue_switch.assert_called_once_with(ACCOUNT, 'row2', 'gemma', 'qwen', 'excursion')
        self.assertEqual(self.mac_items(), [])
        self.assertTrue(channel.enqueue_switch(ACCOUNT, 'row3', 'qwen', 'gemma', 'home_return'))
        self.assertEqual(len(self.mac_items()), 1)
        self.assertTrue(channel.has_event(ACCOUNT, 'row3'))


class MacRelayTests(Fixture):
    def test_poll_ack_expiry_and_account_scope(self):
        self.alerts.send(ACCOUNT, 'problems', 'e1', 'T', 'B', screen='nowhere')
        self.alerts.send('other', 'problems', 'e1', 'T', 'B')
        reply = self.alerts.poll(ACCOUNT, 'granted', NOON)
        self.assertEqual(reply['pollSeconds'], 15)
        [item] = reply['items']
        self.assertEqual(item['id'], event_key(ACCOUNT, 'problems', 'e1'))
        self.assertEqual(item['screen'], 'test')
        self.assertEqual(self.alerts.poll(ACCOUNT, 'granted', NOON + 901)['items'], [])
        self.alerts.ack([item['id']], NOON + 1)
        self.assertEqual(self.mac_items(), [])
        status = self.alerts.mac_status(ACCOUNT, NOON + 60)
        self.assertEqual(
            (status['available'], status['permission'], status['lastDeliveredAt'], status['pending']),
            (True, 'granted', NOON + 1, 0),
        )
        self.assertFalse(self.alerts.mac_status(ACCOUNT, NOON + 121)['available'])

    def test_mac_test_is_rate_limited(self):
        self.alerts.test_mac(ACCOUNT, NOON)
        with self.assertRaises(ValueError):
            self.alerts.test_mac(ACCOUNT, NOON + 5)
        self.alerts.test_mac(ACCOUNT, NOON + 11)
        self.assertEqual([i['kind'] for i in self.mac_items()], ['test', 'test'])


class EarningsTests(Fixture):
    def check(self, at):
        self.alerts.earnings_at = None
        self.alerts.check_earnings(ACCOUNT, at, 'gemma-4')

    def test_sustained_pace_alerts_once_with_amounts_on_the_mac_only(self):
        self.save(phoneAmounts=False)  # amounts turned off for the phone; on by default
        self.rates(NOON - 1800, NOON, 0.42)
        self.check(NOON)
        self.check(NOON + 60)
        [item] = self.mac_items()
        self.assertEqual(item['screen'], 'overview')
        self.assertIn('$0.42/h over the last 30 min (your alert: $0.30/h), serving gemma-4', item['body'])
        phone = self.push.enqueue_notice.call_args.args[3]
        self.assertNotIn('$', phone)

    def test_brief_spikes_and_thin_coverage_do_not_alert(self):
        self.rates(NOON - 1800, NOON - 600, 0.10)
        self.rates(NOON - 600, NOON, 1.50)  # one 10-min burst: the window's mean is 0.57
        self.check(NOON)
        self.rates(NOON - 1800, NOON - 1200, 1.0, account='gap')
        self.alerts.earnings_at = None
        self.alerts.check_earnings('gap', NOON, None)  # a third of the window covered
        self.assertEqual(self.mac_items(), [])
        self.push.enqueue_notice.assert_not_called()

    def test_a_new_episode_needs_half_an_hour_below_and_at_most_three_a_day(self):
        self.save(earnings={'minutes': 10})
        at = NOON
        told = 0
        for episode in range(5):
            self.rates(at - 600, at, 0.5)
            self.check(at)
            self.rates(at, at + 3600, 0.05)
            for step in range(0, 3600, 300):
                self.check(at + step)
            at += 3600
            told = self.push.enqueue_notice.call_count
        self.assertEqual(told, 3)
        # Staying high is one episode.
        self.push.enqueue_notice.reset_mock()
        tomorrow = NOON + 86400
        self.rates(tomorrow - 600, tomorrow + 3600, 0.5)
        for step in range(0, 3600, 300):
            self.check(tomorrow + step)
        self.assertEqual(self.push.enqueue_notice.call_count, 1)

    def test_quiet_hours_hold_an_episode_until_they_end(self):
        self.save(quietHours={'enabled': True, 'start': '11:00', 'end': '12:05'})
        self.rates(NOON - 1800, NOON + 900, 0.5)
        self.check(NOON)
        self.check(NOON + 60)
        self.assertEqual(self.push.enqueue_notice.call_count, 0)
        self.check(NOON + 360)
        self.check(NOON + 420)
        self.assertEqual(self.push.enqueue_notice.call_count, 1)
        self.assertEqual(len(self.alerts.poll(ACCOUNT, 'granted', NOON + 420)['items']), 1)

    def test_pace_view_for_the_settings_hint(self):
        self.rates(NOON - 900, NOON, 0.2)
        pace = self.alerts.earnings_pace(ACCOUNT, NOON, 30)
        self.assertAlmostEqual(pace['usdPerHour'], 0.2)
        self.assertAlmostEqual(pace['coverage'], 0.5)
        self.assertEqual(pace['halves'][0], None)
        self.assertIsNone(self.alerts.earnings_pace('none', NOON, 30)['usdPerHour'])


class ManagerTests(Fixture):
    def optimizer(self, manager, view=None):
        o = Mock()
        o.lock = threading.RLock()
        o.state = {'manager': manager}
        o.last_demand_decision = {'manager': view or {}}
        return o

    def test_first_arming_check_then_excursion_start(self):
        self.save(phoneAmounts=False)  # amounts turned off for the phone; on by default
        arming = {'model': 'qwen', 'since': NOON - 60, 'checks': 1, 'lastCheckAt': NOON - 60}
        view = {'home': {'model': 'gemma'}, 'arming': {'model': 'qwen', 'ratio': 2.34}}
        o = self.optimizer({'arming': arming, 'home': {'model': 'gemma'}}, view)
        self.alerts.check_manager(ACCOUNT, NOON, o)
        self.alerts.check_manager(ACCOUNT, NOON + 5, o)
        [item] = self.mac_items()
        self.assertIn('qwen paying 2.3x gemma', item['body'])
        self.assertEqual(item['title'], 'BloomGauge · considering a switch')
        excursion = {
            'target': 'qwen',
            'from': 'gemma',
            'startedAt': NOON + 3600,
            'predictedUsdPerHour': 0.31,
        }
        o = self.optimizer({'excursion': excursion, 'home': {'model': 'gemma'}}, view)
        self.alerts.check_manager(ACCOUNT, NOON + 3610, o)
        self.alerts.check_manager(ACCOUNT, NOON + 3615, o)
        [item] = self.alerts.poll(ACCOUNT, 'granted', NOON + 3615)['items']  # the arming one expired
        self.assertIn('Now serving qwen (was gemma) to catch high demand; expected about $0.31/h', item['body'])
        self.assertNotIn('$', self.push.enqueue_notice.call_args.args[3])
        # Over or stale: nothing new.
        self.alerts.check_manager(ACCOUNT, NOON + 3700, self.optimizer({'excursion': {**excursion, 'endReason': 'x'}}))
        self.alerts.check_manager(ACCOUNT, NOON + 9000, self.optimizer({'excursion': excursion}))
        self.assertEqual(self.push.enqueue_notice.call_count, 2)

    def test_old_or_restarted_arming_is_not_news_for_three_hours(self):
        old = {'model': 'qwen', 'since': NOON - 7200, 'checks': 1, 'lastCheckAt': NOON - 7200}
        self.alerts.check_manager(ACCOUNT, NOON, self.optimizer({'arming': old}))
        self.push.enqueue_notice.assert_not_called()
        first = {'model': 'qwen', 'since': NOON, 'checks': 1, 'lastCheckAt': NOON}
        self.alerts.check_manager(ACCOUNT, NOON, self.optimizer({'arming': first}))
        again = {'model': 'qwen', 'since': NOON + 7200, 'checks': 1, 'lastCheckAt': NOON + 7200}
        self.alerts.check_manager(ACCOUNT, NOON + 7200, self.optimizer({'arming': again}))
        later = {'model': 'qwen', 'since': NOON + 11000, 'checks': 1, 'lastCheckAt': NOON + 11000}
        self.alerts.check_manager(ACCOUNT, NOON + 11000, self.optimizer({'arming': later}))
        other = {'model': 'llama', 'since': NOON + 7200, 'checks': 1, 'lastCheckAt': NOON + 7200}
        self.alerts.check_manager(ACCOUNT, NOON + 7200, self.optimizer({'arming': other}))
        self.assertEqual(self.push.enqueue_notice.call_count, 3)
        self.assertIn('paying more than your home model', self.push.enqueue_notice.call_args_list[0].args[3])


class SpikeTests(Fixture):
    def test_demand_spikes_when_turned_on(self):
        with self.h.lock:
            self.h.db.execute("""CREATE TABLE demand_alert_events(
                id INTEGER PRIMARY KEY,account TEXT,device TEXT,model TEXT,at REAL,payload TEXT)""")
            self.h.db.execute(
                'INSERT INTO demand_alert_events VALUES(1,?,?,?,?,?)',
                (ACCOUNT, DEVICE, 'qwen', NOON - 30, json.dumps({'pressureRatio': 3.1})),
            )
        self.alerts.check_spikes(ACCOUNT, DEVICE, NOON)
        self.assertEqual(self.mac_items(), [])  # off by default
        self.save(kinds={'demandSpike': {'mac': True}})
        self.alerts.check_spikes(ACCOUNT, DEVICE, NOON)  # still fresh: told once turned on
        self.alerts.check_spikes(ACCOUNT, DEVICE, NOON + 5)
        [item] = self.mac_items()
        self.assertEqual((item['screen'], item['body']), ('demand', 'qwen: demand is 3.1x its usual level on Darkbloom right now.'))
        with self.h.lock:
            for i in range(3, 12):
                self.h.db.execute(
                    'INSERT INTO demand_alert_events VALUES(?,?,?,?,?,?)',
                    (i, ACCOUNT, DEVICE, 'm%d' % i, NOON, '{}'),
                )
        self.alerts.check_spikes(ACCOUNT, DEVICE, NOON)
        self.assertEqual(len(self.alerts.poll(ACCOUNT, 'granted', NOON)['items']), 10)  # a batch
        self.assertEqual(self.alerts.mac_status(ACCOUNT, NOON)['pending'], 10)
        self.assertIn('well above', self.mac_items()[-1]['body'])


class NotificationHTTPTests(unittest.TestCase):
    setUp = test_remote.RemoteHTTPTests.setUp
    read = test_remote.RemoteHTTPTests.read
    denied = test_remote.RemoteHTTPTests.denied

    def tearDown(self):
        for server in (self.local, self.phone):
            server.shutdown()
            server.server_close()
        collector = self.local.RequestHandlerClass.collector
        collector.close()
        collector.history.close()
        self.tmp.cleanup()

    def post(self, server, path, body, headers):
        with self.read(server, path, headers, json.dumps(body).encode()) as response:
            return json.load(response)

    def test_settings_read_save_and_mac_test(self):
        collector = self.local.RequestHandlerClass.collector
        collector.account = ACCOUNT
        with self.read(self.local, '/api/notifications') as response:
            view = json.load(response)
        self.assertEqual(view['settings'], DEFAULTS)
        self.assertEqual(set(view), {'settings', 'defaults', 'limits', 'mac', 'phone', 'earningsNow', 'recent', 'remote'})
        self.assertFalse(view['remote'])
        headers = {'Content-Type': 'application/json', 'X-Bloom-Action': 'notifications'}
        out = self.post(self.local, '/api/notifications', {'action': 'save', 'settings': {'phoneAmounts': True}}, headers)
        self.assertTrue(out['settings']['phoneAmounts'])
        with self.assertRaises(urllib.error.HTTPError) as bad:
            self.post(self.local, '/api/notifications', {'action': 'save', 'settings': {'earnings': {'minutes': 7}}}, headers)
        self.assertEqual(bad.exception.code, 400)
        self.assertIn('10 to 240 minutes', json.load(bad.exception)['error'])
        self.denied(self.local, '/api/notifications', {**headers, 'X-Bloom-Action': 'demand-alerts'}, b'{}')
        self.post(self.local, '/api/notifications', {'action': 'test', 'channel': 'mac'}, headers)
        self.assertEqual(collector.alerts.mac_status(ACCOUNT, time.time())['pending'], 1)
        # The phone may change settings (owner + origin), not send the Mac test.
        phone = {**headers, **self.headers, 'Origin': 'https://' + test_remote.HOST + ':8443'}
        out = self.post(self.phone, '/api/notifications', {'action': 'save', 'settings': {'phoneAmounts': False}}, phone)
        self.assertFalse(out['settings']['phoneAmounts'])
        self.assertTrue(out['remote'])
        with self.assertRaises(urllib.error.HTTPError) as bad:
            self.post(self.phone, '/api/notifications', {'action': 'save', 'settings': {'phoneAmounts': True}}, phone)
        self.assertEqual(bad.exception.code, 400)
        with self.assertRaises(urllib.error.HTTPError) as bad:
            self.post(self.phone, '/api/notifications', {'action': 'test', 'channel': 'mac'}, phone)
        self.assertEqual(bad.exception.code, 400)
        self.denied(self.phone, '/api/notifications', {**phone, 'Origin': 'https://foreign.example'}, b'{}')

    def test_native_relay_needs_the_app_token(self):
        collector = self.local.RequestHandlerClass.collector
        collector.account = ACCOUNT
        collector.reputation.native_token = 'synthetic-native-token'
        collector.alerts.send(ACCOUNT, 'problems', 'e1', 'T', 'B')
        headers = {
            'Content-Type': 'application/json',
            'X-Bloom-Action': 'notifications',
            'X-Bloom-Native': 'synthetic-native-token',
        }
        path = '/api/notifications/native'
        for denied in (
            {k: v for k, v in headers.items() if k != 'X-Bloom-Native'},
            {**headers, 'X-Bloom-Native': 'wrong'},
            {**headers, 'X-Bloom-Action': 'update'},
        ):
            self.denied(self.local, path, denied, b'{"action":"poll"}')
        self.denied(self.phone, path, {**headers, **self.headers}, b'{"action":"poll"}')
        reply = self.post(self.local, path, {'action': 'poll', 'permission': 'denied'}, headers)
        [item] = reply['items']
        for bad in ({'action': 'ack', 'ids': ['x']}, {'action': 'ack', 'ids': [item['id']] * 21}, {'action': 'poll', 'x': 1}):
            with self.assertRaises(urllib.error.HTTPError) as result:
                self.post(self.local, path, bad, headers)
            self.assertEqual(result.exception.code, 400)
        self.assertEqual(self.post(self.local, path, {'action': 'ack', 'ids': [item['id']]}, headers), {'ok': True})
        self.assertEqual(self.post(self.local, path, {'action': 'poll'}, headers)['items'], [])
        self.assertEqual(collector.alerts.mac_status(ACCOUNT, time.time())['permission'], 'denied')


if __name__ == '__main__':
    unittest.main()
