// First "Optimizer on": a short explainer and an optional 3-day Learning boost.
export const introSeenKey = 'bloom-optimizer-intro-seen-v1';
export const introBoostKey = 'bloom-optimizer-intro-boost-v1';
export const introBoostSeconds = 259200;

export const introPoints: { title: string; text: string }[] = [
  {
    title: 'Runs your best earner',
    text: 'Most of the time Bloomkeeper keeps whichever model is earning the most on this Mac right now.',
  },
  {
    title: 'Catches demand spikes',
    text: 'When a big model gets a rush of paid requests, Bloomkeeper can switch to it while the rush lasts. Large Qwen models can pay more than $0.30 an hour during a spike.',
  },
  {
    title: 'Keeps a fallback',
    text: 'When the main earners (such as Gemma or Nemotron) go quiet, Bloomkeeper moves to a model that almost always has some demand, such as gpt-oss, instead of sitting idle.',
  },
];

/** The manager strategy: what "Manager on" does (native/manager.py). No learning trials. */
export const managerIntroPoints: { title: string; text: string }[] = [
  {
    title: 'Holds your best model',
    text: 'Bloomkeeper keeps a home model running: your pick if you pinned one, otherwise the model that has paid best on this Mac over the last 30 days, judged day by day so one lucky day can’t decide it. Until this Mac has 3 days of its own, it starts from what pays best on Macs with the same chip and memory, and tells you before switching. It does not run blind trials of other models.',
  },
  {
    title: 'Recovers by itself',
    text: 'If a switch fails or no model has been ready for about ten minutes, Bloomkeeper restores the home model instead of turning itself off. If two restores in a row fail, it tells you and keeps retrying, at least every two hours.',
  },
  {
    title: 'Moves only on strong evidence',
    text: 'With “Switch to better models when network evidence is strong” on, it leaves home only when at least five Macs like this one have clearly earned more on another model for two hours in a row. It comes back when that evidence fades or the model pays less than home would, and turns these moves off if they haven’t clearly paid off.',
  },
];

export function learningText(
  protectUsdPerHour: number,
  learningMinutesPerDay: number,
) {
  const time =
    learningMinutesPerDay === 0
      ? 'no time'
      : learningMinutesPerDay < 60
        ? `up to ${learningMinutesPerDay} minutes a day`
        : `up to ${Number((learningMinutesPerDay / 60).toFixed(2))} hour${learningMinutesPerDay === 60 ? '' : 's'} a day`;
  return `Bloomkeeper only knows what a model pays on your Mac by running it. So it spends ${time} measuring other models, only while pace is below $${protectUsdPerHour.toFixed(2)} an hour, and only models with real demand. A model earning more than that is never interrupted just to learn.`;
}

const pause = (ms: number, signal: AbortSignal) =>
  new Promise<void>((resolve, reject) => {
    const t = setTimeout(resolve, ms);
    signal.addEventListener(
      'abort',
      () => {
        clearTimeout(t);
        reject(signal.reason);
      },
      { once: true },
    );
  });

/** Start a Learning boost once On has been accepted. Retries while the optimizer is still settling. */
export async function startIntroBoost(
  signal: AbortSignal,
  fetcher: typeof fetch = fetch,
  waitMs = 3000,
): Promise<'started' | 'already'> {
  let last: unknown = null;
  for (let attempt = 0; attempt < 6; attempt++) {
    if (attempt) await pause(waitMs, signal);
    try {
      const current = await fetcher('/api/optimizer', {
        cache: 'no-store',
        signal,
      });
      const value = (await current.json()) as {
        controlVersion?: unknown;
        demandAuto?: { dataGathering?: { active?: unknown } };
      };
      if (!current.ok || typeof value.controlVersion !== 'string')
        throw Error('Optimizer status is unavailable.');
      if (value.demandAuto?.dataGathering?.active === true) return 'already';
      const response = await fetcher('/api/optimizer', {
        method: 'POST',
        signal,
        cache: 'no-store',
        headers: {
          'Content-Type': 'application/json',
          'X-Bloom-Action': 'optimizer',
        },
        body: JSON.stringify({
          action: 'data-gathering',
          seconds: introBoostSeconds,
          expectedControl: value.controlVersion,
        }),
      });
      if (response.ok) return 'started';
      const error = (await response.json().catch(() => null)) as {
        error?: unknown;
      } | null;
      last = Error(
        typeof error?.error === 'string'
          ? error.error
          : 'Could not start the Learning boost.',
      );
    } catch (e) {
      if (signal.aborted) throw e;
      last = e;
    }
  }
  throw last instanceof Error
    ? last
    : Error('Could not start the Learning boost.');
}
