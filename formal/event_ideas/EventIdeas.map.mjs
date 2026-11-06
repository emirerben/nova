// Chart labels for EventIdeas.

export const readerActions = ["OpenFinder", "ToggleResearch", "FindIdeas", "AnotherIdea"];

const WORK = {
	claiming: "checking saved answers",
	asking: "AI is writing ideas",
	reviewing: "fact checker reviewing ideas",
	filtering: "dropping repeats",
	retrying: "asking the AI once more",
};

const NOTICE = {
	none: "",
	problem: "“Kria hit a problem”",
	noOther: "“No other idea found”",
	noneYet: "“No idea found yet”",
};

function count(v) {
	return v.shown === 0 ? "no ideas" : v.shown === 1 ? "some ideas" : "more ideas";
}

export function state(v) {
	const context = v.research ? "Research on" : "Research off";
	if (v.screen === "working") {
		const what = v.kind === "another" ? "Another idea" : "Find ideas";
		return `${context}: ${what}, ${WORK[v.req]}`;
	}
	const base =
		v.screen === "finder" ? "direction panel open"
		: v.screen === "ideas" ? `${count(v)} on screen`
		: "no ideas yet";
	const n = NOTICE[v.notice];
	return `${context}: ${base}${n ? `, ${n}` : ""}`;
}

export function group(v) {
	if (v.screen === "working") return "Working";
	if (v.notice === "problem") return "Error shown";
	if (v.notice === "noOther" || v.notice === "noneYet") return "Nothing new shown";
	return v.shown > 0 ? "Ideas shown" : "Before ideas";
}
export const groupColors = {
	Working: "#fdf2d0",
	"Error shown": "#f8d7da",
	"Nothing new shown": "#e2e3e5",
	"Ideas shown": "#d1f2eb",
	"Before ideas": "#ffffff",
};

const STEPS = {
	OpenFinder: "tap Find ideas",
	ToggleResearch: "toggle Research",
	FindIdeas: "tap Find ideas from N ready",
	AnotherIdea: "tap Another idea",
	CachedAnswer: "same request as last time, saved answer reused",
	AskModel: "no saved answer, AI asked",
	ModelAnswers: "AI answers",
	ModelFails: "AI times out or every idea is malformed",
	ReviewApproves: "fact checker approves",
	ReviewRejects: "fact checker flags one idea",
	KeepIdeas: "new ideas appear",
	RetryOnce: "every idea was a repeat",
	RetryAnswers: "AI answers again",
	NothingNew: "nothing new survives",
};
export function step(action, before, after) {
	if (action === "ModelAnswers") {
		const o = { good: "with usable ideas", allFiltered: "with only repeats", empty: "with nothing" }[after.out];
		return `AI answers ${o}`;
	}
	if (action === "RetryAnswers") return after.out === "good" ? "AI answers again with a new idea" : "AI answers again with nothing";
	return STEPS[action] ?? action;
}

export function cause(v, facts, invariant) {
	if (invariant === "UsableIdeaNeverShownAsProblem")
		return "with Research on, the fact checker rejects the whole batch when it flags one idea, so good ideas are thrown away and the request fails";
	if (invariant === "NoIdeaYetMeansAsked")
		return "an empty first answer is saved, so tapping Find ideas again with the same direction replays it without asking the AI";
	return null;
}

export const legend = { works: "Works on the PR branch", wrong: "Breaks a rule" };
