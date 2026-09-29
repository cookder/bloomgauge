'use client';
import {
  createContext,
  memo,
  useContext,
  useEffect,
  useLayoutEffect,
  useRef,
  useState,
  useSyncExternalStore,
  type ReactNode,
  type RefObject,
} from 'react';
import {
  CircleDollarSign,
  SlidersHorizontal,
  Ellipsis,
  Laptop,
} from 'lucide-react';
import { ScreenErrorBoundary } from './screen-error';
import { setSupportContext } from '@/lib/support-issues';
import { bindMobileViewport } from '@/lib/mobile-viewport';
import { WebsiteLinks } from './website-links';
import { AppearanceSetting } from './theme-toggle';

const appSections = [
  {
    id: 'earnings',
    label: 'Earnings',
    icon: CircleDollarSign,
    pages: [
      ['overview', 'Overview'],
      ['charts', 'Charts'],
      ['credits', 'Credits'],
      ['target', 'Target'],
    ],
  },
  {
    id: 'optimizer',
    label: 'Optimizer',
    icon: SlidersHorizontal,
    pages: [
      ['test', 'Overview'],
      ['switch', 'Model controls'],
      ['demand', 'Demand'],
      ['results', 'History'],
      ['optimizer-tools', 'Manage'],
      ['pairs', 'Pair tests'],
      ['diagnostics', 'Diagnostics'],
    ],
  },
  {
    id: 'machines',
    label: 'My Macs',
    icon: Laptop,
    pages: [['machines', 'My Macs']],
  },
  {
    id: 'more',
    label: 'More',
    icon: Ellipsis,
    pages: [
      ['tools', 'More'],
      ['hardware', 'Hardware'],
      ['energy', 'Energy'],
      ['throughput', 'Throughput'],
      ['processes', 'Processes'],
      ['sessions', 'Sessions'],
      ['reputation', 'Reputation'],
      ['access', 'Phone access'],
      ['sources', 'Sources'],
      ['guide', 'Guide'],
      ['support', 'Help & feedback'],
      ['plan', 'About BloomGauge'],
      ['workload', 'Workload'],
      ['traffic', 'Network traffic'],
      ['fleet', 'Network fleet'],
      ['community', 'Community'],
    ],
  },
] as const;
export type Screen = (typeof appSections)[number]['pages'][number][0];
const validScreen = (value: string | null): value is Screen =>
  appSections.some((section) => section.pages.some(([id]) => id === value));
const sectionFor = (screen: Screen) =>
  appSections.find((s) => s.pages.some(([id]) => id === screen))!;
const parentFor = (screen: Screen) =>
  [
    'test',
    'results',
    'optimizer-tools',
    'switch',
    'pairs',
    'diagnostics',
  ].includes(screen)
    ? 'optimizer'
    : ['demand', 'traffic', 'fleet'].includes(screen)
      ? 'network'
      : screen === 'community'
        ? 'community'
        : 'mac';
const primaryPages = (screen: Screen) => {
  const section = sectionFor(screen);
  return section.id === 'earnings' || section.id === 'machines'
    ? section.pages
    : section.id === 'optimizer'
      ? section.pages.filter(([id]) =>
          ['test', 'demand', 'results'].includes(id),
        )
      : [];
};
const subscribe = (change: () => void) => {
  const media = window.matchMedia('(max-width: 680px)');
  media.addEventListener('change', change);
  return () => media.removeEventListener('change', change);
};
const mobileSnapshot = () => window.matchMedia('(max-width: 680px)').matches;
type Navigation = {
  mobile: boolean;
  screen: Screen;
  tab: string;
  navigate: (screen: Screen) => void;
  selectTab: (tab: string) => void;
  visible: (screen: Screen) => boolean;
  selectSection: (section: (typeof appSections)[number]) => void;
  scrollRoot: RefObject<HTMLElement | null>;
};
const Context = createContext<Navigation | null>(null);
export const useAppNavigation = () => useContext(Context)!;
const ScreenActivity = createContext(true);
export const useScreenActive = () => useContext(ScreenActivity);

export function AppNavigationProvider({ children }: { children: ReactNode }) {
  const mobile = useSyncExternalStore(subscribe, mobileSnapshot, () => false);
  const [screen, setScreen] = useState<Screen>(() => {
    try {
      const requested = new URLSearchParams(window.location.search).get(
        'screen',
      );
      if (validScreen(requested)) return requested;
      const saved = sessionStorage.getItem('bloom-screen');
      if (validScreen(saved)) return saved;
    } catch {
      /* Private browsing may disable preference storage. */
    }
    return 'overview';
  });
  const positions = useRef<Record<string, number>>({});
  const lastPages = useRef<Record<string, Screen>>({});
  const pendingScroll = useRef<number | null>(null);
  const scrollRoot = useRef<HTMLElement>(null);
  const readScroll = () =>
    mobile ? (scrollRoot.current?.scrollTop ?? 0) : window.scrollY;
  const moveScroll = (top: number) =>
    (mobile ? scrollRoot.current : window)?.scrollTo({
      top,
      behavior: 'instant',
    });
  const tab = parentFor(screen);
  useEffect(() => setSupportContext(screen), [screen]);
  function navigate(next: Screen) {
    positions.current[screen] = readScroll();
    lastPages.current[sectionFor(screen).id] = screen;
    if (next === screen) {
      pendingScroll.current = null;
      moveScroll(0);
    } else {
      pendingScroll.current = positions.current[next] ?? 0;
      setScreen(next);
    }
  }
  function selectTab(next: string) {
    const destination = (
      {
        mac: 'overview',
        optimizer: 'test',
        network: 'demand',
        community: 'community',
        workload: 'workload',
        more: 'tools',
      } as Record<string, Screen>
    )[next];
    if (destination) navigate(destination);
  }
  useLayoutEffect(() => {
    if (pendingScroll.current == null) return;
    moveScroll(pendingScroll.current);
    pendingScroll.current = null;
  }, [screen, tab, mobile]);
  useEffect(() => {
    try {
      sessionStorage.setItem('bloom-screen', screen);
    } catch {
      /* Optional preference. */
    }
  }, [screen]);
  return (
    <Context.Provider
      value={{
        mobile,
        screen,
        tab,
        navigate,
        selectTab,
        scrollRoot,
        visible: (id) => screen === id,
        selectSection: (section) =>
          navigate(
            sectionFor(screen).id === section.id
              ? screen
              : (lastPages.current[section.id] ?? section.pages[0][0]),
          ),
      }}
    >
      {children}
    </Context.Provider>
  );
}

export function AppScreen({
  name,
  children,
  continuation = false,
}: {
  name: Screen;
  children: ReactNode;
  continuation?: boolean;
}) {
  const { mobile, visible } = useAppNavigation();
  const active = visible(name);
  return (
    <div
      className="app-screen"
      id={continuation ? undefined : `screen-${name}`}
      hidden={!visible(name)}
      role={mobile && !continuation ? 'region' : undefined}
      aria-label={
        mobile && !continuation
          ? sectionFor(name).pages.find(([id]) => id === name)?.[1]
          : undefined
      }
    >
      <ScreenActivity.Provider value={active}>
        <ScreenErrorBoundary name={name}>
          <RetainedContent active={active}>{children}</RetainedContent>
        </ScreenErrorBoundary>
      </ScreenActivity.Provider>
    </div>
  );
}
// Keep local filters/loaded results, without redrawing offscreen charts for
// every one-second snapshot. The first hide still delivers paused props.
const RetainedContent = memo(
  function RetainedContent({
    children,
  }: {
    active: boolean;
    children: ReactNode;
  }) {
    return children;
  },
  (previous, next) => !previous.active && !next.active,
);

export function AppFrame({ children }: { children: ReactNode }) {
  const { mobile } = useAppNavigation();
  const frame = useRef<HTMLDivElement>(null);
  useLayoutEffect(() => {
    if (mobile && frame.current) return bindMobileViewport(frame.current);
  }, [mobile]);
  return (
    <div
      ref={frame}
      className={mobile ? 'app-mobile-frame' : 'app-desktop-frame'}
    >
      {children}
    </div>
  );
}

export function MobileNavigation() {
  const { mobile, screen, navigate } = useAppNavigation();
  const subnav = useRef<HTMLElement>(null);
  useLayoutEffect(() => {
    const nav = subnav.current;
    const header = nav
      ?.closest('.dashboard')
      ?.querySelector<HTMLElement>('.topbar');
    if (!mobile || !nav || !header) return;
    const style = document.documentElement.style;
    const measure = () => {
      const height = header.getBoundingClientRect().height;
      style.setProperty('--app-header-height', `${height}px`);
      style.setProperty(
        '--app-chrome-height',
        `${height + nav.getBoundingClientRect().height + 14}px`,
      );
    };
    measure();
    const observer = new ResizeObserver(measure);
    observer.observe(header);
    observer.observe(nav);
    return () => {
      observer.disconnect();
      style.removeProperty('--app-header-height');
      style.removeProperty('--app-chrome-height');
    };
  }, [mobile]);
  useLayoutEffect(() => {
    const nav = subnav.current;
    const selected = nav?.querySelector<HTMLElement>('[aria-current="page"]');
    if (!mobile || !nav || !selected) return;
    // Move only the horizontal menu. scrollIntoView would also move the page,
    // undoing the saved position when returning to a screen.
    const left = selected.offsetLeft;
    const right = left + selected.offsetWidth;
    if (left < nav.scrollLeft) nav.scrollLeft = left;
    else if (right > nav.scrollLeft + nav.clientWidth)
      nav.scrollLeft = right - nav.clientWidth;
  }, [mobile, screen]);
  const current = sectionFor(screen);
  const pages = primaryPages(screen);
  return (
    <nav
      ref={subnav}
      className={`app-subnav ${mobile ? '' : 'desktop-subnav'} ${current.id === 'machines' ? 'macs-subnav' : ''}`}
      aria-label={`${current.label} sections`}
    >
      {(!pages.length || !pages.some(([id]) => id === screen)) &&
        !['tools', 'optimizer-tools'].includes(screen) && (
          <button
            type="button"
            onClick={() =>
              navigate(current.id === 'optimizer' ? 'optimizer-tools' : 'tools')
            }
          >
            ← {current.id === 'optimizer' ? 'Manage optimizer' : 'More'}
          </button>
        )}
      {pages.map(([id, label]) => (
        <button
          type="button"
          key={id}
          aria-current={
            screen === id ||
            (id === 'optimizer-tools' &&
              ['pairs', 'diagnostics'].includes(screen))
              ? 'page'
              : undefined
          }
          aria-controls={`screen-${id}`}
          onClick={() => navigate(id)}
        >
          {label}
        </button>
      ))}
    </nav>
  );
}

export function MobileBottomNavigation() {
  const { mobile, screen, selectSection } = useAppNavigation();
  if (!mobile) return null;
  const current = sectionFor(screen);
  return (
    <nav className="app-bottom-nav" aria-label="Main navigation">
      {appSections.map((section) => {
        const Icon = section.icon;
        return (
          <button
            type="button"
            key={section.id}
            aria-current={section.id === current.id ? 'page' : undefined}
            onClick={() => selectSection(section)}
          >
            <Icon size={21} aria-hidden="true" />
            <span>{section.label}</span>
          </button>
        );
      })}
    </nav>
  );
}

export function MobilePageTitle() {
  const { mobile, screen } = useAppNavigation();
  return mobile ? (
    <span className="app-page-title">{sectionFor(screen).label}</span>
  ) : null;
}

export function DesktopNavigation() {
  const { mobile, screen, selectSection } = useAppNavigation();
  if (mobile) return null;
  return (
    <nav className="desktop-primary-nav" aria-label="Main navigation">
      {appSections.map((section) => {
        const Icon = section.icon;
        return (
          <button
            key={section.id}
            type="button"
            aria-current={
              sectionFor(screen).id === section.id ? 'page' : undefined
            }
            onClick={() => selectSection(section)}
          >
            <Icon size={19} />
            {section.label}
          </button>
        );
      })}
    </nav>
  );
}

export function NavigationHub({ optimizer = false }: { optimizer?: boolean }) {
  const { navigate } = useAppNavigation();
  const groups: { title: string; items: [Screen, string, string][] }[] =
    optimizer
      ? [
          {
            title: 'Model controls',
            items: [
              [
                'switch',
                'Switch models',
                'Choose a model manually. This pauses automatic switching.',
              ],
              [
                'pairs',
                'Pair tests',
                'Compare compatible pairs of warm models.',
              ],
              [
                'diagnostics',
                'Decision log & diagnostics',
                'Review changes, cache recovery and model readiness.',
              ],
            ],
          },
          {
            title: 'Deeper network analysis',
            items: [
              [
                'traffic',
                'Traffic patterns',
                'Historical network throughput and day-of-week patterns.',
              ],
              [
                'fleet',
                'Network fleet',
                'Provider capacity, hardware and reported geography.',
              ],
            ],
          },
        ]
      : [
          {
            title: 'Your Mac',
            items: [
              ['hardware', 'Hardware', 'Live load, memory and temperatures.'],
              ['energy', 'Energy', 'Power usage and electricity costs.'],
              [
                'throughput',
                'Throughput',
                'Output, concurrency and provider performance.',
              ],
              ['processes', 'Processes', 'CPU, GPU and memory by process.'],
            ],
          },
          {
            title: 'Reporting',
            items: [
              ['sessions', 'Sessions', 'Session boundaries and warm runtime.'],
              ['reputation', 'Reputation', 'Your provider’s network standing.'],
              ['workload', 'Workload', 'Requests, tokens and paid work.'],
              [
                'traffic',
                'Network traffic',
                'Trends and weekly traffic patterns.',
              ],
              ['fleet', 'Network fleet', 'Capacity and geography.'],
              ['community', 'Community', 'Saved community insights.'],
            ],
          },
          {
            title: 'Connections & support',
            items: [
              [
                'plan',
                'About BloomGauge',
                'What’s included, plus optional sharing and contact.',
              ],
              [
                'access',
                'Phone access',
                'Private remote access and connection setup.',
              ],
              ['sources', 'Data sources', 'Freshness and connection health.'],
              [
                'guide',
                'Guide',
                'Step-by-step walkthroughs and fixes for common setup issues.',
              ],
              [
                'support',
                'Help & feedback',
                'Contact Andrew on Slack or email, or review optional diagnostics.',
              ],
            ],
          },
        ];
  return (
    <section className="navigation-hub">
      <div className="panel-heading">
        <div>
          <div className="eyebrow">
            {optimizer ? 'OPTIMIZER' : 'TOOLS & REPORTS'}
          </div>
          <h2>{optimizer ? 'Manage & investigate.' : 'A little deeper.'}</h2>
        </div>
      </div>
      {!optimizer && <AppearanceSetting />}
      {!optimizer && <WebsiteLinks />}
      {groups.map((group) => (
        <div key={group.title}>
          <h3>{group.title}</h3>
          <div className="navigation-cards">
            {group.items.map(([id, title, detail]) => (
              <button key={id} type="button" onClick={() => navigate(id)}>
                <strong>
                  {title}
                  <span aria-hidden="true">→</span>
                </strong>
                <span>{detail}</span>
              </button>
            ))}
          </div>
        </div>
      ))}
    </section>
  );
}

export function ScreenActivityBoundary({
  active,
  children,
}: {
  active: boolean;
  children: ReactNode;
}) {
  const parent = useScreenActive();
  return (
    <ScreenActivity.Provider value={parent && active}>
      {children}
    </ScreenActivity.Provider>
  );
}
