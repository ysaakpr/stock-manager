export const meta = {
  name: 'build-wave',
  description: 'Run autonomous build waves over TASK_GRAPH.yaml until nothing is ready or a human decision blocks everything',
  whenToUse: 'In-session equivalent of `./orch run`. Use when you want the build driven from this conversation with live progress instead of a detached process.',
  phases: [
    { title: 'Plan', detail: 'read the ready set from ./orch' },
    { title: 'Build', detail: 'one fresh builder agent per ready task' },
    { title: 'Report', detail: 'what got built, what is blocked, and why' },
  ],
}

// Each wave is a genuine dependency barrier: nothing in wave N+1 can start until its
// deps are DONE, so parallel() (a barrier) is the right primitive here, not pipeline().
const MAX_WAVES = (args && args.maxWaves) || 12

const READY = {
  type: 'object',
  properties: {
    tasks: {
      type: 'array',
      items: {
        type: 'object',
        properties: { id: { type: 'string' }, title: { type: 'string' } },
        required: ['id', 'title'],
      },
    },
    parked: { type: 'array', items: { type: 'string' } },
  },
  required: ['tasks'],
}

const OUTCOME = {
  type: 'object',
  properties: {
    id: { type: 'string' },
    state: { type: 'string', enum: ['DONE', 'FAILED', 'PARKED', 'SPLIT', 'UNKNOWN'] },
    summary: { type: 'string' },
  },
  required: ['id', 'state', 'summary'],
}

const done = []
const blocked = []
let waves = 0

for (let wave = 1; wave <= MAX_WAVES; wave++) {
  phase('Plan')
  const plan = await agent(
    `Run \`./orch ready --json\` in the repo root and \`./orch status\`.
Return exactly the ready tasks as {tasks:[{id,title}]} plus {parked:[task ids currently PARKED]}.
Do not build anything. Do not interpret or filter the list — return it verbatim.`,
    { label: `plan:wave-${wave}`, phase: 'Plan', schema: READY }
  )

  if (!plan || !plan.tasks || plan.tasks.length === 0) {
    log(`wave ${wave}: nothing ready — stopping`)
    if (plan && plan.parked) blocked.push(...plan.parked)
    break
  }

  waves = wave
  log(`wave ${wave}: ${plan.tasks.length} task(s) — ${plan.tasks.map((t) => t.id).join(', ')}`)

  phase('Build')
  const outcomes = await parallel(
    plan.tasks.map((task) => () =>
      agent(
        `First claim the task so no other wave picks it up: \`./orch set ${task.id} IN_PROGRESS\`.
Then run \`./orch prompt ${task.id}\` in the repo root and follow the brief it prints, exactly.
That brief is your complete instruction set — it names the files to read, the acceptance criteria,
and the three legal ways to finish. Record your outcome with ./orch before you stop.

Then return {id:"${task.id}", state:<the state you recorded>, summary:<one line>}.`,
        { label: task.id, phase: 'Build', schema: OUTCOME }
      )
    )
  )

  for (const o of outcomes.filter(Boolean)) {
    if (o.state === 'DONE' || o.state === 'SPLIT') done.push(o)
    else blocked.push(`${o.id} (${o.state}): ${o.summary}`)
  }
}

phase('Report')
const report = await agent(
  `Run \`./orch status\` and read HUMAN_DECISIONS.md.
Write a factual handoff for the owner: how many tasks are DONE per milestone, what is PARKED and the
exact decision each one needs, and what failed with the real reason. Do not soften failures and do not
speculate about what will happen next. Return the handoff as plain markdown text.`,
  { label: 'handoff', phase: 'Report' }
)

return { waves, completed: done.length, blocked, report }
