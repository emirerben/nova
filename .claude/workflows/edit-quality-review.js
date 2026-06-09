export const meta = {
  name: 'edit-quality-review',
  description: 'Judge a rendered Nova edit: filming-guide sequence alignment + influencer-readiness',
  whenToUse: 'After rendering a generative/plan edit locally, to grade it before shipping pipeline changes. args: {videoPath, filmingGuide: [{what, how, duration_s}], clipColorKey?: string, framesDir?: string}',
  phases: [
    { title: 'Probe', detail: 'extract frames + probe timing' },
    { title: 'Judge', detail: 'parallel judges: sequence, hook, pacing, readiness' },
    { title: 'Verify', detail: 'adversarial check on failed dimensions' },
  ],
}

const videoPath = args?.videoPath
if (!videoPath) throw new Error('args.videoPath required')
const guide = args?.filmingGuide || []
const colorKey = args?.clipColorKey || ''

phase('Probe')
const probe = await agent(
  `You are preparing evidence for video-edit judges. Video: ${videoPath}\n` +
  `1. ffprobe duration, resolution, fps.\n` +
  `2. Extract 1 frame per second to a temp dir (ffmpeg -vf fps=1, scale to 270x480 PNGs).\n` +
  `3. Read every frame image in order. For each second, describe in one line what is on screen ` +
  `(dominant colors, visible text, scene content, any overlay text verbatim).\n` +
  `Return JSON-ish plain text: duration, fps, then "t=Ns: <description>" lines for every second.`,
  { label: 'probe:frames', phase: 'Probe' }
)

phase('Judge')
const guideBlock = guide.length
  ? `FILMING GUIDE (intended shot order):\n${guide.map((s, i) => `  shot_${i}: ${s.what} (${s.how || 'any framing'}, ~${s.duration_s}s)`).join('\n')}\n${colorKey ? `CLIP→COLOR KEY (synthetic test): ${colorKey}\n` : ''}`
  : 'No filming guide provided — skip sequence judging, grade only quality dimensions.\n'

const VERDICT = {
  type: 'object',
  properties: {
    dimension: { type: 'string' },
    score: { type: 'number', description: '0-10' },
    pass: { type: 'boolean' },
    evidence: { type: 'array', items: { type: 'string' } },
    improvements: { type: 'array', items: { type: 'string' } },
  },
  required: ['dimension', 'score', 'pass', 'evidence', 'improvements'],
}

const DIMENSIONS = [
  {
    key: 'sequence_alignment',
    prompt:
      `Judge ONLY sequence alignment. Given the per-second frame log below and the filming guide, ` +
      `determine the order in which source clips appear in the edit and whether it matches the guide's shot order ` +
      `(shot_0 footage first, then shot_1, etc.; extra pool footage allowed only after/between later shots, never before shot_0). ` +
      `pass=true ONLY if the order is monotonic per the guide. Cite t=Ns evidence lines.`,
  },
  {
    key: 'hook_strength',
    prompt:
      `Judge ONLY the hook (first 3 seconds). Does the opening create a question in the viewer's mind? ` +
      `Is there immediate visual interest and (if text overlay exists) a compelling hook line? ` +
      `Established-influencer bar: a scroll-stopping first frame. pass requires score >= 7.`,
  },
  {
    key: 'pacing_flow',
    prompt:
      `Judge ONLY pacing and narrative flow. Do cuts land on a sensible rhythm (roughly beat-spaced, no slot ` +
      `shorter than ~1.5s or longer than ~8s unless intentional)? Does the sequence build (arc) rather than feel shuffled? ` +
      `pass requires score >= 7.`,
  },
  {
    key: 'influencer_readiness',
    prompt:
      `Judge OVERALL influencer-readiness: would an established short-form creator post this as-is? ` +
      `Consider cohesion, text overlay quality/placement, duration fit for the format, and whether the edit feels planned. ` +
      `Be harsh — "fine for AI" is a fail. pass requires score >= 7.`,
  },
]

const verdicts = await parallel(
  DIMENSIONS.map((d) => () =>
    agent(
      `${d.prompt}\n\n${guideBlock}\nVideo: ${videoPath}\nPER-SECOND FRAME LOG:\n${probe}\n\n` +
      `You may re-extract specific frames at full resolution with ffmpeg/Read if the log is ambiguous. ` +
      `Return dimension="${d.key}".`,
      { label: `judge:${d.key}`, phase: 'Judge', schema: VERDICT }
    )
  )
)

phase('Verify')
const failed = verdicts.filter(Boolean).filter((v) => !v.pass)
const verified = await parallel(
  failed.map((v) => () =>
    agent(
      `Adversarially verify this FAIL verdict on a rendered video edit. Try to REFUTE it: re-extract frames from ` +
      `${videoPath} yourself and check the cited evidence. Dimension: ${v.dimension}, score ${v.score}. ` +
      `Evidence: ${JSON.stringify(v.evidence)}. Improvements claimed: ${JSON.stringify(v.improvements)}.\n${guideBlock}\n` +
      `Return dimension="${v.dimension}", pass=true if the FAIL was wrong (the edit is actually fine on this dimension), ` +
      `pass=false if the FAIL stands, with corrected evidence.`,
      { label: `verify:${v.dimension}`, phase: 'Verify', schema: VERDICT }
    )
  )
)

const verifiedByDim = Object.fromEntries(verified.filter(Boolean).map((v) => [v.dimension, v]))
const final = verdicts.filter(Boolean).map((v) => {
  const override = verifiedByDim[v.dimension]
  return override && override.pass !== v.pass ? { ...override, overridden: true } : v
})

const overall = final.every((v) => v.pass)
log(`edit-quality-review: ${overall ? 'PASS' : 'FAIL'} — ${final.map((v) => `${v.dimension}=${v.score}${v.pass ? '✓' : '✗'}`).join(', ')}`)
return {
  overall_pass: overall,
  verdicts: final,
  improvements: final.filter((v) => !v.pass).flatMap((v) => v.improvements),
}
