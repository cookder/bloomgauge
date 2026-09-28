'use client';
import { useEffect, useSyncExternalStore } from 'react';
import { Monitor, Moon, Sun } from 'lucide-react';
import {
  THEME_LABELS,
  THEME_PREFERENCES,
  applyTheme,
  getThemePreference,
  nextThemePreference,
  setThemePreference,
  subscribeThemePreference,
  type ThemePreference,
} from '@/lib/theme';

const icons = { system: Monitor, light: Sun, dark: Moon } as const;

export function useThemePreference(): ThemePreference {
  return useSyncExternalStore(
    subscribeThemePreference,
    getThemePreference,
    () => 'system' as const,
  );
}

/** Header button: cycles System -> Light -> Dark. */
export function ThemeToggle() {
  const preference = useThemePreference();
  // Re-apply once after load: starts following system changes and tells the
  // Mac app which appearance the page uses.
  useEffect(() => {
    applyTheme(getThemePreference());
  }, []);
  const Icon = icons[preference];
  const next = nextThemePreference(preference);
  const label = `Appearance: ${THEME_LABELS[preference]}. Switch to ${THEME_LABELS[next]}`;
  return (
    <button
      type="button"
      className="icon-button theme-toggle"
      onClick={() => setThemePreference(next)}
      title={label}
      aria-label={label}
    >
      <Icon size={17} />
    </button>
  );
}

/** The same choice as three buttons, for the More screen. */
export function AppearanceSetting() {
  const preference = useThemePreference();
  return (
    <div className="appearance-setting">
      <div>
        <strong id="appearance-setting-title">Appearance</strong>
        <span>System follows your Mac or phone’s light or dark setting.</span>
      </div>
      <div
        className="appearance-choices"
        role="radiogroup"
        aria-labelledby="appearance-setting-title"
      >
        {THEME_PREFERENCES.map((choice) => {
          const Icon = icons[choice];
          return (
            <button
              key={choice}
              type="button"
              role="radio"
              aria-checked={preference === choice}
              onClick={() => setThemePreference(choice)}
            >
              <Icon size={15} aria-hidden="true" />
              {THEME_LABELS[choice]}
            </button>
          );
        })}
      </div>
    </div>
  );
}
