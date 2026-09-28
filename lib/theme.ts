// Appearance: System (follows macOS / the phone), Light or Dark.
// public/theme-boot.js applies the saved choice before the first paint; keep
// its storage key, values and fallbacks in step with this file.

export type ThemePreference = 'system' | 'light' | 'dark';
export type ResolvedTheme = 'light' | 'dark';

export const THEME_STORAGE_KEY = 'bloom-theme';
export const THEME_PREFERENCES: readonly ThemePreference[] = [
  'system',
  'light',
  'dark',
];
export const THEME_LABELS: Record<ThemePreference, string> = {
  system: 'System',
  light: 'Light',
  dark: 'Dark',
};
// Browser chrome colour (meta theme-color) for each theme's page background.
export const THEME_CHROME: Record<ResolvedTheme, string> = {
  dark: '#0a0d12',
  light: '#f3f5f8',
};

type ReadableStorage = Pick<Storage, 'getItem'>;
type WritableStorage = Pick<Storage, 'setItem'>;

export function parseThemePreference(value: unknown): ThemePreference {
  return value === 'light' || value === 'dark' || value === 'system'
    ? value
    : 'system';
}

/** System follows the OS; with no answer from the OS the app stays dark, as before. */
export function resolveTheme(
  preference: ThemePreference,
  systemPrefersDark: boolean | null,
): ResolvedTheme {
  if (preference === 'light' || preference === 'dark') return preference;
  return systemPrefersDark === false ? 'light' : 'dark';
}

/** Saved choice, or System when storage is missing, blocked or holds junk. */
export function readThemePreference(
  storage: ReadableStorage | null | undefined,
): ThemePreference {
  try {
    return parseThemePreference(storage?.getItem(THEME_STORAGE_KEY));
  } catch {
    return 'system';
  }
}

/** Returns false when the choice could not be saved (it still applies now). */
export function writeThemePreference(
  storage: WritableStorage | null | undefined,
  preference: ThemePreference,
): boolean {
  try {
    if (!storage) return false;
    storage.setItem(THEME_STORAGE_KEY, preference);
    return true;
  } catch {
    return false;
  }
}

/** The header button cycles System -> Light -> Dark -> System. */
export function nextThemePreference(
  preference: ThemePreference,
): ThemePreference {
  const index = THEME_PREFERENCES.indexOf(preference);
  return THEME_PREFERENCES[(index + 1) % THEME_PREFERENCES.length];
}

// -- browser side --
const DARK_QUERY = '(prefers-color-scheme: dark)';
const listeners = new Set<() => void>();
let current: ThemePreference | null = null;

function safeStorage(): Storage | null {
  try {
    return window.localStorage;
  } catch {
    return null;
  }
}
function systemPrefersDark(): boolean | null {
  try {
    return window.matchMedia(DARK_QUERY).matches;
  } catch {
    return null;
  }
}

export function getThemePreference(): ThemePreference {
  if (current === null) {
    const applied =
      typeof document === 'undefined'
        ? null
        : document.documentElement.getAttribute('data-theme-preference');
    current = applied
      ? parseThemePreference(applied)
      : readThemePreference(safeStorage());
  }
  return current;
}

export function applyTheme(preference: ThemePreference): ResolvedTheme {
  const resolved = resolveTheme(preference, systemPrefersDark());
  const root = document.documentElement;
  root.setAttribute('data-theme', resolved);
  root.setAttribute('data-theme-preference', preference);
  root.classList.toggle('dark', resolved === 'dark');
  document
    .querySelector('meta[name="theme-color"]')
    ?.setAttribute('content', THEME_CHROME[resolved]);
  try {
    // Lets the Mac app match its window and title bar to the choice.
    (
      window as unknown as {
        webkit?: {
          messageHandlers?: {
            bloomAppearance?: { postMessage(value: string): void };
          };
        };
      }
    ).webkit?.messageHandlers?.bloomAppearance?.postMessage(preference);
  } catch {
    // Not in the Mac app.
  }
  return resolved;
}

export function setThemePreference(preference: ThemePreference) {
  current = preference;
  writeThemePreference(safeStorage(), preference);
  applyTheme(preference);
  listeners.forEach((listener) => listener());
}

let watching = false;
function watch() {
  if (watching || typeof window === 'undefined') return;
  watching = true;
  try {
    window
      .matchMedia(DARK_QUERY)
      .addEventListener('change', () => applyTheme(getThemePreference()));
  } catch {
    // No media queries: the app stays on the saved or default theme.
  }
  window.addEventListener('storage', (event) => {
    if (event.key !== THEME_STORAGE_KEY) return;
    current = parseThemePreference(event.newValue);
    applyTheme(current);
    listeners.forEach((listener) => listener());
  });
}

export function subscribeThemePreference(listener: () => void) {
  watch();
  listeners.add(listener);
  return () => {
    listeners.delete(listener);
  };
}
