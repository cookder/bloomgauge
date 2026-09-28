'use client';
import { useEffect, useRef, useState } from 'react';
import { MessageSquarePlus, Sparkles, X } from 'lucide-react';
import { Button } from '@/components/ui/button';
import { useAppNavigation } from './app-navigation';
import {
  earlierUseKeys,
  feedbackAction,
  feedbackText,
  feedbackTitle,
  managerChanges,
  markLocalSeen,
  readLocalSeen,
  shouldShowWhatsChanged,
  slackUrl,
  smallerChanges,
  validWhatsChangedStatus,
  WHATS_CHANGED_EVENT,
  whatsChangedId,
  whatsChangedLocalKey,
  whatsChangedTitle,
  type WhatsChangedStatus,
} from '@/lib/whats-changed';

/** Ask the "What's changed" card to open (the link next to "What it does"). */
export function showWhatsChanged() {
  window.dispatchEvent(new Event(WHATS_CHANGED_EVENT));
}

/** One-time card at the top of Optimizer → Overview after an update. */
export function WhatsChanged() {
  const { navigate } = useAppNavigation();
  const [open, setOpen] = useState(false);
  const card = useRef<HTMLElement>(null);
  useEffect(() => {
    let cancelled = false;
    void (async () => {
      let server: WhatsChangedStatus | null = null;
      try {
        const response = await fetch('/api/whats-changed', {
          cache: 'no-store',
          signal: AbortSignal.timeout(8000),
        });
        const value: unknown = await response.json();
        if (response.ok && validWhatsChangedStatus(value)) server = value;
      } catch {
        /* Fall back to this browser's memory. */
      }
      if (cancelled) return;
      let localSeen: string[] = [];
      let earlierUseHere = false;
      try {
        localSeen = readLocalSeen(localStorage.getItem(whatsChangedLocalKey));
        earlierUseHere = earlierUseKeys.some(
          (key) => localStorage.getItem(key) !== null,
        );
      } catch {
        /* Optional storage. */
      }
      if (shouldShowWhatsChanged({ server, localSeen, earlierUseHere }))
        setOpen(true);
    })();
    return () => {
      cancelled = true;
    };
  }, []);
  useEffect(() => {
    const show = () => {
      setOpen(true);
      requestAnimationFrame(() =>
        card.current?.scrollIntoView({ block: 'start', behavior: 'smooth' }),
      );
    };
    window.addEventListener(WHATS_CHANGED_EVENT, show);
    return () => window.removeEventListener(WHATS_CHANGED_EVENT, show);
  }, []);
  function dismiss() {
    setOpen(false);
    try {
      localStorage.setItem(
        whatsChangedLocalKey,
        markLocalSeen(localStorage.getItem(whatsChangedLocalKey), whatsChangedId),
      );
    } catch {
      /* The Mac still remembers it below. */
    }
    void fetch('/api/whats-changed', {
      method: 'POST',
      cache: 'no-store',
      headers: {
        'Content-Type': 'application/json',
        'X-Bloom-Action': 'whats-changed',
      },
      body: JSON.stringify({ action: 'seen', id: whatsChangedId }),
    }).catch(() => {
      /* Closed in this browser; it may show once more on another device. */
    });
  }
  if (!open) return null;
  return (
    <section
      ref={card}
      className="panel whats-changed"
      aria-labelledby="whats-changed-title"
    >
      <div className="whats-changed-heading">
        <Sparkles size={18} aria-hidden />
        <h2 id="whats-changed-title">{whatsChangedTitle}</h2>
        <button
          type="button"
          className="icon-button"
          aria-label="Close what’s changed"
          onClick={dismiss}
        >
          <X size={16} />
        </button>
      </div>
      <h3>The optimizer is now a manager</h3>
      <ul>
        {managerChanges.map((text) => (
          <li key={text}>{text}</li>
        ))}
      </ul>
      <h3>Smaller improvements</h3>
      <ul>
        {smallerChanges.map((text) => (
          <li key={text}>{text}</li>
        ))}
      </ul>
      <div className="whats-changed-feedback">
        <MessageSquarePlus size={18} aria-hidden />
        <div>
          <h3>{feedbackTitle}</h3>
          <p>{feedbackText}</p>
          <div className="whats-changed-actions">
            <button
              type="button"
              className="action"
              onClick={() => navigate('support')}
            >
              {feedbackAction}
            </button>
            <a
              className="text-link"
              href={slackUrl}
              target="_blank"
              rel="noopener noreferrer"
            >
              Bloomkeeper Slack ↗
            </a>
          </div>
        </div>
      </div>
      <div className="whats-changed-actions">
        <Button variant="ghost" onClick={dismiss}>
          Got it
        </Button>
      </div>
      <p className="footnote">
        You can open this again from “What’s changed” on this page.
      </p>
    </section>
  );
}
