'use client';
import {
  networkOutage,
  outageEffect,
  outageMessage,
} from '@/lib/network-health';
import { shortModel } from './shared';

/**
 * "It's not you": shown on Overview (Mac and phone) while Darkbloom's network is down
 * (native/network_health.py). Bloomkeeper waits instead of restarting (and, unless only
 * one model is affected, switching), and automatic problem reports pause, until it recovers.
 */
export function NetworkOutageBanner({ health }: { health: unknown }) {
  const outage = networkOutage(health);
  if (!outage) return null;
  return (
    <div className="network-outage" role="status">
      <strong>{outageMessage(outage, undefined, shortModel)}</strong>
      <small>
        {outage.detail} {outageEffect(outage)}
      </small>
    </div>
  );
}
