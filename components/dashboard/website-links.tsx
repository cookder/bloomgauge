import { bloomWebsiteLinks } from '@/lib/website-links';

export function WebsiteLinks() {
  return (
    <nav className="website-links" aria-label="Bloomkeeper website links">
      <h3>Bloomkeeper online</h3>
      <div className="support-actions">
        {bloomWebsiteLinks.map((link) => (
          <a
            key={link.href}
            className="action"
            href={link.href}
            target="_blank"
            rel="noopener noreferrer"
            referrerPolicy="no-referrer"
          >
            {link.label} <span aria-hidden="true">↗</span>
          </a>
        ))}
      </div>
      <p className="footnote">
        Opens in your browser. Your dashboard stays open.
      </p>
    </nav>
  );
}
