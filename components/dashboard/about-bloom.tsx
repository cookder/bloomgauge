'use client';
import { UsageSharing } from './usage-sharing';
import { ContactOptIn } from './contact-opt-in';
import { PaySharing } from './pay-sharing';

export function AboutBloom() {
  return (
    <section className="bloom-plan" aria-label="About Bloomkeeper">
      <div className="plan-heading">
        <div className="eyebrow">YOUR MAC. YOUR CHOICE.</div>
        <h1>
          Bloomkeeper is free.
          <br />
          <span>Automation is your choice.</span>
        </h1>
        <p>
          Reporting, manual controls, private phone access and the optimizer are
          included. Bloomkeeper never turns the optimizer on by itself, and
          updating Bloomkeeper does not resume a plan you paused.
        </p>
        <p className="small muted">
          Bloomkeeper (formerly Bloom) is an independent companion for
          Darkbloom. It is not made by or affiliated with Darkbloom or Eigen
          Labs.
        </p>
      </div>
      <UsageSharing />
      <ContactOptIn />
      <PaySharing />
    </section>
  );
}
