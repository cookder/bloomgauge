// Shared by the in-app Guide and bloomkeeper.io/guide. Plain data only, so the
// website can copy this file unchanged. Keep labels matching the app's UI text.

export type GuideStep = { title?: string; text: string };
export type GuideSection = {
  id: string;
  title: string;
  summary: string;
  steps: GuideStep[];
  note?: string;
};
export type GuideProblem = {
  id: string;
  symptom: string;
  cause: string;
  fixes: string[];
};

export const guideSections: GuideSection[] = [
  {
    id: 'start',
    title: 'Get started',
    summary: 'Install Bloomkeeper and connect it to Darkbloom on this Mac.',
    steps: [
      {
        title: 'Check requirements',
        text: 'You need an Apple Silicon Mac on macOS 14 or later, with the Darkbloom provider installed and signed in. Bloomkeeper works alongside Darkbloom; it does not replace it.',
      },
      {
        title: 'Install',
        text: 'Download Bloomkeeper from bloomkeeper.io, drag it into Applications and open it.',
      },
      {
        title: 'Finish setup',
        text: 'Setup has three short steps: Your Mac, Make it yours, Ready to observe. Bloomkeeper starts in observe mode, so it changes nothing until you choose to.',
      },
      {
        title: 'Keep it running',
        text: 'Closing the window keeps Bloomkeeper collecting. Reopen it from the leaf in the menu bar. Quit Bloomkeeper stops Bloomkeeper only; Darkbloom keeps serving.',
      },
    ],
  },
  {
    id: 'manual',
    title: 'Choose models yourself',
    summary: 'Manual mode: you pick the model, Bloomkeeper handles the switch.',
    steps: [
      { text: 'Open Optimizer and choose Manual on the card at the top.' },
      { text: 'Pick a model under Model to run, then choose Start or Switch.' },
      {
        text: 'Bloomkeeper restarts Darkbloom with that model and checks it is warm and serving before calling it ready. With cache cleanup permission (see Troubleshooting), it also clears the macOS file cache first so large models fit.',
      },
    ],
    note: 'Manual always pauses automatic switching.',
  },
  {
    id: 'what-optimizer-does',
    title: 'What the optimizer does',
    summary: 'Three jobs it does on its own, and why it learns.',
    steps: [
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
      {
        title: 'Why it learns',
        text: 'Bloomkeeper only knows what a model pays on your Mac by running it. It spends a little time a day (Learning time, default 1 hour) measuring other models, only while pace is below your protect level and only models with real demand. A model earning more than that is never interrupted just to learn.',
      },
    ],
    note: 'The first time you choose Optimizer on, Bloomkeeper shows this summary and offers a 3-day Learning boost for a new Mac. Open it again any time with What it does on the optimizer card.',
  },
  {
    id: 'optimizer',
    title: 'Let Bloomkeeper choose',
    summary: 'Optimizer on: Bloomkeeper follows demand and your own paid work.',
    steps: [
      {
        title: 'Pick the models',
        text: 'Under Models Bloomkeeper can use, tap models to include or leave out. Dimmed models are not available on this Mac; hover one to see why. Keep at least two.',
      },
      {
        title: 'Protect good earnings',
        text: 'Protect earnings above is the pace Bloomkeeper guards (default $0.20/hour). While the current model pays at least that, Bloomkeeper won’t interrupt it to learn. A clearly better model can still take over.',
      },
      {
        title: 'Three numbers, three jobs',
        text: 'Protect earnings above (default $0.20/hour) is the only one that holds a model in place: above it, Bloomkeeper won’t interrupt to learn. The earnings goal (default $0.12/hour) is for the Target report only and never changes what runs. The switch gain under Fine-tune (Balanced: 20% better and at least $0.02 more over the next hour, after a confirmation wait) is how much better another model must look before Bloomkeeper moves to it.',
      },
      {
        title: 'Give it time to learn',
        text: 'Learning time is how long a day Bloomkeeper may spend measuring other models while pace is below your protect level (default 1 hour), so it knows where to go when the current model fades. It only measures models with real demand.',
      },
      {
        title: 'Pick a style',
        text: 'How actively Bloomkeeper switches runs from Very passive to Very aggressive and sets trial length, waits and daily limits together.',
      },
      {
        title: 'Save',
        text: 'Changes show a Save changes bar. Saving while on applies right away; the current model keeps serving.',
      },
      {
        title: 'Turn it on',
        text: 'Choose Optimizer on. The first time, choose Turn on with 3-day boost (recommended for a Mac new to Bloomkeeper) or Turn on without boost. Bloomkeeper first confirms this Mac appears in Darkbloom’s provider list and the model is warm; this can take a few minutes.',
      },
    ],
    note: 'Every switch still has to pass memory, temperature, power and daily-limit checks. Fine-tune limits holds each value if you want to type your own, plus the earnings target used by Earnings → Target. What Bloomkeeper knows about each model shows what it has measured so far.',
  },
  {
    id: 'gathering',
    title: 'Learn a new Mac faster',
    summary: 'Learning boost measures more models for a set time.',
    steps: [
      {
        text: 'With the optimizer on, choose 24 hours, 3 days or 7 days under Learning boost.',
      },
      {
        text: 'Bloomkeeper learns for at least 3 hours a day and doesn’t wait for low earnings to start a learning run.',
      },
      {
        text: 'It ends by itself. Choose Stop to end it early. Your saved limits are unchanged afterwards.',
      },
    ],
    note: 'Use it when a Mac is new to Bloomkeeper. With Darkbloom 0.9.9 or later, a switch lets accepted requests finish first; older versions can interrupt them. A model earning above your protect level is never pulled away to learn.',
  },
  {
    id: 'earnings',
    title: 'Read your earnings',
    summary: 'What the numbers mean and which ones are confirmed.',
    steps: [
      {
        text: 'Earnings → Overview shows your confirmed balance and the live Pulse meter for the current session.',
      },
      {
        text: 'Confirmed credits come from your Darkbloom account. Estimates and forecasts are labeled as such and never counted as money earned.',
      },
      {
        text: 'Earnings → Target shows how many hours met your hourly target.',
      },
    ],
  },
  {
    id: 'phone',
    title: 'Check in from your phone',
    summary: 'Private access through your own Tailscale account.',
    steps: [
      {
        text: 'Install Tailscale on the Mac and the phone, signed in to the same account.',
      },
      {
        text: 'On the Mac, open More → Phone access and choose Enable phone access.',
      },
      {
        text: 'Scan the QR code with your phone. Keep the Mac awake and online.',
      },
    ],
    note: 'My Macs combines up to ten Macs: combined pace, each Mac’s model and how busy it is. Add each Mac using its phone access address (More → My Macs has step-by-step setup).',
  },
  {
    id: 'reports',
    title: 'Report a problem',
    summary: 'One tap sends a short, private report.',
    steps: [
      {
        text: 'When something goes wrong, a card asks if you want to send a report. Choose Send report. Add a note if you like.',
      },
      {
        text: 'To stop being asked, tick Send these automatically from now on. You can turn it off in More → Help & feedback.',
      },
      {
        text: 'Reports contain the app version, Mac chip and memory size, and status codes. Never earnings, account details, model names or logs.',
      },
    ],
  },
];

export const guideProblems: GuideProblem[] = [
  {
    id: 'login',
    symptom: 'No Darkbloom login found, or Login expired',
    cause:
      'Bloomkeeper reads earnings with the login Darkbloom saves on this Mac, and it is missing or expired.',
    fixes: [
      'Sign in to Darkbloom on this Mac (the Darkbloom app, or darkbloom login in Terminal).',
      'Wait about a minute; Bloomkeeper retries on its own.',
    ],
  },
  {
    id: 'roster',
    symptom:
      'Getting ready: waiting for a fresh match between this Mac and the provider roster',
    cause:
      'Before switching models, Bloomkeeper confirms this Mac appears in Darkbloom’s public provider list. Right after a restart, or while Darkbloom reconnects, the Mac can be missing for a few minutes.',
    fixes: [
      'Make sure Darkbloom is running and online: run darkbloom status in Terminal.',
      'Wait a few minutes, then choose Try turning on again.',
      'If it keeps timing out, choose Manual, restart the current model, wait until it shows Warm and ready, then turn the optimizer on again.',
    ],
  },
  {
    id: 'battery',
    symptom: 'Model switching waits while the Mac is on battery power',
    cause:
      'Bloomkeeper only switches models on AC power, because loading a model is heavy work.',
    fixes: ['Plug in the Mac. Waiting switches continue on their own.'],
  },
  {
    id: 'memory',
    symptom: 'A large model won’t load, or needs more memory',
    cause:
      'macOS keeps the last model’s files in its file cache, and Darkbloom counts that memory as in use.',
    fixes: [
      'Give Bloomkeeper permission to clear the file cache: in Manual, pick the large model; when Cache cleanup before loading appears, choose Enable cache cleanup and approve with your Mac password. It allows only the purge command, with no options.',
      'With permission, Bloomkeeper clears the cache before every switch, and the optimizer counts that memory when it checks whether a larger model fits.',
      'Close memory-heavy apps if it still says more memory is needed.',
    ],
  },
  {
    id: 'hot',
    symptom: 'Switching waits because the Mac is hot',
    cause:
      'Bloomkeeper pauses switches while temperatures are high to avoid throttling.',
    fixes: [
      'Leave it be; it resumes when the Mac cools. Make sure vents are not blocked.',
    ],
  },
  {
    id: 'stale',
    symptom: 'Numbers say stale or reconnecting after sleep',
    cause:
      'Nothing is collected while the Mac sleeps. Bloomkeeper labels older readings instead of showing them as new.',
    fixes: [
      'Wait a minute after waking for fresh readings.',
      'To avoid gaps, keep the Mac awake while it serves.',
    ],
  },
  {
    id: 'phone',
    symptom: 'The phone page won’t load',
    cause:
      'The phone reaches your Mac only through Tailscale, and only while the Mac is awake.',
    fixes: [
      'Check Tailscale is connected on both devices with the same account.',
      'Open More → Phone access on the Mac and confirm it is enabled.',
      'Keep the Mac awake and online.',
    ],
  },
  {
    id: 'other',
    symptom: 'Something else',
    cause: 'Anything not listed here.',
    fixes: [
      'Send a report from More → Help & feedback, or email support@bloomkeeper.io.',
    ],
  },
];
