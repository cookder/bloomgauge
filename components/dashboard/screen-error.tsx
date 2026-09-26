'use client';
import { Component, Fragment, type ReactNode } from 'react';
import {
  openSupportReport,
  recordSupportIssue,
  supportContext,
} from '@/lib/support-issues';

// One failed chart must not take away navigation or the rest of the dashboard.
export class ScreenErrorBoundary extends Component<
  { children: ReactNode; name: string },
  { failed: boolean; attempt: number }
> {
  state = { failed: false, attempt: 0 };

  static getDerivedStateFromError() {
    return { failed: true };
  }

  componentDidCatch() {
    recordSupportIssue('ui', supportContext(this.props.name));
  }

  render() {
    if (this.state.failed) {
      return (
        <section className="panel" role="alert">
          <h2>This view couldn’t be displayed.</h2>
          <p className="footnote">
            Your Mac keeps collecting. You can retry this view or use another
            section.
          </p>
          <button
            className="small-button"
            onClick={() =>
              this.setState(({ attempt }) => ({
                failed: false,
                attempt: attempt + 1,
              }))
            }
          >
            Retry this view
          </button>
          <button
            className="small-button"
            onClick={() =>
              openSupportReport('ui', supportContext(this.props.name))
            }
          >
            Review and report
          </button>
        </section>
      );
    }
    return <Fragment key={this.state.attempt}>{this.props.children}</Fragment>;
  }
}
