// @ts-nocheck
import React from "react";
import { act, render, screen, waitFor } from "@testing-library/react";
import "@testing-library/jest-dom";

import PlanPage from "@/app/plan/page";
import { getContentPlan, getPersona } from "@/lib/plan-api";

jest.mock("next-auth/react", () => ({
  useSession: () => ({ status: "authenticated" }),
}));

jest.mock("next/navigation", () => ({
  useRouter: () => ({ replace: jest.fn() }),
  useSearchParams: () => ({ get: () => null }),
}));

jest.mock("@/lib/plan-api", () => ({
  getContentPlan: jest.fn(),
  getPersona: jest.fn(),
  createContentPlan: jest.fn(),
  retunePersonaFromFeedback: jest.fn(),
  tiktokScrape: jest.fn(),
  updatePersona: jest.fn(),
  recordOnboardingFork: jest.fn(),
  NotAuthenticatedError: class NotAuthenticatedError extends Error {},
}));

jest.mock("@/lib/generative-api", () => ({ createGenerativeJob: jest.fn() }));
jest.mock("@/app/plan/_lib/route", () => ({ resolvePlanMode: () => "workspace" }));
jest.mock("@/app/plan/_components/workspace/WorkspaceHome", () => ({
  WorkspaceHome: ({ plan, onRefresh, onPlanChange }) => (
    <div>
      <div data-testid="plan-status">{plan.plan_status}</div>
      <button type="button" onClick={() => void onRefresh()}>Refresh</button>
      <button
        type="button"
        onClick={() => onPlanChange({ id: "plan-1", plan_status: "generating", items: [] })}
      >
        Accept generation
      </button>
    </div>
  ),
}));

const mockGetContentPlan = getContentPlan as jest.MockedFunction<typeof getContentPlan>;
const mockGetPersona = getPersona as jest.MockedFunction<typeof getPersona>;

function deferred<T>() {
  let resolve: (value: T) => void = () => {};
  const promise = new Promise<T>((done) => {
    resolve = done;
  });
  return { promise, resolve };
}

function plan(plan_status: "generating" | "ready") {
  return { id: "plan-1", plan_status, items: [] };
}

describe("PlanPage polling", () => {
  beforeEach(() => {
    jest.useFakeTimers();
    mockGetPersona.mockReset().mockResolvedValue({ persona_status: "ready" } as never);
    mockGetContentPlan.mockReset();
  });

  afterEach(() => {
    jest.useRealTimers();
  });

  it("ignores an older refresh that resolves after a newer ready refresh", async () => {
    const slowRefresh = deferred<ReturnType<typeof plan>>();
    const fastRefresh = deferred<ReturnType<typeof plan>>();
    mockGetContentPlan
      .mockResolvedValueOnce(plan("ready") as never)
      .mockReturnValueOnce(slowRefresh.promise as never)
      .mockReturnValueOnce(fastRefresh.promise as never);

    render(<PlanPage />);
    expect(await screen.findByTestId("plan-status")).toHaveTextContent("ready");

    screen.getByRole("button", { name: "Refresh" }).click();
    await waitFor(() => expect(mockGetContentPlan).toHaveBeenCalledTimes(2));
    screen.getByRole("button", { name: "Refresh" }).click();
    await waitFor(() => expect(mockGetContentPlan).toHaveBeenCalledTimes(3));

    await act(async () => fastRefresh.resolve(plan("ready")));
    expect(screen.getByTestId("plan-status")).toHaveTextContent("ready");

    await act(async () => slowRefresh.resolve(plan("generating")));
    expect(screen.getByTestId("plan-status")).toHaveTextContent("ready");
  });

  it("lets a slow poll finish instead of superseding it every interval", async () => {
    const slowPoll = deferred<ReturnType<typeof plan>>();
    mockGetContentPlan
      .mockResolvedValueOnce(plan("generating") as never)
      .mockReturnValueOnce(slowPoll.promise as never);

    render(<PlanPage />);
    expect(await screen.findByTestId("plan-status")).toHaveTextContent("generating");

    await act(async () => {
      jest.advanceTimersByTime(6000);
    });
    expect(mockGetContentPlan).toHaveBeenCalledTimes(2);

    await act(async () => slowPoll.resolve(plan("ready")));
    expect(screen.getByTestId("plan-status")).toHaveTextContent("ready");
  });

  it("releases the polling gate when a request never settles", async () => {
    mockGetContentPlan
      .mockResolvedValueOnce(plan("generating") as never)
      .mockImplementation(() => new Promise(() => {}) as never);

    render(<PlanPage />);
    expect(await screen.findByTestId("plan-status")).toHaveTextContent("generating");

    await act(async () => {
      jest.advanceTimersByTime(12000);
    });
    expect(mockGetContentPlan).toHaveBeenCalledTimes(2);

    await act(async () => {
      jest.advanceTimersByTime(2000);
    });
    expect(mockGetContentPlan).toHaveBeenCalledTimes(3);
  });

  it("invalidates an older GET when the accepted POST hands off generating state", async () => {
    const staleRefresh = deferred<ReturnType<typeof plan>>();
    mockGetContentPlan
      .mockResolvedValueOnce(plan("ready") as never)
      .mockReturnValueOnce(staleRefresh.promise as never);

    render(<PlanPage />);
    expect(await screen.findByTestId("plan-status")).toHaveTextContent("ready");

    screen.getByRole("button", { name: "Refresh" }).click();
    await waitFor(() => expect(mockGetContentPlan).toHaveBeenCalledTimes(2));
    await act(async () => {
      screen.getByRole("button", { name: "Accept generation" }).click();
    });
    expect(screen.getByTestId("plan-status")).toHaveTextContent("generating");

    await act(async () => staleRefresh.resolve(plan("ready")));
    expect(screen.getByTestId("plan-status")).toHaveTextContent("generating");
  });
});
