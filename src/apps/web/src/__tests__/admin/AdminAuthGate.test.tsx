import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import "@testing-library/jest-dom";

// Mock next/navigation
jest.mock("next/navigation", () => ({
  usePathname: () => "/admin",
  useRouter: () => ({ push: jest.fn(), replace: jest.fn() }),
  useSearchParams: () => new URLSearchParams(),
  useParams: () => ({ id: "test-id" }),
}));

// Mock admin-api
const mockValidateToken = jest.fn();
jest.mock("@/lib/admin-api", () => ({
  getAdminToken: jest.fn(() => null),
  setAdminToken: jest.fn(),
  clearAdminToken: jest.fn(),
  adminValidateToken: (...args: unknown[]) => mockValidateToken(...args),
  adminListTemplates: jest.fn().mockResolvedValue({ templates: [], total: 0 }),
}));

import AdminLayout from "@/app/admin/layout";

describe("AdminAuthGate", () => {
  beforeEach(() => {
    jest.clearAllMocks();
    // Default: no token stored
    const adminApi = require("@/lib/admin-api");
    adminApi.getAdminToken.mockReturnValue(null);
  });

  it("shows login prompt when no token is stored", async () => {
    render(
      <AdminLayout>
        <div>Protected content</div>
      </AdminLayout>,
    );

    await waitFor(() => {
      expect(screen.getByText("Nova Admin")).toBeInTheDocument();
      expect(screen.getByPlaceholderText("Admin token")).toBeInTheDocument();
    });

    // Protected content should not be visible
    expect(screen.queryByText("Protected content")).not.toBeInTheDocument();
  });

  it("shows error on invalid token", async () => {
    mockValidateToken.mockResolvedValue({ ok: false, reason: "invalid_token" });

    render(
      <AdminLayout>
        <div>Protected content</div>
      </AdminLayout>,
    );

    await waitFor(() => {
      expect(screen.getByPlaceholderText("Admin token")).toBeInTheDocument();
    });

    const input = screen.getByPlaceholderText("Admin token");
    const button = screen.getByRole("button", { name: /sign in/i });

    fireEvent.change(input, { target: { value: "wrong-token" } });
    fireEvent.click(button);

    await waitFor(() => {
      expect(screen.getByText("Invalid admin token")).toBeInTheDocument();
    });
  });

  it("distinguishes a misconfigured server from a bad token", async () => {
    mockValidateToken.mockResolvedValue({ ok: false, reason: "server_error" });

    render(
      <AdminLayout>
        <div>Protected content</div>
      </AdminLayout>,
    );

    await waitFor(() => {
      expect(screen.getByPlaceholderText("Admin token")).toBeInTheDocument();
    });

    const input = screen.getByPlaceholderText("Admin token");
    const button = screen.getByRole("button", { name: /sign in/i });

    fireEvent.change(input, { target: { value: "any-token" } });
    fireEvent.click(button);

    await waitFor(() => {
      expect(screen.getByText(/service unavailable/i)).toBeInTheDocument();
    });
    // Must NOT mislead the operator into thinking their token was wrong.
    expect(screen.queryByText("Invalid admin token")).not.toBeInTheDocument();
  });

  it("shows the unavailable screen when validation fails server-side on load", async () => {
    const adminApi = require("@/lib/admin-api");
    adminApi.getAdminToken.mockReturnValue("stored-token");
    mockValidateToken.mockResolvedValue({ ok: false, reason: "server_error" });

    render(
      <AdminLayout>
        <div>Protected content</div>
      </AdminLayout>,
    );

    await waitFor(() => {
      expect(screen.getByText("Admin auth unavailable")).toBeInTheDocument();
    });
    expect(screen.queryByText("Protected content")).not.toBeInTheDocument();
  });

  it("shows content after valid token", async () => {
    const adminApi = require("@/lib/admin-api");
    adminApi.getAdminToken.mockReturnValue("valid-token");
    mockValidateToken.mockResolvedValue({ ok: true });

    render(
      <AdminLayout>
        <div>Protected content</div>
      </AdminLayout>,
    );

    await waitFor(() => {
      expect(screen.getByText("Protected content")).toBeInTheDocument();
    });
  });
});
