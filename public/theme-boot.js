// Applies the saved appearance (System, Light or Dark) before the first paint,
// so the page never flashes the other theme. Keep in step with lib/theme.ts.
(function () {
  var preference = 'system';
  try {
    var saved = window.localStorage.getItem('bloom-theme');
    if (saved === 'light' || saved === 'dark' || saved === 'system')
      preference = saved;
  } catch (error) {
    // Storage blocked: follow the system.
  }
  var dark = preference !== 'light';
  if (preference === 'system') {
    try {
      dark = window.matchMedia('(prefers-color-scheme: dark)').matches;
    } catch (error) {
      dark = true;
    }
  }
  var root = document.documentElement;
  root.setAttribute('data-theme', dark ? 'dark' : 'light');
  root.setAttribute('data-theme-preference', preference);
  if (dark) root.classList.add('dark');
  else root.classList.remove('dark');
  var chrome = document.querySelector('meta[name="theme-color"]');
  if (chrome) chrome.setAttribute('content', dark ? '#0a0d12' : '#f3f5f8');
})();
