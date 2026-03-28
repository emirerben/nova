import { NextRequest, NextResponse } from "next/server";

const GITHUB_TOKEN = process.env.GITHUB_TOKEN;
const GITHUB_REPO = process.env.GITHUB_REPO ?? "emirerben/nova";

function githubHeaders(): Record<string, string> {
  const headers: Record<string, string> = {
    Accept: "application/vnd.github.v3+json",
  };
  if (GITHUB_TOKEN) {
    headers.Authorization = `Bearer ${GITHUB_TOKEN}`;
  }
  return headers;
}

async function fetchIssues(label: string) {
  if (!GITHUB_TOKEN) return { items: [] };

  const url = `https://api.github.com/repos/${GITHUB_REPO}/issues?labels=${encodeURIComponent(label)}&state=open&per_page=20`;

  const res = await fetch(url, { headers: githubHeaders() });

  if (res.status === 403) {
    return { items: [], rateLimited: true };
  }
  if (!res.ok) {
    return { items: [] };
  }

  const data = await res.json();
  const items = data.map(
    (issue: { title: string; html_url: string; number: number; state: string; created_at: string }) => ({
      title: issue.title,
      url: issue.html_url,
      number: issue.number,
      state: issue.state,
      created_at: issue.created_at,
    })
  );

  return { items };
}

async function fetchCommits(path: string) {
  if (!GITHUB_TOKEN) return { items: [] };

  const url = `https://api.github.com/repos/${GITHUB_REPO}/commits?path=${encodeURIComponent(path)}&per_page=5`;

  const res = await fetch(url, { headers: githubHeaders() });

  if (res.status === 403) {
    return { items: [], rateLimited: true };
  }
  if (!res.ok) {
    return { items: [] };
  }

  const data = await res.json();
  const items = data.map(
    (commit: {
      sha: string;
      commit: { message: string; author: { name: string; date: string } };
      html_url: string;
    }) => ({
      sha: commit.sha.slice(0, 7),
      message: commit.commit.message.split("\n")[0],
      author: commit.commit.author.name,
      date: commit.commit.author.date,
      url: commit.html_url,
    })
  );

  return { items };
}

export async function GET(req: NextRequest) {
  const { searchParams } = new URL(req.url);
  const type = searchParams.get("type");

  if (type === "issues") {
    const label = searchParams.get("label");
    if (!label) {
      return NextResponse.json({ error: "Missing label param" }, { status: 400 });
    }
    const result = await fetchIssues(label);
    return NextResponse.json(result);
  }

  if (type === "commits") {
    const path = searchParams.get("path");
    if (!path) {
      return NextResponse.json({ error: "Missing path param" }, { status: 400 });
    }
    const result = await fetchCommits(path);
    return NextResponse.json(result);
  }

  return NextResponse.json({ error: "Invalid type param. Use 'issues' or 'commits'." }, { status: 400 });
}
