export const meta = {
  name: 'creator-agent-v1-roadmap',
  description: 'Dependency-gated Luna workflow for the remaining Creator Agent roadmap. Slice PRs target the integration branch; only the final autoship PR targets main.',
  phases: [
    { title: 'Shared contracts', detail: 'Land inert review, craft, workspace, and automation contracts' },
    { title: 'Wave 1', detail: 'Critic, day-vlog policy, and off-plan intake in parallel' },
    { title: 'Wave 2', detail: 'Single-hero policy, core craft, and workspace coordination in parallel' },
    { title: 'Wave 3', detail: 'Overlay craft and SFX/speech-cut craft in parallel' },
    { title: 'Wave 4', detail: 'One bounded automatic revision after critic and all craft receipts' },
    { title: 'Integration', detail: 'Docs, rollout, verification ledger, and autoship handoff' },
  ],
}

const INTEGRATION_BRANCH = 'codex/creator-agent-roadmap'
const LUNA = 'gpt-5.6-luna'

const RESULT_SCHEMA = {
  type: 'object',
  additionalProperties: false,
  required: ['lane', 'status', 'branch', 'summary', 'tests', 'human_verification', 'blockers'],
  properties: {
    lane: { type: 'string' },
    status: { type: 'string', enum: ['done', 'partial', 'blocked'] },
    branch: { type: 'string' },
    pr_url: { type: 'string' },
    summary: { type: 'string' },
    commits: { type: 'array', items: { type: 'string' } },
    files_changed: { type: 'array', items: { type: 'string' } },
    tests: {
      type: 'array',
      items: {
        type: 'object',
        additionalProperties: false,
        required: ['command', 'status'],
        properties: {
          command: { type: 'string' },
          status: { type: 'string', enum: ['passed', 'failed', 'not_run'] },
          note: { type: 'string' },
        },
      },
    },
    human_verification: { type: 'array', items: { type: 'string' } },
    taste_decisions: { type: 'array', items: { type: 'string' } },
    blockers: { type: 'array', items: { type: 'string' } },
  },
}

const RULES = [
  `Work only in the isolated worktree supplied by this workflow. Slice PRs target ${INTEGRATION_BRANCH}; never main.`,
  'Use model gpt-5.6-luna. At most three lanes may run concurrently.',
  'Do not merge, deploy, flip flags, publish, acquire external assets, modify VERSION/CHANGELOG, or infer creator preferences.',
  'All new behavior is default-off and preserves V1 behavior when disabled.',
  'Use opaque IDs and exact creator/plan/item/ownership-epoch/revision/Job/variant/generation pins. Reuse existing owner-scoped validators and render helpers.',
  'Command bundles are atomic and idempotent. A stale identity fails closed. Model output is inert until deterministic server validation.',
  'Prompt changes require prompt_version, replay fixtures/rubrics, structural evals, and a live-eval item in human_verification.',
  'Add focused schema, capability, ownership, stale-generation, idempotency, crash-resume, atomicity, and kill-switch tests appropriate to the slice.',
  'Commit only the slice. Push the branch and open a PR to the integration branch, but do not merge it.',
].join('\n')

const LANES = {
  shared: {
    branch: 'codex/creator-agent-shared-contracts',
    deps: [],
    prompt: 'Define inert bounded review, craft-command, workspace-proposal, preference-signal, and automation-decision contracts plus default-off settings. Do not activate behavior.',
  },
  critic: {
    branch: 'codex/creator-agent-stage2-critic',
    deps: ['shared'],
    prompt: 'Implement Stage 2 exact-generation review. Reuse VideoQualityGrader, persist timestamped evidence and one inert revision proposal through last_review and AgentRun, expose bounded review response, enqueue exactly once for the ready Job/variant/generation, and fail open to manual feedback. MAIN_CREATOR_AGENT_QUALITY_REVIEW_ENABLED is a child of the existing review flag.',
  },
  dayVlog: {
    branch: 'codex/creator-agent-day-vlog',
    deps: ['shared'],
    prompt: 'Implement EDIT_FORMAT_DAY_VLOG_ENABLED and a strict guided-renderer day_vlog policy: chronological, filming-guide-aware sequence, bounded transitions/duration, insufficient-media failure, mixed-version worker defense, and no montage downgrade.',
  },
  intake: {
    branch: 'codex/creator-agent-off-plan-intake',
    deps: ['shared'],
    prompt: 'Implement authenticated plan-scoped create/poll/decide relevance proposal endpoints. Persist exact media, creator, plan ownership epoch, status, and idempotency. Approval may attach to an existing item, create a montage item, or reject. Nothing else happens before approval.',
  },
  singleHero: {
    branch: 'codex/creator-agent-single-hero',
    deps: ['dayVlog'],
    prompt: 'Build on the landed guided-policy infrastructure. Implement EDIT_FORMAT_SINGLE_HERO_ENABLED: one dominant owned clip, supporting cutaways, explicit duration policy, insufficient-media failure, mixed-version defense, and no montage downgrade.',
  },
  coreCraft: {
    branch: 'codex/creator-agent-core-craft',
    deps: ['shared'],
    prompt: 'Implement atomic idempotent execution of caption-style, transition, and look commands. Resolve through live capabilities and existing owner-scoped validators/render helpers. Preserve treatment flags and exact generation/revision fences.',
  },
  workspace: {
    branch: 'codex/creator-agent-workspace',
    deps: ['intake'],
    prompt: 'Implement a plan-level workspace receipt coordinating child PlanItems while retaining one Creator session, Job, ownership epoch, and generation receipt per deliverable. Preference signals are explicit creator feedback only; exclude inferred learning, training enrollment, and cross-item media reuse.',
  },
  overlayCraft: {
    branch: 'codex/creator-agent-overlay-craft',
    deps: ['coreCraft'],
    prompt: 'Implement atomic media-overlay command execution using existing upload/catalog ownership validation, overlay flags, apply helpers, and exact-generation receipts. Add treatment preview response/UI tests. Run overlay verification and local render or report them for human verification.',
  },
  audioCutCraft: {
    branch: 'codex/creator-agent-audio-cut-craft',
    deps: ['coreCraft'],
    prompt: 'Implement licensed-SFX and already-validated silence/retake candidate command execution using existing flags, catalog/owner validation, revision guards, atomic receipts, and render helpers. Do not accept arbitrary assets or raw time ranges.',
  },
  autonomy: {
    branch: 'codex/creator-agent-bounded-autonomy',
    deps: ['critic', 'coreCraft', 'overlayCraft', 'audioCutCraft'],
    prompt: 'Implement explicit session opt-in plus deterministic one-cycle policy: confidence >= .85, current quality < 4/5, expected improvement >= .5, objective review, and render budget. Allow only corrective transition fallback, caption legibility, removal of failing optional overlay/SFX, or validated speech-cut candidates. Preserve previous ready generation as rollback. Never change media, audio strategy, voiceover, visible copy, publishing, or external assets.',
  },
  integration: {
    branch: 'codex/creator-agent-integration-docs',
    deps: ['singleHero', 'workspace', 'autonomy'],
    prompt: 'Reconcile docs/pipelines/creator-agent.md, rollout/runbook instructions, flags, response contracts, frontend state, and acceptance-test ledger with the integrated implementation. Record local-render, overlay, live-eval, browser-QA, and production canary items honestly. Do not change release metadata or deploy.',
  },
}

function blockersFor(lane, results) {
  const byLane = new Map(results.map((result) => [result.lane, result]))
  return lane.deps.flatMap((dep) => {
    const result = byLane.get(dep)
    if (!result) return [`${dep} has no result`]
    if (result.status !== 'done') return [`${dep} is ${result.status}`]
    return []
  })
}

async function runLane(name, results) {
  const lane = LANES[name]
  const blockers = blockersFor(lane, results)
  if (blockers.length) {
    return { lane: name, status: 'blocked', branch: lane.branch, summary: 'Dependency gate prevented dispatch.', tests: [], human_verification: [], blockers }
  }
  return agent([
    RULES,
    '',
    `LANE: ${name}`,
    `SLICE BRANCH: ${lane.branch}`,
    `PR TARGET: ${INTEGRATION_BRANCH}`,
    `DEPENDENCIES: ${lane.deps.join(', ') || 'none'}`,
    '',
    lane.prompt,
    '',
    'Before editing, verify all dependency symbols exist on the integration base. Return blocked instead of duplicating a missing dependency. Return done only after focused tests pass and the slice PR is open.',
  ].join('\n'), {
    label: lane.branch,
    phase: name,
    isolation: 'worktree',
    model: LUNA,
    schema: RESULT_SCHEMA,
  })
}

const results = []

phase('Shared contracts')
results.push(await runLane('shared', results))

phase('Wave 1')
results.push(...await parallel([
  () => runLane('critic', results),
  () => runLane('dayVlog', results),
  () => runLane('intake', results),
]))

phase('Wave 2')
results.push(...await parallel([
  () => runLane('singleHero', results),
  () => runLane('coreCraft', results),
  () => runLane('workspace', results),
]))

phase('Wave 3')
results.push(...await parallel([
  () => runLane('overlayCraft', results),
  () => runLane('audioCutCraft', results),
]))

phase('Wave 4')
results.push(await runLane('autonomy', results))

phase('Integration')
results.push(await runLane('integration', results))

const humanVerification = results.flatMap((result) => result.human_verification.map((item) => `[${result.lane}] ${item}`))
const tasteDecisions = results.flatMap((result) => (result.taste_decisions || []).map((item) => `[${result.lane}] ${item}`))
const blockers = results.flatMap((result) => result.blockers.map((item) => `[${result.lane}] ${item}`))

return {
  integration_branch: INTEGRATION_BRANCH,
  results,
  human_verification: humanVerification,
  taste_decisions: tasteDecisions,
  blockers,
  autoship_gate: 'Root must run full backend/frontend checks, local renders, overlay verification, /browse QA, preship, review, release bump/changelog, and open one final PR to main. Stop for mandatory batch approval before landing.',
}
