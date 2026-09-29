'use client';
import { WebsiteLinks } from './website-links';
import { useEffect, useRef, useState } from 'react';
import { Download, FileSearch, ShieldCheck, Share2 } from 'lucide-react';
import { UpdateSettings } from './update-settings';
import { openSupportReport } from '@/lib/support-issues';
import { useAppNavigation } from './app-navigation';
import {
  readAutoSend,
  writeAutoSend,
  SupportRequestError,
} from '@/lib/support-report';

/** Opt-in automatic problem reports; the same allowlisted report as a one-tap send. */
function AutoSendSetting() {
  const [value, setValue] = useState<boolean | null>(null),
    [error, setError] = useState('');
  useEffect(() => {
    const control = new AbortController();
    readAutoSend(control.signal)
      .then(setValue)
      .catch(() => setValue(false));
    return () => control.abort();
  }, []);
  return (
    <label className="support-auto-setting">
      <input
        type="checkbox"
        checked={!!value}
        disabled={value === null}
        onChange={(event) => {
          const next = event.target.checked;
          setError('');
          writeAutoSend(next, AbortSignal.timeout(10000))
            .then(setValue)
            .catch((e) =>
              setError(
                e instanceof SupportRequestError && e.status === 'mac_only'
                  ? 'Turn this on in BloomGauge on your Mac.'
                  : 'Could not save. Try again.',
              ),
            );
        }}
      />
      <span>
        <strong>Send problem reports automatically</strong>
        <small>
          When BloomGauge hits a problem, it sends the same short report you
          would send with one tap: app version, Mac chip and memory size, and
          status codes. Never earnings, account details, model names or logs.{' '}
          {error}
        </small>
      </span>
    </label>
  );
}

type Report = {
  schema: 'bloom-diagnostics-v1';
  generatedAt: string;
  app: { version: string; surface: string };
  privacy: { earningsIncluded: boolean; excluded: string[] };
  hardware: { chip: string; memoryTotalGB: number | null };
  optimizer: {
    mode: string;
    recentDecisions: unknown[];
    recentEvents: unknown[];
  };
  issues: { code: string; description: string }[];
};
type NativeWindow = Window & {
  webkit?: {
    messageHandlers?: {
      bloomDiagnostics?: { postMessage: (value: unknown) => void };
    };
  };
};

export function SupportDiagnostics() {
  const navigation = useAppNavigation();
  const [includeEarnings, setIncludeEarnings] = useState(false);
  const [report, setReport] = useState<Report | null>(null);
  const [busy, setBusy] = useState(false),
    [saving, setSaving] = useState(false);
  const [error, setError] = useState(''),
    [message, setMessage] = useState('');
  const request = useRef<AbortController | null>(null),
    saveId = useRef<string | null>(null);
  const mounted = useRef(true);
  useEffect(() => {
    mounted.current = true;
    function saved(event: Event) {
      const detail = (event as CustomEvent).detail;
      if (!detail || detail.requestId !== saveId.current) return;
      saveId.current = null;
      setSaving(false);
      if (detail.status === 'saved')
        setMessage('Report saved. Nothing was uploaded.');
      else if (detail.status === 'cancelled')
        setMessage('Save cancelled. Nothing was shared.');
      else
        setError(
          'The report could not be saved. Choose another location and try again.',
        );
    }
    window.addEventListener('bloom-diagnostics-saved', saved);
    return () => {
      mounted.current = false;
      request.current?.abort();
      window.removeEventListener('bloom-diagnostics-saved', saved);
    };
  }, []);
  async function preview() {
    request.current?.abort();
    const controller = new AbortController();
    request.current = controller;
    const timeout = setTimeout(() => controller.abort(), 15000);
    setBusy(true);
    setError('');
    setMessage('');
    setReport(null);
    try {
      const response = await fetch('/api/diagnostics/preview', {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          'X-Bloom-Action': 'diagnostics',
        },
        body: JSON.stringify({ includeEarnings }),
        signal: controller.signal,
        cache: 'no-store',
      });
      if (!response.ok)
        throw Error(
          'Could not prepare diagnostics. Check the connection and try again.',
        );
      const text = await response.text();
      if (new TextEncoder().encode(text).length > 262144)
        throw Error('The report exceeded its size limit.');
      const value = JSON.parse(text);
      if (
        value?.schema !== 'bloom-diagnostics-v1' ||
        !value.app ||
        !value.hardware ||
        typeof value.generatedAt !== 'string' ||
        !Number.isFinite(Date.parse(value.generatedAt)) ||
        typeof value.app.version !== 'string' ||
        typeof value.hardware.chip !== 'string' ||
        !(
          value.hardware.memoryTotalGB == null ||
          (typeof value.hardware.memoryTotalGB === 'number' &&
            Number.isFinite(value.hardware.memoryTotalGB))
        ) ||
        value.privacy?.earningsIncluded !== includeEarnings ||
        !Array.isArray(value.issues) ||
        !value.issues.every(
          (issue: { code?: unknown; description?: unknown } | null) =>
            issue &&
            typeof issue.code === 'string' &&
            typeof issue.description === 'string',
        ) ||
        typeof value.optimizer?.mode !== 'string' ||
        !Array.isArray(value.optimizer?.recentDecisions) ||
        !Array.isArray(value.optimizer?.recentEvents)
      ) {
        throw Error(
          'The report format was not recognized. Refresh BloomGauge before trying again.',
        );
      }
      if (mounted.current && request.current === controller) setReport(value);
    } catch (err) {
      if (mounted.current && request.current === controller)
        setError(
          controller.signal.aborted
            ? 'The request timed out. Your provider was not changed.'
            : err instanceof SyntaxError
              ? 'The report format was not recognized. Refresh BloomGauge before trying again.'
              : (err as Error).message,
        );
    } finally {
      clearTimeout(timeout);
      if (mounted.current && request.current === controller) setBusy(false);
    }
  }
  const content = report ? JSON.stringify(report, null, 2) + '\n' : '';
  const filename =
    'Bloom-diagnostics-' +
    (report?.generatedAt.slice(0, 10) || 'report') +
    '.json';
  function file() {
    return new File([content], filename, { type: 'application/json' });
  }
  function download() {
    if (!report) return;
    setMessage('');
    setError('');
    const bridge = (window as NativeWindow).webkit?.messageHandlers
      ?.bloomDiagnostics;
    if (bridge) {
      saveId.current = crypto.randomUUID();
      setSaving(true);
      try {
        bridge.postMessage({
          action: 'save',
          requestId: saveId.current,
          content,
        });
      } catch {
        saveId.current = null;
        setSaving(false);
        setError('Could not open Save. Reload the dashboard and try again.');
      }
      return;
    }
    const url = URL.createObjectURL(file());
    const anchor = document.createElement('a');
    anchor.href = url;
    anchor.download = filename;
    document.body.appendChild(anchor);
    anchor.click();
    anchor.remove();
    setTimeout(() => URL.revokeObjectURL(url), 30000);
    setMessage(
      'Download requested. Check your browser’s downloads or Files. Nothing was uploaded.',
    );
  }
  async function share() {
    if (!report) return;
    setError('');
    setMessage('');
    try {
      await navigator.share({
        files: [file()],
        title: 'BloomGauge diagnostics',
      });
    } catch (err) {
      if ((err as Error).name !== 'AbortError')
        setError('Sharing is unavailable. Use Save report instead.');
    }
  }
  let canShare = false;
  try {
    canShare = Boolean(
      report &&
      typeof navigator !== 'undefined' &&
      typeof File !== 'undefined' &&
      typeof navigator.canShare === 'function' &&
      navigator.canShare({ files: [file()] }),
    );
  } catch {
    /* Saving remains available when file sharing is unsupported. */
  }
  return (
    <>
      <WebsiteLinks />
      <UpdateSettings />
      <section className="panel support-panel">
        <div className="panel-heading">
          <div>
            <p className="eyebrow">HELP & FEEDBACK</p>
            <h2>Get in touch.</h2>
          </div>
          <ShieldCheck size={23} aria-hidden="true" />
        </div>
        <p>
          Ask a question, report a problem, or suggest something for
          BloomGauge. Most setup issues are covered in the{' '}
          <button
            type="button"
            className="text-link"
            onClick={() => navigation.navigate('guide')}
          >
            Guide
          </button>
          .
        </p>
        <button
          type="button"
          className="action"
          onClick={() => openSupportReport('manual', 'help')}
        >
          Report a problem
        </button>
        <AutoSendSetting />
        <p className="footnote">
          Reports are private, optional and separate from usage sharing.
          Reporting a problem here lets you review the full report before
          sending.
        </p>
        <div className="support-actions">
          <a
            className="action"
            href="https://darkbloom.slack.com/archives/C0C4HC8HZLN"
            target="_blank"
            rel="noopener noreferrer"
          >
            Join the BloomGauge Slack channel
          </a>
          <a className="action" href="mailto:support@bloomgauge.io">
            Email support
          </a>
        </div>
        <p className="footnote">
          You choose what to send. Opening a contact link sends no report or
          account details.
        </p>
        <h3>Optional diagnostics</h3>
        <p>
          Prepare a small diagnostics report for troubleshooting. Review it
          here, then save or share it with someone you choose.
        </p>
        <div className="support-privacy">
          <strong>Your account stays private.</strong>
          <p>
            Includes app version, hardware capacity, connection status and
            recent optimizer decisions. Leaves out credentials, account IDs,
            private links, file paths and raw logs. Models use aliases with
            their family and size.
          </p>
        </div>
        <label className="support-option">
          <input
            type="checkbox"
            checked={includeEarnings}
            disabled={busy || saving}
            onChange={(event) => {
              setIncludeEarnings(event.target.checked);
              setReport(null);
              setMessage('');
              setError('');
            }}
          />
          <span>
            Include recorded inference earnings from the past 24 hours{' '}
            <small>Optional · excludes account balance and base rewards</small>
          </span>
        </label>
        <button
          className="action"
          type="button"
          disabled={busy || saving}
          onClick={preview}
        >
          <FileSearch size={16} />
          {busy
            ? 'Preparing…'
            : report
              ? 'Refresh preview'
              : 'Review diagnostics'}
        </button>
        {error && (
          <p role="alert" className="support-error">
            {error}
          </p>
        )}
        {message && (
          <p role="status" className="support-message">
            {message}
          </p>
        )}
        {report && (
          <div className="support-preview">
            <h3>Ready for your review</h3>
            <p>
              Captured {new Date(report.generatedAt).toLocaleString()} ·
              BloomGauge {report.app.version}
            </p>
            <dl>
              <div>
                <dt>Hardware</dt>
                <dd>
                  {report.hardware.chip}
                  {report.hardware.memoryTotalGB != null
                    ? ` · ${report.hardware.memoryTotalGB} GB`
                    : ''}
                </dd>
              </div>
              <div>
                <dt>Optimizer</dt>
                <dd>{report.optimizer.mode}</dd>
              </div>
              <div>
                <dt>Recent decisions / events</dt>
                <dd>
                  {report.optimizer.recentDecisions.length} /{' '}
                  {report.optimizer.recentEvents.length}
                </dd>
              </div>
              <div>
                <dt>Earnings</dt>
                <dd>
                  {report.privacy.earningsIncluded
                    ? 'Included — recorded inference only'
                    : 'Excluded'}
                </dd>
              </div>
            </dl>
            {report.issues.length > 0 && (
              <div className="support-issues">
                <strong>Items to investigate</strong>
                <ul>
                  {report.issues.map((issue) => (
                    <li key={issue.code}>
                      <code>{issue.code}</code>
                      <span>{issue.description}</span>
                    </li>
                  ))}
                </ul>
              </div>
            )}
            <details>
              <summary>View exact report</summary>
              <pre tabIndex={0} aria-label="Diagnostics JSON preview">
                {content}
              </pre>
            </details>
            <div className="support-actions">
              <button
                className="action"
                type="button"
                onClick={download}
                disabled={saving}
              >
                <Download size={16} />
                {saving ? 'Choose a save location…' : 'Save report'}
              </button>
              {canShare && (
                <button
                  className="action"
                  type="button"
                  onClick={share}
                  disabled={saving}
                >
                  <Share2 size={16} />
                  Share report
                </button>
              )}
            </div>
            <p className="footnote">
              Saving uses this exact preview. No automatic uploads or background
              sharing. A report describes observed state; it does not prove
              earnings improvements.
            </p>
          </div>
        )}
      </section>
    </>
  );
}
