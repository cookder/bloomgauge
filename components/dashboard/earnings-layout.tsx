'use client';
import { useEffect, useRef, useState, type ReactNode } from 'react';
import {
  ArrowDown,
  ArrowUp,
  GripVertical,
  LayoutDashboard,
  RotateCcw,
  X,
} from 'lucide-react';
import {
  dashboardWidgets,
  dashboardLayoutKey,
  defaultDashboardLayout,
  readDashboardLayout,
  serializeDashboardLayout,
  moveDashboardWidget,
  type DashboardLayout,
  type DashboardWidgetId,
  type DashboardWidth,
} from '@/lib/dashboard-layout';
import { ScreenActivityBoundary, useAppNavigation } from './app-navigation';

export function EarningsLayout({
  render,
}: {
  render: (id: DashboardWidgetId, visible: boolean) => ReactNode;
}) {
  const { mobile } = useAppNavigation();
  const drafts = useRef<
    Partial<Record<'phone' | 'desktop', DashboardLayout | null>>
  >({});
  const profile = mobile ? 'phone' : 'desktop';
  return (
    <LayoutEditor
      key={profile}
      mobile={mobile}
      render={render}
      initialDraft={drafts.current[profile] ?? null}
      onDraftChange={(value) => {
        drafts.current[profile] = value;
      }}
    />
  );
}

function LayoutEditor({
  mobile,
  render,
  initialDraft,
  onDraftChange,
}: {
  mobile: boolean;
  render: (id: DashboardWidgetId, visible: boolean) => ReactNode;
  initialDraft: DashboardLayout | null;
  onDraftChange: (value: DashboardLayout | null) => void;
}) {
  const [saved, setSaved] = useState(() => defaultDashboardLayout(mobile));
  const [draft, setDraftState] = useState<DashboardLayout | null>(initialDraft);
  function setDraft(value: DashboardLayout | null) {
    setDraftState(value);
    onDraftChange(value);
  }
  const [message, setMessage] = useState('');
  const [dragging, setDragging] = useState<DashboardWidgetId | null>(null);
  const [drop, setDrop] = useState<DashboardWidgetId | null>(null);
  const pointer = useRef<{
    id: DashboardWidgetId;
    x: number;
    y: number;
    moved: boolean;
    target: DashboardWidgetId | null;
  } | null>(null);
  const toolbar = useRef<HTMLButtonElement>(null);
  const layout = draft ?? saved,
    editing = draft !== null;
  const visible = layout.order.filter((id) => !layout.hidden.includes(id));
  useEffect(() => {
    try {
      setSaved(
        readDashboardLayout(
          localStorage.getItem(dashboardLayoutKey(mobile)),
          mobile,
        ),
      );
    } catch {
      setMessage(
        'Layout storage is unavailable. Changes will last for this visit.',
      );
    }
  }, [mobile]);
  function change(next: DashboardLayout) {
    setDraft(next);
  }
  function move(id: DashboardWidgetId, target: DashboardWidgetId) {
    const next = moveDashboardWidget(layout, id, target);
    change(next);
    const title = dashboardWidgets.find((w) => w.id === id)!.title;
    setMessage(
      `${title} moved to position ${next.order.filter((x) => !next.hidden.includes(x)).indexOf(id) + 1}.`,
    );
  }
  function finish(save: boolean) {
    if (save && draft) {
      setSaved(draft);
      try {
        localStorage.setItem(
          dashboardLayoutKey(mobile),
          serializeDashboardLayout(draft),
        );
        setMessage('Layout saved on this browser.');
      } catch {
        setMessage(
          'Layout applied for this visit. This browser could not save it.',
        );
      }
    } else setMessage('Layout changes canceled.');
    pointer.current = null;
    setDragging(null);
    setDrop(null);
    setDraft(null);
    requestAnimationFrame(() => toolbar.current?.focus());
  }
  function cancelDrag() {
    pointer.current = null;
    setDragging(null);
    setDrop(null);
  }
  return (
    <div className={`earnings-layout ${editing ? 'is-editing' : ''}`}>
      <div className="earnings-layout-toolbar">
        <div>
          <strong>
            <LayoutDashboard size={17} /> Your dashboard
          </strong>
          <span>
            {editing
              ? 'Drag a handle or use the move buttons. Changes are saved only when you finish.'
              : `${mobile ? 'Phone' : 'Desktop'} layout · saved on this browser`}
          </span>
        </div>
        <div className="earnings-layout-actions">
          {editing ? (
            <>
              <button
                type="button"
                onClick={() => {
                  change(defaultDashboardLayout(mobile));
                  setMessage(
                    'Default layout previewed. Save layout to keep it.',
                  );
                }}
              >
                <RotateCcw size={15} /> Reset layout
              </button>
              <button type="button" onClick={() => finish(false)}>
                Cancel
              </button>
              <button
                type="button"
                className="layout-save"
                onClick={() => finish(true)}
              >
                Save layout
              </button>
            </>
          ) : (
            <button
              ref={toolbar}
              type="button"
              onClick={() => {
                setDraft(saved);
                setMessage('Editing layout. Move, resize or hide cards.');
              }}
            >
              Edit layout
            </button>
          )}
        </div>
      </div>
      <p className="layout-announcement" role="status" aria-live="polite">
        {message}
      </p>
      {editing && (
        <div className="layout-available" aria-label="Dashboard cards">
          <span>Show cards</span>
          {dashboardWidgets.map((w) => (
            <label key={w.id}>
              <input
                type="checkbox"
                checked={!layout.hidden.includes(w.id)}
                onChange={(e) =>
                  change({
                    ...layout,
                    hidden: e.target.checked
                      ? layout.hidden.filter((id) => id !== w.id)
                      : [...layout.hidden, w.id],
                  })
                }
              />
              {w.title}
            </label>
          ))}
        </div>
      )}
      {!visible.length && (
        <p className="layout-empty">
          Your dashboard is empty.{' '}
          {editing ? (
            'Select a card above to add it.'
          ) : (
            <button type="button" onClick={() => setDraft(saved)}>
              Edit layout to add cards
            </button>
          )}
        </p>
      )}
      <div className="earnings-widget-grid">
        {layout.order.map((id) => {
          const widget = dashboardWidgets.find((w) => w.id === id)!,
            shown = !layout.hidden.includes(id),
            index = visible.indexOf(id);
          return (
            <div
              key={id}
              data-dashboard-widget={id}
              data-widget-title={widget.title}
              data-width={layout.widths[id]}
              hidden={!shown}
              className={`dashboard-widget ${widget.tile ? 'dashboard-tile' : ''} ${dragging === id ? 'is-dragging' : ''} ${drop === id ? 'is-drop-target' : ''}`}
            >
              {editing && shown && (
                <div className="widget-edit-controls">
                  <button
                    type="button"
                    className="widget-drag-handle"
                    aria-label={`Drag ${widget.title}`}
                    title="Drag to move. Arrow keys also move this card."
                    onPointerDown={(e) => {
                      if (e.button !== 0) return;
                      e.currentTarget.setPointerCapture(e.pointerId);
                      pointer.current = {
                        id,
                        x: e.clientX,
                        y: e.clientY,
                        moved: false,
                        target: null,
                      };
                    }}
                    onPointerMove={(e) => {
                      const p = pointer.current;
                      if (!p || p.id !== id) return;
                      if (
                        !p.moved &&
                        Math.hypot(e.clientX - p.x, e.clientY - p.y) < 8
                      )
                        return;
                      p.moved = true;
                      setDragging(id);
                      const target = document
                        .elementFromPoint(e.clientX, e.clientY)
                        ?.closest<HTMLElement>('[data-dashboard-widget]')
                        ?.dataset.dashboardWidget as
                        | DashboardWidgetId
                        | undefined;
                      p.target =
                        target && target !== id && visible.includes(target)
                          ? target
                          : null;
                      setDrop(p.target);
                    }}
                    onPointerUp={() => {
                      const p = pointer.current;
                      if (p?.moved && p.target) move(id, p.target);
                      cancelDrag();
                    }}
                    onPointerCancel={cancelDrag}
                    onKeyDown={(e) => {
                      if (e.key === 'Escape') {
                        cancelDrag();
                        return;
                      }
                      const target =
                        e.key === 'ArrowUp' || e.key === 'ArrowLeft'
                          ? visible[index - 1]
                          : e.key === 'ArrowDown' || e.key === 'ArrowRight'
                            ? visible[index + 1]
                            : undefined;
                      if (target) {
                        e.preventDefault();
                        move(id, target);
                      }
                    }}
                  >
                    <GripVertical size={18} />
                  </button>
                  <strong>{widget.title}</strong>
                  <div className="widget-edit-buttons">
                    <button
                      type="button"
                      aria-label={`Move ${widget.title} earlier`}
                      disabled={index === 0}
                      onClick={() => move(id, visible[index - 1])}
                    >
                      <ArrowUp size={16} />
                    </button>
                    <button
                      type="button"
                      aria-label={`Move ${widget.title} later`}
                      disabled={index === visible.length - 1}
                      onClick={() => move(id, visible[index + 1])}
                    >
                      <ArrowDown size={16} />
                    </button>
                    {!mobile && (
                      <select
                        aria-label={`${widget.title} width`}
                        value={layout.widths[id]}
                        onChange={(e) =>
                          change({
                            ...layout,
                            widths: {
                              ...layout.widths,
                              [id]: e.target.value as DashboardWidth,
                            },
                          })
                        }
                      >
                        {widget.tile && (
                          <option value="compact">Quarter</option>
                        )}
                        <option value="half">Half</option>
                        <option value="full">Full</option>
                      </select>
                    )}
                    <button
                      type="button"
                      aria-label={`Hide ${widget.title}`}
                      onClick={() => {
                        change({ ...layout, hidden: [...layout.hidden, id] });
                        setMessage(
                          `${widget.title} hidden. Add it again under Show cards.`,
                        );
                      }}
                    >
                      <X size={16} />
                    </button>
                  </div>
                </div>
              )}
              <ScreenActivityBoundary active={shown}>
                {render(id, shown)}
              </ScreenActivityBoundary>
            </div>
          );
        })}
      </div>
    </div>
  );
}
