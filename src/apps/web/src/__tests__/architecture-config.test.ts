import * as fs from "fs";
import * as path from "path";
import {
  modules,
  edges,
  getAllModules,
  getDirectDependents,
  getPipelineModules,
  getDataStoreModules,
  getChildCount,
  type Module,
} from "@/lib/architecture-config";

// Repo root relative to the web app (src/apps/web)
const REPO_ROOT = path.resolve(__dirname, "../../../../..");

describe("architecture-config", () => {
  const allModules = getAllModules();
  const l1Modules = Object.values(modules);
  const pipelineModules = getPipelineModules();

  test("all modules have required fields", () => {
    for (const mod of allModules) {
      expect(mod.id).toBeTruthy();
      expect(mod.name).toBeTruthy();
      expect(mod.description).toBeTruthy();
      expect(Array.isArray(mod.files)).toBe(true);
      expect(Array.isArray(mod.dependsOn)).toBe(true);
      expect(["L1", "L2"]).toContain(mod.level);
    }
  });

  test("no circular dependencies in dependsOn graph", () => {
    const moduleMap = new Map(allModules.map((m) => [m.id, m]));

    function hasCycle(id: string, visited: Set<string>, stack: Set<string>): boolean {
      visited.add(id);
      stack.add(id);
      const mod = moduleMap.get(id);
      if (!mod) return false;
      for (const depId of mod.dependsOn) {
        if (stack.has(depId)) return true;
        if (!visited.has(depId) && hasCycle(depId, visited, stack)) return true;
      }
      stack.delete(id);
      return false;
    }

    const visited = new Set<string>();
    for (const mod of allModules) {
      if (!visited.has(mod.id)) {
        expect(hasCycle(mod.id, visited, new Set())).toBe(false);
      }
    }
  });

  test("all L2 modules are children of an L1 parent", () => {
    const l2FromChildren: Module[] = [];
    for (const l1 of l1Modules) {
      if (l1.children) {
        l2FromChildren.push(...Object.values(l1.children));
      }
    }

    const l2Modules = allModules.filter((m) => m.level === "L2");
    const l2ChildIds = new Set(l2FromChildren.map((m) => m.id));

    for (const l2 of l2Modules) {
      expect(l2ChildIds.has(l2.id)).toBe(true);
    }
  });

  test("all file paths in files arrays exist on disk", () => {
    const missing: string[] = [];
    for (const mod of allModules) {
      for (const filePath of mod.files) {
        const fullPath = path.join(REPO_ROOT, filePath);
        if (!fs.existsSync(fullPath)) {
          missing.push(`${mod.id}: ${filePath}`);
        }
      }
    }
    expect(missing).toEqual([]);
  });

  test("getDirectDependents returns correct downstream modules", () => {
    // "processing" depends on "upload", so "processing" is a dependent of "upload"
    const uploadDependents = getDirectDependents("upload");
    const dependentIds = uploadDependents.map((m) => m.id);
    expect(dependentIds).toContain("processing");
  });

  test("getDirectDependents with no dependents returns empty array", () => {
    // "delivery" is the end of the pipeline — nothing depends on it at L1
    const deliveryDependents = getDirectDependents("delivery");
    expect(deliveryDependents).toEqual([]);
  });

  test("dependency graph is valid — all referenced IDs exist", () => {
    const allIds = new Set(allModules.map((m) => m.id));
    const invalid: string[] = [];
    for (const mod of allModules) {
      for (const depId of mod.dependsOn) {
        if (!allIds.has(depId)) {
          invalid.push(`${mod.id} depends on unknown "${depId}"`);
        }
      }
    }
    expect(invalid).toEqual([]);
  });

  test("L1 pipeline modules (non-data-store) have at least one L2 child", () => {
    for (const mod of pipelineModules) {
      const count = getChildCount(mod.id);
      expect(count).toBeGreaterThan(0);
    }
  });
});
