import assert from 'node:assert/strict';
import test from 'node:test';
import { registerHooks } from 'node:module';

// Node's test runner needs the bundler's extensionless TS resolution.
registerHooks({
  resolve(specifier, context, next) {
    return next(
      specifier === './optimizer-manager' ? `${specifier}.ts` : specifier,
      context,
    );
  },
});
const {
  KINDS,
  LIMITS,
  applyChanges,
  channelsText,
  checkAmount,
  checkClock,
  checkMinutes,
  earningsHint,
  kindDetail,
  macStatus,
  mergeSettings,
  minutesText,
  readNotifications,
  settingsSummary,
} = await import('./notification-settings.ts');

const settings = () => ({
  kinds: {
    earningsHigh: { mac: true, phone: true },
    switchConsidered: { mac: true, phone: true },
    modelSwitched: { mac: true, phone: true },
    problems: { mac: true, phone: true },
    demandSpike: { mac: false, phone: false },
    networkNews: { mac: false, phone: false },
  },
  earnings: { usdPerHour: 0.3, minutes: 30 },
  quietHours: { enabled: false, start: '22:00', end: '07:00' },
  phoneAmounts: true,
});
const mac = {
  available: true,
  permission: 'granted',
  lastPolledAt: 1_790_600_000,
  lastDeliveredAt: null,
  pending: 0,
};
const response = (extra = {}) => ({
  settings: settings(),
  defaults: settings(),
  limits: LIMITS,
  mac,
  phone: { supported: true, subscriptionCount: 0 },
  earningsNow: { usdPerHour: 0.21, minutes: 30, coverage: 1 },
  recent: [
    {
      kind: 'earningsHigh',
      at: 1_790_600_000,
      title: 'Earnings running high',
      channels: ['mac', 'phone'],
    },
  ],
  ...extra,
});

test('reads the contract response and keeps only what the card uses', () => {
  const view = readNotifications(response());
  assert.deepEqual(view.settings, settings());
  assert.deepEqual(view.limits, LIMITS);
  assert.equal(view.mac.permission, 'granted');
  assert.equal(view.earningsNow.usdPerHour, 0.21);
  assert.equal(view.recent.length, 1);
  assert.equal(view.phones, 0);
  assert.equal('phone' in view, false);
  assert.equal(readNotifications(response({ phone: null })).phones, null);
});

test('missing settings or Mac status is unreadable; extras fall back', () => {
  assert.equal(readNotifications(null), null);
  assert.equal(readNotifications(response({ settings: undefined })), null);
  assert.equal(readNotifications(response({ mac: null })), null);
  const partial = settings();
  delete partial.kinds.networkNews;
  assert.equal(readNotifications(response({ settings: partial })), null);
  const view = readNotifications(
    response({
      limits: { usdPerHour: { min: 1, max: 0, step: 1 } },
      mac: { available: false, permission: 'maybe' },
      earningsNow: { usdPerHour: 'x', minutes: 30, coverage: 1 },
      recent: [
        { kind: 'x', at: 'soon', title: 'x', channels: [] },
        5,
        {
          kind: 'problems',
          at: 1,
          title: 'No work',
          channels: ['email', 'phone'],
        },
      ],
    }),
  );
  assert.deepEqual(view.limits, LIMITS);
  assert.equal(view.mac.permission, 'unknown');
  assert.equal(view.mac.lastDeliveredAt, null);
  assert.equal(view.earningsNow, null);
  assert.deepEqual(view.recent, [
    { kind: 'problems', at: 1, title: 'No work', channels: ['phone'] },
  ]);
  assert.equal(
    readNotifications(response({ earningsNow: null })).earningsNow,
    null,
  );
});

test('a partial save changes only what it names', () => {
  const before = settings();
  const after = mergeSettings(before, {
    kinds: { demandSpike: { phone: true } },
    earnings: { minutes: 45 },
  });
  assert.deepEqual(after.kinds.demandSpike, { mac: false, phone: true });
  assert.deepEqual(after.kinds.earningsHigh, before.kinds.earningsHigh);
  assert.deepEqual(after.earnings, { usdPerHour: 0.3, minutes: 45 });
  assert.deepEqual(after.quietHours, before.quietHours);
  assert.deepEqual(before, settings(), 'the input is not changed');
  const quiet = mergeSettings(before, {
    quietHours: { enabled: true, start: undefined },
    phoneAmounts: true,
  });
  assert.deepEqual(quiet.quietHours, {
    enabled: true,
    start: '22:00',
    end: '07:00',
  });
  assert.equal(quiet.phoneAmounts, true);
});

test('saves still on their way apply in order', () => {
  const out = applyChanges(settings(), [
    { kinds: { problems: { phone: false } } },
    { kinds: { problems: { phone: true, mac: false } } },
    { earnings: { usdPerHour: 0.5 } },
  ]);
  assert.deepEqual(out.kinds.problems, { mac: false, phone: true });
  assert.equal(out.earnings.usdPerHour, 0.5);
  assert.deepEqual(applyChanges(settings(), []), settings());
});

test('hourly amounts: $0.01 to $10, whole cents', () => {
  assert.deepEqual(checkAmount('0.30', LIMITS.usdPerHour), {
    ok: true,
    value: 0.3,
  });
  assert.deepEqual(checkAmount('10', LIMITS.usdPerHour), {
    ok: true,
    value: 10,
  });
  assert.equal(checkAmount('0.01', LIMITS.usdPerHour).ok, true);
  for (const bad of ['', ' ', '0', '10.01', '-1', 'abc', 'Infinity'])
    assert.equal(
      checkAmount(bad, LIMITS.usdPerHour).error,
      'Choose an amount from $0.01 to $10.00 an hour.',
      bad,
    );
  assert.equal(
    checkAmount('0.305', LIMITS.usdPerHour).error,
    'Use at most 2 decimals, like 0.30.',
  );
});

test('minutes: whole, in range, in steps of 5', () => {
  assert.deepEqual(checkMinutes('30', LIMITS.minutes), { ok: true, value: 30 });
  assert.equal(checkMinutes('10', LIMITS.minutes).ok, true);
  assert.equal(checkMinutes('240', LIMITS.minutes).ok, true);
  for (const bad of ['', '5', '245', '32', '30.5', 'x'])
    assert.equal(
      checkMinutes(bad, LIMITS.minutes).error,
      'Choose 10 to 240 minutes, in steps of 5.',
      bad,
    );
});

test('quiet hours use 24-hour HH:MM', () => {
  assert.deepEqual(checkClock('22:00'), { ok: true, value: '22:00' });
  assert.deepEqual(checkClock('07:05:00'), { ok: true, value: '07:05' });
  assert.equal(checkClock('00:00').ok, true);
  assert.equal(checkClock('23:59').ok, true);
  for (const bad of ['', '24:00', '7:00', '12:60', '22:00:30', 'noon'])
    assert.equal(checkClock(bad).ok, false, bad);
});

test('plain-language lines', () => {
  assert.equal(minutesText(30), '30 minutes');
  assert.equal(minutesText(60), '1 hour');
  assert.equal(minutesText(90), '90 minutes');
  assert.equal(minutesText(240), '4 hours');
  assert.equal(
    kindDetail('earningsHigh', settings()),
    'Your pace stays at or above $0.30/h for 30 minutes.',
  );
  for (const kind of KINDS) assert.ok(kindDetail(kind, settings()).length < 80);
  assert.equal(
    earningsHint({ usdPerHour: 0.21, minutes: 30, coverage: 1 }),
    'Last 30 min: $0.21/h',
  );
  assert.equal(
    earningsHint({ usdPerHour: 0.061, minutes: 30, coverage: 0.4 }),
    'Last 30 min: $0.061/h, from 12 min of data',
  );
  assert.equal(
    earningsHint({ usdPerHour: null, minutes: 30, coverage: 0 }),
    null,
  );
  assert.equal(earningsHint(null), null);
  assert.equal(channelsText(['mac', 'phone']), 'Mac and phone');
  assert.equal(channelsText(['phone']), 'Phone');
  assert.equal(channelsText([]), '');
});

test('the summary counts alerts, channels and quiet hours', () => {
  assert.equal(settingsSummary(settings()), '4 of 6 on · Mac and phone');
  const s = settings();
  for (const kind of KINDS) s.kinds[kind] = { mac: false, phone: false };
  assert.equal(settingsSummary(s), 'All off');
  s.kinds.problems.phone = true;
  s.quietHours.enabled = true;
  assert.equal(
    settingsSummary(s),
    '1 of 6 on · Phone only · quiet 22:00–07:00',
  );
  s.quietHours.end = '22:00';
  assert.equal(settingsSummary(s), '1 of 6 on · Phone only');
});

test('Mac status: closed app, denied permission, first use, ready', () => {
  assert.deepEqual(macStatus({ ...mac, available: false }, false), {
    text: 'Mac notifications start when BloomGauge is open on this Mac.',
    problem: false,
  });
  assert.match(
    macStatus({ ...mac, available: false }, true).text,
    /open on your Mac\.$/,
  );
  assert.deepEqual(macStatus({ ...mac, permission: 'denied' }, false), {
    text: 'Mac notifications are off in System Settings → Notifications → BloomGauge.',
    problem: true,
  });
  assert.match(
    macStatus({ ...mac, permission: 'notDetermined' }, false).text,
    /asks you to allow/,
  );
  assert.equal(macStatus(mac, false).problem, false);
});
