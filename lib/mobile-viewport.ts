/** Keep the phone app frame in the visible viewport, including keyboard/resume.
 * The footer is a grid row, so it never relies on a fixed bottom offset.
 */
export function bindMobileViewport(frame: HTMLElement) {
  const viewport = window.visualViewport;
  let pending = 0;
  let settleTimer: ReturnType<typeof setTimeout> | undefined;
  const update = () => {
    pending = 0;
    // Let the browser own pinch zoom; do not resize the app under magnification.
    const normalScale = !viewport || Math.abs(viewport.scale - 1) < 0.01;
    const height = viewport?.height ?? window.innerHeight;
    const top = viewport?.offsetTop ?? 0;
    if (
      normalScale &&
      Number.isFinite(height) &&
      height > 0 &&
      Number.isFinite(top)
    ) {
      frame.style.setProperty('--app-viewport-height', `${height}px`);
      frame.style.setProperty('--app-viewport-top', `${Math.max(0, top)}px`);
    } else {
      frame.style.removeProperty('--app-viewport-height');
      frame.style.removeProperty('--app-viewport-top');
    }
  };
  const schedule = () => {
    if (!pending) pending = requestAnimationFrame(update);
  };
  const settle = () => {
    schedule();
    clearTimeout(settleTimer);
    // WebKit can dispatch resize/focus before its final viewport values land.
    settleTimer = setTimeout(schedule, 300);
  };
  document.documentElement.classList.add('bloom-mobile-view');
  update();
  viewport?.addEventListener('resize', settle);
  viewport?.addEventListener('scroll', schedule);
  window.addEventListener('resize', settle);
  window.addEventListener('orientationchange', settle);
  window.addEventListener('pageshow', settle);
  document.addEventListener('visibilitychange', settle);
  document.addEventListener('focusin', settle);
  document.addEventListener('focusout', settle);
  return () => {
    cancelAnimationFrame(pending);
    clearTimeout(settleTimer);
    viewport?.removeEventListener('resize', settle);
    viewport?.removeEventListener('scroll', schedule);
    window.removeEventListener('resize', settle);
    window.removeEventListener('orientationchange', settle);
    window.removeEventListener('pageshow', settle);
    document.removeEventListener('visibilitychange', settle);
    document.removeEventListener('focusin', settle);
    document.removeEventListener('focusout', settle);
    document.documentElement.classList.remove('bloom-mobile-view');
    frame.style.removeProperty('--app-viewport-height');
    frame.style.removeProperty('--app-viewport-top');
  };
}
