import type { Copy } from './types'

/**
 * English — the canonical dictionary.
 *
 * Written to sell, not to document. Two rules held throughout:
 *
 * 1. Every claim maps to something that exists. Agents, browsing, files,
 *    the terminal, approval gates, the 5-hour renewing window, per-plan
 *    intelligence tiers and project/agent limits are all real and read from
 *    packages/core/src/entitlements.py and the router's PLAN_LIMITS.
 * 2. The page says what the product gives you, never what it protects you
 *    from. "You stay in control" instead of "autonomy is a risk" — same
 *    fact, and only one of the two is worth reading before you have bought
 *    anything.
 */
export const en: Copy = {
  meta: {
    title: 'TRINDATRON — Don’t just ask AI. Put it to work.',
    description:
      'Most AI answers. TRINDATRON works: it takes the objective, coordinates multiple ' +
      'intelligences, uses a browser, files and a desktop of its own, and brings back the ' +
      'finished job.',
    ogLocale: 'en_US',
    htmlLang: 'en',
  },

  nav: {
    product: 'Product',
    work: 'Work',
    agents: 'Agents',
    reach: 'Computer use',
    intelligence: 'Intelligence',
    pricing: 'Pricing',
    signIn: 'Sign in',
    startFree: 'Start free',
    openMenu: 'Open menu',
    closeMenu: 'Close menu',
    language: 'Language',
    languageHint: 'Choose your language',
    backToApp: 'Open workspace',
  },

  hero: {
    kicker: 'One workspace. Many intelligences.',
    titleA: 'Don’t just ask AI.',
    titleB: 'Put it to work.',
    body:
      'Hand over the objective. TRINDATRON decides how to reach it, puts the right intelligence ' +
      'on each step, and uses real tools — the web, files, a desktop of its own — until the job ' +
      'is done.',
    primary: 'Start free',
    secondary: 'Watch it work',
    note: 'Free plan to begin. No card required.',
  },

  inAction: {
    eyebrow: 'In action',
    title: 'Ask for an outcome. Watch it happen.',
    body:
      'One instruction, and the work runs in front of you — read, planned, carried out, checked. ' +
      'You see every step as it lands.',
    request: 'Compare how our three closest competitors price their plans, and put it in a table.',
    steps: [
      { label: 'Reads the request', detail: 'Works out the goal and what a finished answer looks like.' },
      { label: 'Chooses the intelligence', detail: 'Matches the task to the one best suited to it.' },
      { label: 'Gathers the facts', detail: 'Opens the pages, reads them, collects the figures.' },
      { label: 'Builds the answer', detail: 'Writes the comparison and confirms the numbers line up.' },
      { label: 'Delivers', detail: 'Hands back the table, with every source it used.' },
    ],
    resultLabel: 'Delivered',
    resultBody: 'Pricing comparison — three competitors, plan by plan.',
    resultMeta: 'Table · 5 sources · ready to use',
  },

  newWay: {
    eyebrow: 'A different kind of AI',
    title: 'There is more to AI than a chat box.',
    body:
      'The assistants everyone knows are built to reply. TRINDATRON is built to finish things — ' +
      'a workspace where the objective goes in and completed work comes out.',
    fromLabel: 'What you are used to',
    toLabel: 'What TRINDATRON does',
    rows: [
      { from: 'You get an answer', to: 'You get the finished work' },
      { from: 'You pick the model', to: 'The right intelligence is chosen for every step' },
      { from: 'You copy, paste and redo it yourself', to: 'It uses the browser, files and a desktop for you' },
      { from: 'Every conversation starts over', to: 'Projects and memory carry your context forward' },
      { from: 'One model, one opinion', to: 'Many intelligences, one result' },
    ],
  },

  intelligences: {
    eyebrow: 'Intelligence',
    title: 'Many intelligences. One result.',
    body:
      'You talk to one workspace. Behind it, each step goes to whichever intelligence handles it ' +
      'best — and as stronger ones arrive they join the work, without changing a thing on your side.',
    points: [
      {
        title: 'The right one for the job',
        body: 'A quick answer and a long analysis are different kinds of work. Each goes where it does best.',
      },
      {
        title: 'Always current',
        body: 'New intelligences join the workspace as they arrive. Your side of it stays exactly the same.',
      },
      {
        title: 'Nothing to choose',
        body: 'No model picker, no settings to keep up with. You describe the outcome; the choice is handled.',
      },
    ],
    caption: 'Every request is matched to the intelligence that fits it.',
    youLabel: 'You',
    coreLabel: 'TRINDATRON',
    fieldLabel: 'Available intelligences',
  },

  work: {
    eyebrow: 'Work',
    title: 'Delegate the task. Keep the last word.',
    body:
      'Real work is more than one answer. TRINDATRON carries an objective all the way to a ' +
      'result, in the open — and stops for you at the moments that matter.',
    steps: [
      { label: 'Understand', detail: 'Turns your objective into what actually has to be true at the end.' },
      { label: 'Plan', detail: 'Breaks it into steps and decides the order to take them in.' },
      { label: 'Execute', detail: 'Does the work with real tools — the web, files, the terminal.' },
      { label: 'Check', detail: 'Reviews the result against the objective before calling it done.' },
      { label: 'Deliver', detail: 'Brings back the finished work and how it got there.' },
    ],
    controlTitle: 'You stay in control',
    controlBody:
      'Reading is automatic. Acting asks first: before it writes a file, runs a command or ' +
      'submits anything on a page, the work pauses and waits for your approval.',
    approve: 'Approve',
    review: 'Review',
  },

  agents: {
    eyebrow: 'Agents',
    title: 'Give direction. Let it carry the work.',
    body:
      'Build an agent for the work that comes back every week and give it the objective instead ' +
      'of the instructions. It plans, works and reports back.',
    points: [
      {
        title: 'Delegate an outcome',
        body: 'Describe what a good result looks like. Working out the steps is the agent’s job.',
      },
      {
        title: 'Works while you do not',
        body: 'Scheduled agents wake up on their own, check the situation and act only when there is something to do.',
      },
      {
        title: 'Reports back',
        body: 'Every run leaves a trail you can read: what it did, which tools it used, what came out.',
      },
    ],
  },

  reach: {
    eyebrow: 'Computer use',
    title: 'AI that works beyond the chat.',
    body:
      'TRINDATRON has a computer of its own — a browser it really browses, files it really ' +
      'writes, a terminal it really runs, and a desktop it can see and operate. Every action that ' +
      'changes something waits for your approval first.',
    liveLabel: 'Working today',
    nextLabel: 'Rolling out',
    live: [
      {
        title: 'The web, first hand',
        body: 'Opens real pages, reads them, clicks through and fills forms to reach what it needs.',
      },
      {
        title: 'Files and terminal',
        body: 'Reads and writes files and runs commands inside an isolated workspace of its own.',
      },
      {
        title: 'Screens it can see',
        body: 'Captures what a page actually looks like, so the result is checked against the real thing.',
      },
    ],
    next: [
      {
        title: 'A desktop it can operate',
        body:
          'Its own screen, mouse and keyboard, inside a sealed environment — built, approval-gated ' +
          'and under test, reaching accounts as the capacity for it rolls out.',
      },
      {
        title: 'The tools you already use',
        body: 'Bringing the same reach to the applications your work already lives in.',
      },
    ],
  },

  memory: {
    eyebrow: 'Continuity',
    title: 'It remembers the work.',
    body:
      'Every conversation builds on the last one. Projects hold their own context, and what you ' +
      'established weeks ago is still there when you come back.',
    points: [
      { title: 'Projects', body: 'Work grouped the way you think about it, each with its own context and history.' },
      { title: 'Memory', body: 'What you have told it once stays known — preferences, decisions, the shape of your work.' },
      { title: 'Continuity', body: 'Pick up where you left off, days later, without setting the scene again.' },
    ],
  },

  why: {
    eyebrow: 'Why TRINDATRON',
    title: 'Built for work that has to land.',
    body: 'What makes a result worth relying on, built into the way the work happens.',
    points: [
      { title: 'The right intelligence for the job', body: 'Every step is matched to what handles it best.' },
      { title: 'Work keeps moving', body: 'When one path is busy, the work continues down another.' },
      { title: 'Your workspace stays yours', body: 'Sessions, projects and files are isolated per account and checked on every request.' },
      { title: 'You approve what matters', body: 'Writing, running and submitting all wait for you.' },
      { title: 'A controlled environment', body: 'Work runs inside an isolated workspace with restricted network access.' },
      { title: 'See how it was done', body: 'Each step names the tool it used, so the result can be followed back.' },
    ],
  },

  pricing: {
    eyebrow: 'Pricing',
    title: 'Start free. Move up when it pays for itself.',
    body:
      'You pay for the work you get out of it, not per model. Change plan or cancel whenever ' +
      'you like.',
    perMonth: '/month',
    free: 'Free',
    recommended: 'Most popular',
    plans: {
      free: {
        tagline: 'Explore',
        volume: 'A look at the workspace',
        features: [
          { label: 'Conversation, search and memory', included: true },
          { label: 'Up to 3 projects', included: true },
          { label: 'Essential intelligence tier', included: true },
          { label: 'Agents that carry out work', included: false },
          { label: 'Browser, files and terminal', included: false },
        ],
        cta: 'Start free',
      },
      start: {
        tagline: 'Build momentum',
        volume: 'Everyday work',
        features: [
          { label: 'Agents that carry out work', included: true },
          { label: 'Browser, files and terminal', included: true },
          { label: 'Advanced intelligence tier', included: true },
          { label: 'Up to 10 projects and 5 agents', included: true },
          { label: 'Sessions that renew every 5 hours', included: true },
        ],
        cta: 'Choose START',
      },
      pro: {
        tagline: 'Work professionally',
        volume: 'Professional, all day',
        features: [
          { label: 'Everything in START', included: true },
          { label: 'Approval-gated execution (WORK)', included: true },
          { label: 'Up to 50 projects and 20 agents', included: true },
          { label: 'Far more room to execute', included: true },
          { label: 'Sessions that renew every 5 hours', included: true },
        ],
        cta: 'Choose PRO',
      },
      max: {
        tagline: 'Push further',
        volume: 'Intensive, without watching the meter',
        features: [
          { label: 'Everything in PRO', included: true },
          { label: 'Premium intelligences', included: true },
          { label: 'Longest tasks per request', included: true },
          { label: 'Projects and agents without a fixed cap', included: true },
          { label: 'Sessions that renew every 5 hours', included: true },
        ],
        cta: 'Choose MAX',
      },
      team: {
        tagline: 'Scale together',
        volume: 'Intensive, shared across the team',
        features: [
          { label: 'Everything in MAX, for the team', included: true },
          { label: 'One shared pool of usage', included: true },
          { label: 'Central workspace', included: true },
          { label: 'Dedicated onboarding', included: true },
          { label: 'Sessions that renew every 5 hours', included: true },
        ],
        cta: 'Choose TEAM',
      },
    },
    windowNote:
      'Paid plans work in five-hour sessions that renew on their own, with daily and weekly ' +
      'allowances behind them. You are told when a window is close and exactly when it comes back.',
    currencyNote: 'Prices in US dollars.',
  },

  closing: {
    title: 'Stop asking. Start finishing.',
    body:
      'Bring one real task — the one you were about to do yourself — and watch what comes back. ' +
      'It takes less time than choosing a model.',
    primary: 'Start free',
    secondary: 'Sign in',
  },

  footer: {
    tagline: 'The AI workspace that does the work, not just the talking.',
    product: 'Product',
    company: 'Company',
    signIn: 'Sign in',
    rights: 'All rights reserved.',
  },
}
