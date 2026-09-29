'use client';
import { BookOpen } from 'lucide-react';
import { guideProblems, guideSections } from '@/lib/guide-content';
import { openSupportReport } from '@/lib/support-issues';

/** In-app Guide: walkthroughs plus symptom-first troubleshooting. Same content as bloomformac.com/guide. */
export function Guide() {
  const jump = (id: string) =>
    document
      .getElementById('guide-' + id)
      ?.scrollIntoView({ block: 'start', behavior: 'smooth' });
  return (
    <section className="panel guide" aria-label="BloomGauge guide">
      <div className="panel-heading">
        <div>
          <p className="eyebrow">GUIDE</p>
          <h2>How to use BloomGauge.</h2>
        </div>
        <BookOpen size={23} aria-hidden="true" />
      </div>
      <nav className="guide-contents" aria-label="Guide contents">
        {guideSections.map((section) => (
          <button
            key={section.id}
            type="button"
            className="text-link"
            onClick={() => jump(section.id)}
          >
            {section.title}
          </button>
        ))}
        <button
          type="button"
          className="text-link"
          onClick={() => jump('troubleshooting')}
        >
          Troubleshooting
        </button>
      </nav>
      {guideSections.map((section) => (
        <article
          key={section.id}
          id={'guide-' + section.id}
          className="guide-section"
        >
          <h3>{section.title}</h3>
          <p className="guide-summary">{section.summary}</p>
          <ol>
            {section.steps.map((step, index) => (
              <li key={index}>
                {step.title && <strong>{step.title}. </strong>}
                {step.text}
              </li>
            ))}
          </ol>
          {section.note && <p className="footnote">{section.note}</p>}
        </article>
      ))}
      <article id="guide-troubleshooting" className="guide-section">
        <h3>Troubleshooting</h3>
        <p className="guide-summary">
          Find the message you see, then try the fixes in order.
        </p>
        <div className="guide-problems">
          {guideProblems.map((problem) => (
            <div key={problem.id} className="guide-problem">
              <strong>{problem.symptom}</strong>
              <p>{problem.cause}</p>
              <ol>
                {problem.fixes.map((fix, index) => (
                  <li key={index}>{fix}</li>
                ))}
              </ol>
            </div>
          ))}
        </div>
        <button
          type="button"
          className="action"
          onClick={() => openSupportReport('manual', 'help')}
        >
          Still stuck? Report a problem
        </button>
      </article>
    </section>
  );
}
