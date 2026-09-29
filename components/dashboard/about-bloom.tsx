'use client';
import { UsageSharing } from './usage-sharing';
import { ContactOptIn } from './contact-opt-in';
import { PaySharing } from './pay-sharing';

export function AboutBloom() {
  return (
    <section className="bloom-plan" aria-label="About BloomGauge">
      <div className="plan-heading">
        <div className="eyebrow">YOUR MAC. YOUR CHOICE.</div>
        <h1>
          BloomGauge is free.
          <br />
          <span>Automation is your choice.</span>
        </h1>
        <p>
          Reporting, manual controls, private phone access and the optimizer are
          included. BloomGauge never turns the optimizer on by itself, and
          updating BloomGauge does not resume a plan you paused.
        </p>
        <p className="small muted">
          BloomGauge (formerly Bloomkeeper) is an independent companion for
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
