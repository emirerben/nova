export const meta = {
  name: 'decompose',
  description: 'Decompose a big task into independent subtasks, fan them out across subagents, and synthesize one report. Use for batchable work (per-file transforms, audits, migrations, multi-variant changes) instead of doing items one at a time.',
  phases: [
    { title: 'Plan', detail: 'split the task into independent subtasks (skipped if subtasks are passed in)' },
    { title: 'Execute', detail: 'one subagent per subtask, fanned out via pipeline' },
    { title: 'Synthesize', detail: 'collate all subtask results into one report' },
  ],
}

// args shape:
//   { subtasks: [{ title, prompt, files? }], mutates?: bool, synthesize?: bool }
//     -> skip planning, fan out the given subtasks directly
//   { task: "<one big task description>", maxSubtasks?: number, mutates?: bool, synthesize?: bool }
//     -> a planner agent decomposes the task first, then fan out
//
//   mutates    (default false): subtasks edit files -> run each in an isolated worktree
//   synthesize (default true):  run a final agent to collate results into one report

const PLAN_SCHEMA = {
  type: 'object',
  additionalProperties: false,
  required: ['subtasks'],
  properties: {
    subtasks: {
      type: 'array',
      items: {
        type: 'object',
        additionalProperties: false,
        required: ['title', 'prompt'],
        properties: {
          title: { type: 'string', description: 'short label for this subtask' },
          prompt: { type: 'string', description: 'self-contained instruction for one subagent — assume no shared context' },
          files: { type: 'array', items: { type: 'string' }, description: 'likely files to touch (optional)' },
        },
      },
    },
  },
}

const RESULT_SCHEMA = {
  type: 'object',
  additionalProperties: false,
  required: ['title', 'status', 'summary'],
  properties: {
    title: { type: 'string' },
    status: { type: 'string', enum: ['done', 'partial', 'blocked'] },
    summary: { type: 'string', description: 'what was done / found, 1-4 sentences' },
    files_changed: { type: 'array', items: { type: 'string' } },
    follow_ups: { type: 'array', items: { type: 'string' } },
  },
}

const a = args || {}
const mutates = a.mutates === true
const synthesize = a.synthesize !== false

// --- Phase: Plan -----------------------------------------------------------
let subtasks = Array.isArray(a.subtasks) ? a.subtasks : null

if (!subtasks) {
  if (!a.task || typeof a.task !== 'string') {
    throw new Error('decompose: pass either args.subtasks (array) or args.task (string).')
  }
  phase('Plan')
  const cap = typeof a.maxSubtasks === 'number' ? a.maxSubtasks : 8
  const plan = await agent(
    `Decompose this task into at most ${cap} INDEPENDENT subtasks that can each run in a separate subagent with no shared context. ` +
      `Each subtask prompt must be fully self-contained (name files, the goal, and how to verify). ` +
      `Do not create subtasks that depend on each other's output.\n\nTASK:\n${a.task}`,
    { label: 'plan:decompose', phase: 'Plan', schema: PLAN_SCHEMA },
  )
  subtasks = plan.subtasks
  log(`Planned ${subtasks.length} subtask(s)`)
}

if (!subtasks.length) {
  return { subtasks: [], results: [], report: 'No subtasks to run.' }
}

// --- Phase: Execute --------------------------------------------------------
phase('Execute')
const results = await pipeline(
  subtasks,
  (st, _orig, i) => {
    const filesHint = st.files && st.files.length ? `\n\nLikely files: ${st.files.join(', ')}` : ''
    return agent(`${st.prompt}${filesHint}`, {
      label: `exec:${st.title || 'subtask-' + (i + 1)}`,
      phase: 'Execute',
      schema: RESULT_SCHEMA,
      ...(mutates ? { isolation: 'worktree' } : {}),
    }).then((r) => ({ ...r, title: r.title || st.title || `subtask-${i + 1}` }))
  },
)

const done = results.filter(Boolean)
log(`${done.length}/${subtasks.length} subtask(s) returned`)

// --- Phase: Synthesize -----------------------------------------------------
let report = null
if (synthesize && done.length) {
  phase('Synthesize')
  const bundle = done
    .map((r, i) => `### ${i + 1}. ${r.title} [${r.status}]\n${r.summary}` +
      (r.files_changed && r.files_changed.length ? `\nFiles: ${r.files_changed.join(', ')}` : '') +
      (r.follow_ups && r.follow_ups.length ? `\nFollow-ups: ${r.follow_ups.join('; ')}` : ''))
    .join('\n\n')
  report = await agent(
    `These are the results of ${done.length} subtasks run in parallel. Write a single tight report for the user: ` +
      `what got done, anything blocked or partial, and the consolidated follow-ups. Do not pad.\n\n${bundle}`,
    { label: 'synthesize', phase: 'Synthesize' },
  )
}

return { subtasks, results: done, report }
