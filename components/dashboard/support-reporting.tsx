'use client';
import {
  Component,
  useEffect,
  useRef,
  useState,
  useSyncExternalStore,
  type ReactNode,
} from 'react';
import { MessageSquareWarning, X } from 'lucide-react';
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogTitle,
} from '@/components/ui/dialog';
import {
  autoSendSupportPrompt,
  clearSupportPrompt,
  dismissSupportIssue,
  getServerSupportIssues,
  getSupportIssues,
  recordSupportIssue,
  setSupportReporting,
  subscribeSupportIssues,
  supportIssueTitle,
  supportTitles,
  type SupportIssue,
} from '@/lib/support-issues';
import {
  quickSupportReport,
  readAutoSend,
  supportRequest,
  validSupportPreview,
  validSupportReceipt,
  writeAutoSend,
  SupportRequestError,
  type SupportPreview,
  type SupportReport,
  SEND_TIMEOUT_MS,
} from '@/lib/support-report';

export function SupportReporting({ children }: { children: ReactNode }) {
  useEffect(() => {
    // Never inspect or retain an Error, rejection reason, URL, stack or DOM text.
    const runtime = (event: Event) => {
      if (event instanceof ErrorEvent) recordSupportIssue('ui');
    };
    const rejection = () => recordSupportIssue('ui');
    const polling = (event: Event) => {
      const detail = (event as CustomEvent).detail;
      if (detail && typeof detail === 'object')
        recordSupportIssue(detail.category, detail.context, detail.source);
    };
    window.addEventListener('error', runtime);
    window.addEventListener('unhandledrejection', rejection);
    window.addEventListener('bloom-support-issue', polling);
    return () => {
      window.removeEventListener('error', runtime);
      window.removeEventListener('unhandledrejection', rejection);
      window.removeEventListener('bloom-support-issue', polling);
    };
  }, []);
  return (
    <>
      {children}
      <ReportingBoundary>
        <SupportReportHost />
      </ReportingBoundary>
    </>
  );
}
class ReportingBoundary extends Component<
  { children: ReactNode },
  { failed: boolean }
> {
  state = { failed: false };
  static getDerivedStateFromError() {
    return { failed: true };
  }
  render() {
    return this.state.failed ? (
      <aside className="support-report-fallback">
        <a
          href="https://bloomformac.com/support"
          target="_blank"
          rel="noreferrer"
        >
          Reporting unavailable · Contact BloomGauge support ↗
        </a>
      </aside>
    ) : (
      this.props.children
    );
  }
}
type Phase =
  | 'edit'
  | 'preparing'
  | 'review'
  | 'sending'
  | 'unconfirmed'
  | 'sent';
const contextNames = {
  overview: 'Overview',
  setup: 'Setup',
  models: 'Models',
  phone: 'Phone access',
  earnings: 'Earnings',
  hardware: 'Hardware',
  network: 'Network',
  help: 'Help & feedback',
  unknown: 'Unknown screen',
};
const memoryNames: Record<string, string> = {
  'up-to-16': 'Up to 16 GB',
  '17-32': '17–32 GB',
  '33-64': '33–64 GB',
  '65-128': '65–128 GB',
  'over-128': 'Over 128 GB',
  unknown: 'Memory unknown',
};
const modeNames: Record<string, string> = {
  observe: 'Observe only',
  week: 'Weekly test',
  combo: 'Combination test',
  best: 'Best earnings',
  demand: 'Demand',
  unknown: 'Unknown mode',
};
const readable = (value: string) => value.replace(/[-_]/g, ' ');
function ReportSummary({ report }: { report: SupportReport }) {
  const d = report.diagnostics;
  return (
    <dl className="support-report-summary" aria-label="Report summary">
      <div>
        <dt>Issue</dt>
        <dd>
          {supportTitles[report.category]} · {contextNames[report.context]}
        </dd>
      </div>
      <div>
        <dt>App & Mac</dt>
        <dd>
          BloomGauge {report.appVersion} ·{' '}
          {report.surface === 'phone' ? 'Phone page' : 'Mac dashboard'}
          <br />
          {d.chipFamily === 'Other' ? 'Chip unknown' : d.chipFamily} ·{' '}
          {d.osMajor ? `macOS ${d.osMajor}` : 'macOS unknown'} ·{' '}
          {memoryNames[d.memoryBand]}
        </dd>
      </div>
      <div>
        <dt>Connections</dt>
        <dd>
          Provider:{' '}
          {d.providerOnline === null
            ? 'unknown'
            : d.providerOnline
              ? 'online'
              : 'offline'}
          {d.providerVersion !== 'unknown' ? ` (${d.providerVersion})` : ''}
          {d.sources.map((source) => (
            <span key={source.name}>
              {readable(source.name)}: {readable(source.status)}
              {source.error !== 'none' ? ` · ${readable(source.error)}` : ''}
            </span>
          ))}
        </dd>
      </div>
      <div>
        <dt>Optimizer</dt>
        <dd>
          {modeNames[d.optimizerMode]}
          {d.optimizerStrategy === 'manager'
            ? ' (Manager)'
            : d.optimizerStrategy === 'legacy'
              ? ' (older optimizer)'
              : ''}{' '}
          · {readable(d.optimizerStatus)}
          <span>
            Failure: {readable(d.failureCode)} · Recovery:{' '}
            {readable(d.recoveryCode)}
          </span>
        </dd>
      </div>
      <div>
        <dt>Your notes</dt>
        <dd className="support-report-verbatim">
          {report.description || 'Not provided'}
        </dd>
      </div>
      <div>
        <dt>Reply contact</dt>
        <dd className="support-report-verbatim">
          {report.contact || 'Not provided — Andrew cannot reply directly'}
        </dd>
      </div>
    </dl>
  );
}
function SupportReportHost() {
  const notifications = useSyncExternalStore(
    subscribeSupportIssues,
    getSupportIssues,
    getServerSupportIssues,
  );
  const [open, setOpen] = useState(false),
    [issue, setIssue] = useState<SupportIssue>({
      category: 'manual',
      context: 'help',
    });
  const [description, setDescription] = useState(''),
    [contact, setContact] = useState('');
  const [preview, setPreview] = useState<SupportPreview | null>(null),
    [phase, setPhase] = useState<Phase>('edit'),
    [error, setError] = useState('');
  const request = useRef<AbortController | null>(null),
    generation = useRef(0),
    handled = useRef(0),
    pending = useRef(false),
    autoSending = useRef<SupportIssue | null>(null);
  const busy = phase === 'preparing' || phase === 'sending';
  // Opt-in automatic reports: same allowlisted report as a tap, sent when a problem is recorded.
  const [autoSend, setAutoSend] = useState(false),
    [autoNotice, setAutoNotice] = useState(false);
  useEffect(() => {
    const control = new AbortController();
    readAutoSend(control.signal)
      .then(setAutoSend)
      .catch(() => {});
    return () => control.abort();
  }, []);
  useEffect(() => {
    const issue = notifications.prompt;
    if (!issue || !autoSend || open || autoSending.current === issue) return;
    // Sent or held back, the prompt is cleared so later problems can still be reported.
    autoSending.current = issue;
    void autoSendSupportPrompt(issue, (fields) =>
      quickSupportReport(
        { ...fields, description: '', contact: '' },
        AbortSignal.timeout(20000 + SEND_TIMEOUT_MS),
        true,
      ),
    ).then((sent) => {
      if (autoSending.current === issue) autoSending.current = null;
      if (!sent) return;
      setAutoNotice(true);
      setTimeout(() => setAutoNotice(false), 10000);
    });
  }, [notifications.prompt, autoSend, open]);
  useEffect(() => {
    const next = notifications.request;
    if (!next || next.sequence === handled.current) return;
    handled.current = next.sequence;
    // Preserve an uncertain delivery so reopening cannot silently create a duplicate.
    if (phase !== 'unconfirmed') {
      generation.current++;
      request.current?.abort();
      pending.current = false;
      setIssue({ category: next.category, context: next.context });
      setDescription('');
      setContact('');
      setPreview(null);
      setPhase('edit');
      setError('');
    }
    setOpen(true);
  }, [notifications.request, phase]);
  useEffect(() => {
    setSupportReporting(open);
    return () => setSupportReporting(false);
  }, [open]);
  useEffect(
    () => () => {
      generation.current++;
      request.current?.abort();
    },
    [],
  );
  function changeOpen(value: boolean) {
    if (phase === 'sending') return;
    if (!value) {
      generation.current++;
      request.current?.abort();
      pending.current = false;
      if (phase === 'preparing') setPhase('edit');
    }
    setOpen(value);
  }
  function edit() {
    generation.current++;
    request.current?.abort();
    pending.current = false;
    setPreview(null);
    setPhase('edit');
    setError(
      phase === 'unconfirmed'
        ? 'The previous delivery was not confirmed and may already have arrived. A new preview creates a different report.'
        : '',
    );
  }
  async function prepare() {
    if (pending.current) return;
    pending.current = true;
    const own = ++generation.current,
      control = new AbortController();
    request.current = control;
    setPhase('preparing');
    setError('');
    setPreview(null);
    const fields = { ...issue, description, contact };
    try {
      const value = await supportRequest('preview', fields, control.signal);
      if (own !== generation.current) return;
      if (!validSupportPreview(value, fields)) throw Error('invalid');
      setPreview(value);
      setPhase('review');
    } catch (error) {
      if (own === generation.current) {
        setPhase('edit');
        setError(
          error instanceof SupportRequestError &&
            error.status === 'preview_limit'
            ? 'There are too many recent previews. Wait up to ten minutes for them to expire, then review again. Nothing was sent.'
            : 'Could not prepare a private preview. Keep BloomGauge open on your Mac and check the connection, then try again. You can also use the support link below. Nothing was sent.',
        );
      }
    } finally {
      if (own === generation.current) pending.current = false;
    }
  }
  async function send() {
    if (pending.current || !preview) return;
    pending.current = true;
    const own = ++generation.current,
      control = new AbortController();
    request.current = control;
    setPhase('sending');
    setError('');
    try {
      const result = await supportRequest(
        'send',
        {
          reportId: preview.report.id,
          reviewToken: preview.reviewToken,
          confirmed: true,
        },
        control.signal,
        SEND_TIMEOUT_MS,
      );
      if (own !== generation.current) return;
      if (!validSupportReceipt(result, preview.report.id))
        throw Error('unconfirmed');
      setPhase('sent');
    } catch (error) {
      if (own === generation.current) {
        const status =
          error instanceof SupportRequestError ? error.status : 'unconfirmed';
        if (status === 'expired') {
          setPreview(null);
          setPhase('edit');
          setError(
            'This preview has expired. Review the report again before sending. A previous unconfirmed attempt may already have arrived.',
          );
        } else {
          setPhase('unconfirmed');
          setError(
            status === 'busy'
              ? 'This report is still being processed. Wait, then Retry same report to check delivery. Nothing retries automatically.'
              : status === 'rate_limited'
                ? 'The reporting limit has been reached. Please try later or contact support below. Nothing retries automatically.'
                : status === 'unavailable'
                  ? 'Reporting is temporarily unavailable. Keep this report to retry, or contact support below. Nothing retries automatically.'
                  : 'Delivery was not confirmed. It may already have arrived. Nothing will retry automatically; Retry same report uses the same report ID.',
          );
        }
      }
    } finally {
      if (own === generation.current) pending.current = false;
    }
  }
  return (
    <>
      {notifications.prompt && !open && !autoSend && (
        <QuickReportPrompt
          key={
            notifications.prompt.category + ':' + notifications.prompt.context
          }
          issue={notifications.prompt}
          onAutoEnabled={() => setAutoSend(true)}
        />
      )}
      {autoNotice && !notifications.prompt && (
        <aside
          className="support-report-prompt"
          role="status"
          aria-label="Automatic problem report"
        >
          <MessageSquareWarning size={18} aria-hidden="true" />
          <div>
            <strong>Problem report sent automatically</strong>
            <span>Thanks. It helps Andrew fix this sooner.</span>
            <button
              type="button"
              onClick={() => {
                void writeAutoSend(false, AbortSignal.timeout(10000))
                  .then(setAutoSend)
                  .catch(() => {});
                setAutoNotice(false);
              }}
            >
              Stop sending automatically
            </button>
          </div>
          <button
            type="button"
            className="support-prompt-dismiss"
            aria-label="Close"
            onClick={() => setAutoNotice(false)}
          >
            <X size={18} />
          </button>
        </aside>
      )}
      <Dialog open={open} onOpenChange={changeOpen}>
        <DialogContent
          className="support-report-dialog"
          showCloseButton={phase !== 'sending'}
        >
          <DialogTitle>
            {phase === 'sent'
              ? 'Report sent'
              : preview
                ? 'Review before sending'
                : 'Report a problem'}
          </DialogTitle>
          <DialogDescription>
            {phase === 'sent'
              ? 'Your report reached Andrew’s private BloomGauge support dashboard.'
              : preview
                ? 'This is the report that will be sent to Andrew’s private BloomGauge support dashboard.'
                : 'Reporting is optional. Nothing is sent until you review the report and choose Send.'}
          </DialogDescription>
          {phase === 'sent' && preview ? (
            <div className="support-report-receipt" role="status">
              <strong>Report ID</strong>
              <code>{preview.report.id}</code>
              <p>
                Keep this ID if you contact support. No automatic follow-up
                reports are enabled.
              </p>
              <button
                type="button"
                className="action"
                onClick={() => changeOpen(false)}
              >
                Done
              </button>
            </div>
          ) : (
            <>
              {!preview ? (
                <form
                  className="support-report-form"
                  onSubmit={(event) => {
                    event.preventDefault();
                    void prepare();
                  }}
                >
                  <p className="support-report-context">
                    {supportTitles[issue.category]} · {issue.context}
                  </p>
                  <label htmlFor="support-description">
                    What happened? <span>Optional</span>
                    <textarea
                      id="support-description"
                      maxLength={2000}
                      rows={4}
                      value={description}
                      disabled={busy}
                      onChange={(event) => {
                        setDescription(event.target.value);
                        setPreview(null);
                      }}
                      placeholder="What were you trying to do?"
                    />
                  </label>
                  <label htmlFor="support-contact">
                    How can Andrew reach you?{' '}
                    <span>Optional email or Slack handle</span>
                    <input
                      id="support-contact"
                      type="text"
                      maxLength={254}
                      autoComplete="off"
                      value={contact}
                      disabled={busy}
                      onChange={(event) => {
                        setContact(event.target.value);
                        setPreview(null);
                      }}
                    />
                  </label>
                  <p className="footnote">
                    Only the text you choose to write is included. Leave out
                    passwords, tokens, private links and personal account
                    details. No contact information is filled in automatically.
                  </p>
                  <p className="footnote">
                    The preview includes basic app/Mac information and safe
                    connection or model failure categories. It excludes
                    earnings, account IDs, model names and raw logs. Usage
                    sharing stays separate.
                  </p>
                  <button type="submit" className="action" disabled={busy}>
                    {phase === 'preparing'
                      ? 'Preparing preview…'
                      : 'Review report'}
                  </button>
                </form>
              ) : (
                <div className="support-report-preview">
                  <p className="support-report-context">
                    Snapshot taken{' '}
                    {new Date(preview.report.generatedAt).toLocaleString()}
                  </p>
                  {!preview.report.diagnostics.available && (
                    <p className="notice">
                      Detailed diagnostics were unavailable. This report
                      contains the safe summary shown below; unknown information
                      stays unknown.
                    </p>
                  )}
                  <ReportSummary report={preview.report} />
                  <details>
                    <summary>Exact report JSON</summary>
                    <pre tabIndex={0} aria-label="Exact problem report">
                      {JSON.stringify(preview.report, null, 2)}
                    </pre>
                  </details>
                  <div className="support-report-actions">
                    <button
                      type="button"
                      className="action"
                      disabled={busy}
                      onClick={() => void send()}
                    >
                      {phase === 'sending'
                        ? 'Sending…'
                        : phase === 'unconfirmed'
                          ? 'Retry same report'
                          : 'Send to BloomGauge support'}
                    </button>
                    <button
                      type="button"
                      className="small-button"
                      disabled={busy}
                      onClick={edit}
                    >
                      Edit report
                    </button>
                  </div>
                  <p className="footnote">
                    Sends only this reviewed report. No background retries or
                    changes to your provider.
                  </p>
                </div>
              )}
              {error && (
                <p className="support-error" role="alert">
                  {error}
                </p>
              )}
              <a
                className="text-link"
                href="https://bloomformac.com/support"
                target="_blank"
                rel="noreferrer"
              >
                Contact support another way ↗
              </a>
            </>
          )}
          <p className="footnote support-report-privacy">
            Reports are private and kept for a 30-day reporting window. Add
            optional contact information if you want a reply; keep your report
            ID to request deletion.{' '}
            <a
              href="https://bloomformac.com/privacy"
              target="_blank"
              rel="noreferrer"
            >
              Privacy details ↗
            </a>
          </p>
        </DialogContent>
      </Dialog>
    </>
  );
}

type QuickPhase = 'ask' | 'sending' | 'sent' | 'failed';
/** One tap to send the allowlisted report; a note, contact and auto-send are optional extras. */
function QuickReportPrompt({
  issue,
  onAutoEnabled,
}: {
  issue: SupportIssue;
  onAutoEnabled: () => void;
}) {
  const [phase, setPhase] = useState<QuickPhase>('ask'),
    [noteOpen, setNoteOpen] = useState(false),
    [includedOpen, setIncludedOpen] = useState(false);
  const [note, setNote] = useState(''),
    [contact, setContact] = useState(''),
    [always, setAlways] = useState(false),
    [error, setError] = useState('');
  const control = useRef<AbortController | null>(null);
  useEffect(() => () => control.current?.abort(), []);
  async function send() {
    if (phase === 'sending') return;
    control.current = new AbortController();
    setPhase('sending');
    setError('');
    try {
      if (
        always &&
        (await writeAutoSend(true, control.current.signal).catch(() => false))
      )
        onAutoEnabled();
      await quickSupportReport(
        { ...issue, description: note.trim(), contact: contact.trim() },
        control.current.signal,
      );
      setPhase('sent');
      setTimeout(clearSupportPrompt, 6000);
    } catch (error) {
      setPhase('failed');
      setError(
        error instanceof SupportRequestError && error.status === 'rate_limited'
          ? 'Reporting limit reached for this hour. Nothing else was sent.'
          : 'Couldn’t confirm it was sent. Try again, or use More → Help & feedback.',
      );
    }
  }
  if (phase === 'sent')
    return (
      <aside
        className="support-report-prompt"
        role="status"
        aria-label="Problem report sent"
      >
        <MessageSquareWarning size={18} aria-hidden="true" />
        <div>
          <strong>Report sent. Thank you.</strong>
          <span>Andrew reviews reports daily.</span>
        </div>
        <button
          type="button"
          className="support-prompt-dismiss"
          aria-label="Close"
          onClick={clearSupportPrompt}
        >
          <X size={18} />
        </button>
      </aside>
    );
  return (
    <aside
      className="support-report-prompt quick"
      role="status"
      aria-label="Send a problem report"
    >
      <MessageSquareWarning size={18} aria-hidden="true" />
      <div>
        <strong>{supportIssueTitle(issue)}</strong>
        <span>
          Send a quick report so it can be fixed?{' '}
          <button
            type="button"
            className="support-included-toggle"
            aria-expanded={includedOpen}
            onClick={() => setIncludedOpen((open) => !open)}
          >
            What’s included
          </button>
        </span>
        {includedOpen && (
          <small className="support-included">
            App version, Mac chip and memory size, and connection and optimizer
            status codes. Never earnings, account details, model names or logs.
          </small>
        )}
        {noteOpen && (
          <div className="support-quick-note">
            <textarea
              aria-label="What happened (optional)"
              maxLength={2000}
              rows={2}
              value={note}
              placeholder="What were you doing? (optional)"
              onChange={(event) => setNote(event.target.value)}
            />
            <input
              aria-label="How to reach you (optional)"
              maxLength={254}
              autoComplete="off"
              value={contact}
              placeholder="Email or Slack, for a reply (optional)"
              onChange={(event) => setContact(event.target.value)}
            />
          </div>
        )}
        <label className="support-quick-always">
          <input
            type="checkbox"
            checked={always}
            onChange={(event) => setAlways(event.target.checked)}
          />
          Send these automatically from now on
        </label>
        <div className="support-quick-actions">
          <button
            type="button"
            className="support-quick-send"
            disabled={phase === 'sending'}
            onClick={() => void send()}
          >
            {phase === 'sending'
              ? 'Sending…'
              : phase === 'failed'
                ? 'Try again'
                : 'Send report'}
          </button>
          {!noteOpen && (
            <button type="button" onClick={() => setNoteOpen(true)}>
              Add a note
            </button>
          )}
          <button type="button" onClick={dismissSupportIssue}>
            Not now
          </button>
        </div>
        {error && (
          <span className="support-error" role="alert">
            {error}
          </span>
        )}
      </div>
    </aside>
  );
}
