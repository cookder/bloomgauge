'use client';
import { useEffect, useRef, useState } from 'react';
import { Button } from '@/components/ui/button';
import type { CacheAuthorization } from '@/lib/manual-control';
import {
  cachePermissionResult,
  cachePermissionMessage,
} from '@/lib/cache-permission';

export function CacheRecoveryPermission({
  authorization,
  remote = false,
  locked = false,
  fresh,
  onRefresh,
  onBusy,
}: {
  authorization?: CacheAuthorization;
  remote?: boolean;
  locked?: boolean;
  fresh: boolean;
  onRefresh: () => void;
  onBusy: (value: boolean) => void;
}) {
  const [message, setMessage] = useState(''),
    [busy, setBusy] = useState(false),
    [native, setNative] = useState(false);
  const pending = useRef(''),
    refresh = useRef(onRefresh),
    notify = useRef(onBusy);
  refresh.current = onRefresh;
  notify.current = onBusy;
  useEffect(() => {
    setNative(
      !!(
        window as Window & {
          webkit?: { messageHandlers?: { bloomAccount?: unknown } };
        }
      ).webkit?.messageHandlers?.bloomAccount,
    );
    function receive(event: Event) {
      if (!pending.current) return;
      const result = cachePermissionResult(
        (event as CustomEvent).detail,
        pending.current,
      );
      if (!result) return;
      pending.current = '';
      setBusy(false);
      notify.current(false);
      setMessage(cachePermissionMessage(result.status));
      refresh.current();
    }
    window.addEventListener('bloom-cache-permission-result', receive);
    return () =>
      window.removeEventListener('bloom-cache-permission-result', receive);
  }, []);
  useEffect(() => {
    if (message.startsWith('Setup finished.') && authorization) {
      setMessage(
        authorization.status === 'ready'
          ? 'Password-free cleanup is verified. Start your selected model when you’re ready.'
          : authorization.status === 'required'
            ? 'BloomGauge’s permission is not enabled. Model controls are unchanged.'
            : authorization.detail,
      );
    }
  }, [authorization?.checkedAt]); // Only a new backend check can confirm the result.
  function change(action: 'cache-setup' | 'cache-remove') {
    if (busy || locked || !fresh || remote) return;
    const bridge = (
      window as Window & {
        webkit?: {
          messageHandlers?: {
            bloomAccount?: {
              postMessage: (data: {
                action: string;
                requestId: string;
              }) => void;
            };
          };
        };
      }
    ).webkit?.messageHandlers?.bloomAccount;
    if (!bridge) {
      setMessage(
        'Open the installed BloomGauge app on your Mac to change cache cleanup permission.',
      );
      return;
    }
    const requestId = crypto.randomUUID();
    pending.current = requestId;
    setBusy(true);
    notify.current(true);
    setMessage(
      'Finish or cancel the macOS permission dialog. This does not start a model.',
    );
    try {
      bridge.postMessage({ action, requestId });
    } catch {
      pending.current = '';
      setBusy(false);
      notify.current(false);
      setMessage(
        'Could not open the permission dialog. Reopen BloomGauge and try again.',
      );
    }
  }
  const ready = authorization?.status === 'ready';
  return (
    <details className="cache-recovery-permission" open={!ready}>
      <summary>
        Optional cache cleanup ·{' '}
        {!fresh
          ? 'Checking'
          : ready
            ? 'Enabled'
            : authorization?.status === 'required'
              ? 'Not enabled'
              : 'Status unavailable'}
      </summary>
      <p>
        {authorization?.detail ||
          'Refresh status to check cache cleanup permission on this Mac.'}
      </p>
      <p className="footnote">
        One-time administrator approval allows the cache cleanup command to run
        without another password prompt. Cleanup is used only when eligible;
        BloomGauge rechecks available memory before loading.
      </p>
      <div className="provider-actions">
        {!remote && native && (
          <Button
            variant="outline"
            disabled={locked || busy || !fresh}
            onClick={() => change(ready ? 'cache-remove' : 'cache-setup')}
          >
            {busy
              ? 'Waiting for macOS'
              : ready
                ? 'Remove cache permission'
                : 'Enable cache cleanup'}
          </Button>
        )}
        <Button variant="ghost" disabled={locked || busy} onClick={onRefresh}>
          Check permission
        </Button>
      </div>
      {(remote || !native) && (
        <p className="footnote">
          Open the installed BloomGauge app on your Mac to enable or remove this
          permission.
        </p>
      )}
      {message && <p role="status">{message}</p>}
    </details>
  );
}
