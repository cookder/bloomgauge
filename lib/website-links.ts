export const bloomWebsite = {
  home: 'https://bloomformac.com/',
  changelog: 'https://bloomformac.com/changelog',
  setup: 'https://bloomformac.com/beta/quickstart',
  support: 'https://bloomformac.com/support',
} as const;

export const bloomWebsiteLinks = [
  { label: 'BloomGauge website', href: bloomWebsite.home },
  { label: 'Changelog', href: bloomWebsite.changelog },
  { label: 'Setup guide', href: bloomWebsite.setup },
  { label: 'Support website', href: bloomWebsite.support },
] as const;
