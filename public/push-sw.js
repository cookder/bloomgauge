/* Push-only worker: dashboard responses and account data are never cached. */
// Screens a notification may open. Never a URL or path from the payload.
const SCREENS = ['overview', 'test', 'demand'];
const screenFor = (value) => (SCREENS.includes(value) ? value : 'test');
self.addEventListener('install', () => {
  self.skipWaiting();
});
self.addEventListener('activate', (event) => {
  event.waitUntil(self.clients.claim());
});
self.addEventListener('push', (event) => {
  let payload = {};
  try {
    payload = event.data?.json() || {};
  } catch {
    /* Still show a visible notification. */
  }
  const title =
    typeof payload.title === 'string'
      ? payload.title.slice(0, 100)
      : 'BloomGauge · model update';
  const body =
    typeof payload.body === 'string'
      ? payload.body.slice(0, 240)
      : 'Open BloomGauge to see the current model and switch details.';
  const tag =
    typeof payload.tag === 'string' &&
    /^bloom-switch-[a-f0-9]+$/.test(payload.tag)
      ? payload.tag
      : 'bloom-switch';
  const screen = screenFor(payload.screen);
  event.waitUntil(
    self.registration.showNotification(title, {
      body,
      tag,
      icon: '/bloom-icon-192.png',
      badge: '/bloom-icon-192.png',
      data: { screen, path: '/?screen=' + screen },
    }),
  );
});

self.addEventListener('notificationclick', (event) => {
  event.notification.close();
  // Never accept a remote URL or arbitrary path from push content.
  const screen = screenFor(event.notification.data?.screen);
  const target = new URL('/?screen=' + screen, self.location.origin).href;
  event.waitUntil(
    (async () => {
      const windows = await self.clients.matchAll({
        type: 'window',
        includeUncontrolled: true,
      });
      const current = windows.find(
        (client) => new URL(client.url).origin === self.location.origin,
      );
      if (current) {
        await current.navigate(target);
        await current.focus();
      } else {
        await self.clients.openWindow(target);
      }
    })(),
  );
});
