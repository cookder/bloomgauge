'use client';
import { useSyncExternalStore } from 'react';

export const subscribePageVisibility = (changed: () => void) => {
  document.addEventListener('visibilitychange', changed);
  window.addEventListener('pageshow', changed);
  // Native web views and restored mobile pages can miss a visibility event.
  // Focus only rechecks document.hidden; it never forces background polling.
  window.addEventListener('focus', changed);
  return () => {
    document.removeEventListener('visibilitychange', changed);
    window.removeEventListener('pageshow', changed);
    window.removeEventListener('focus', changed);
  };
};

export function usePageVisible() {
  return useSyncExternalStore(
    subscribePageVisibility,
    () => !document.hidden,
    () => true,
  );
}
