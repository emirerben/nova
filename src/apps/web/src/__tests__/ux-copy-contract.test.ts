import fs from "fs";
import path from "path";
import ts from "typescript";

import { jobFailureCopy } from "@/lib/job-failure-copy";
import { GENERATIVE_PHASE_LABEL } from "@/lib/job-phases";
import { PHASE_LABEL } from "@/lib/template-job-phases";

const SRC_ROOT = path.join(process.cwd(), "src");
const SCAN_ROOTS = [
  path.join(SRC_ROOT, "app"),
  path.join(SRC_ROOT, "components"),
  path.join(SRC_ROOT, "lib", "edit-copilot"),
];

const EXCLUDED_SEGMENTS = [
  `${path.sep}app${path.sep}admin${path.sep}`,
  `${path.sep}app${path.sep}dev-qa${path.sep}`,
  `${path.sep}app${path.sep}api${path.sep}`,
  `${path.sep}__tests__${path.sep}`,
];

const NON_COPY_JSX_ATTRIBUTES = new Set([
  "className",
  "href",
  "src",
  "id",
  "key",
  "name",
  "value",
  "type",
  "role",
  "data-testid",
]);

const COPY_PROPERTY_NAMES = new Set([
  "label",
  "title",
  "detail",
  "description",
  "subtitle",
  "placeholder",
  "ariaLabel",
  "buttonLabel",
  "emptyText",
  "message",
]);

function sourceFilesUnder(root: string): string[] {
  if (!fs.existsSync(root)) return [];
  const files: string[] = [];
  for (const entry of fs.readdirSync(root, { withFileTypes: true })) {
    const full = path.join(root, entry.name);
    if (EXCLUDED_SEGMENTS.some((segment) => full.includes(segment))) continue;
    if (entry.isDirectory()) files.push(...sourceFilesUnder(full));
    else if (/\.tsx?$/.test(entry.name)) files.push(full);
  }
  return files;
}

function propertyName(node: ts.PropertyName | undefined): string | null {
  if (!node) return null;
  if (ts.isIdentifier(node) || ts.isStringLiteral(node)) return node.text;
  return null;
}

function isConsoleArgument(node: ts.Node): boolean {
  for (let current: ts.Node | undefined = node; current; current = current.parent) {
    if (!ts.isCallExpression(current)) continue;
    const callee = current.expression;
    return (
      ts.isPropertyAccessExpression(callee) &&
      ts.isIdentifier(callee.expression) &&
      callee.expression.text === "console"
    );
  }
  return false;
}

function isImportPath(node: ts.Node): boolean {
  for (let current: ts.Node | undefined = node; current; current = current.parent) {
    if (ts.isImportDeclaration(current) || ts.isExportDeclaration(current)) return true;
  }
  return false;
}

function isVisibleContext(node: ts.Node): boolean {
  for (let current: ts.Node | undefined = node; current; current = current.parent) {
    if (ts.isJsxText(current) || ts.isJsxExpression(current)) return true;
    if (ts.isJsxAttribute(current)) {
      const attributeName = ts.isIdentifier(current.name)
        ? current.name.text
        : current.name.getText();
      return !NON_COPY_JSX_ATTRIBUTES.has(attributeName);
    }
    if (ts.isPropertyAssignment(current)) {
      return COPY_PROPERTY_NAMES.has(propertyName(current.name) ?? "");
    }
    if (ts.isCallExpression(current) && ts.isIdentifier(current.expression)) {
      return /^(setError|setMessage|setStatus|toast)$/.test(current.expression.text);
    }
  }
  return false;
}

function jsxTagName(node: ts.JsxOpeningLikeElement): string | null {
  return ts.isIdentifier(node.tagName) ? node.tagName.text : null;
}

function isActionContext(node: ts.Node): boolean {
  for (let current: ts.Node | undefined = node; current; current = current.parent) {
    if (ts.isJsxElement(current)) {
      return ["Button", "Link", "button", "a"].includes(jsxTagName(current.openingElement) ?? "");
    }
    if (ts.isJsxSelfClosingElement(current)) {
      return ["Button", "Link", "button", "a"].includes(jsxTagName(current) ?? "");
    }
    if (ts.isPropertyAssignment(current)) {
      return propertyName(current.name) === "buttonLabel";
    }
  }
  return false;
}

interface Violation {
  file: string;
  line: number;
  rule: string;
  text: string;
}

function copyViolations(): Violation[] {
  const violations: Violation[] = [];
  const files = SCAN_ROOTS.flatMap(sourceFilesUnder);

  for (const file of files) {
    const source = fs.readFileSync(file, "utf8");
    const sourceFile = ts.createSourceFile(
      file,
      source,
      ts.ScriptTarget.Latest,
      true,
      file.endsWith(".tsx") ? ts.ScriptKind.TSX : ts.ScriptKind.TS,
    );

    function record(node: ts.Node, text: string) {
      if (isImportPath(node) || isConsoleArgument(node)) return;
      const visible = isVisibleContext(node);
      const checks: Array<[RegExp, string, boolean]> = [
        [/\bNova\b/, "Use Kria in user-visible copy", true],
        [/Something went wrong/i, "Name what failed and provide recovery", true],
        [/⚠\s*legal review/i, "Never expose legal-review markers in production", true],
        [/[←→]/, "Use an icon or outcome label instead of a textual arrow", visible && isActionContext(node)],
      ];
      for (const [pattern, rule, enabled] of checks) {
        if (!enabled || !pattern.test(text)) continue;
        const { line } = sourceFile.getLineAndCharacterOfPosition(node.getStart(sourceFile));
        violations.push({
          file: path.relative(process.cwd(), file),
          line: line + 1,
          rule,
          text: text.replace(/\s+/g, " ").trim(),
        });
      }
    }

    function visit(node: ts.Node) {
      if (ts.isJsxText(node)) record(node, node.getText(sourceFile));
      if (ts.isStringLiteral(node) || ts.isNoSubstitutionTemplateLiteral(node)) {
        record(node, node.text);
      }
      if (ts.isTemplateExpression(node)) {
        const staticText = [node.head.text, ...node.templateSpans.map((span) => span.literal.text)].join("");
        record(node, staticText);
      }
      ts.forEachChild(node, visit);
    }
    visit(sourceFile);
  }

  return violations;
}

describe("production UX copy contract", () => {
  it("keeps banned internal, vague, review-only, and decorative copy out of production UI", () => {
    const violations = copyViolations();
    expect(
      violations.map(
        ({ file, line, rule, text }) => `${file}:${line} — ${rule}: ${JSON.stringify(text)}`,
      ),
    ).toEqual([]);
  });

  it("provides an outcome label for every shared job recovery", () => {
    for (const reason of [
      "user_clip_unusable",
      "user_clip_download_failed",
      "processing_timeout",
      "ffmpeg_failed",
      "unknown",
    ]) {
      expect(jobFailureCopy(reason).actionLabel).toMatch(/^[A-Z][^.!?]+$/);
    }
  });

  it("uses the approved creator-facing progress vocabulary", () => {
    expect(GENERATIVE_PHASE_LABEL).toEqual({
      queued: "Waiting to start",
      analyze_clips: "Reviewing your footage",
      match_song: "Choosing music",
      render_variants: "Rendering your video",
      finalize: "Finishing up",
    });
    expect(PHASE_LABEL.analyze_clips).toBe("Reviewing your footage…");
    expect(PHASE_LABEL.assemble).toBe("Rendering your video…");
    expect(PHASE_LABEL.finalize).toBe("Finishing up…");
  });
});
