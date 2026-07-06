// codegraph/model.test.ts — ed5512b6
// Vitest coverage for the pure data-shaping fn + role normalization + path split.
import { describe, expect, it } from "vitest";
import {
  buildCodeGraphModel,
  normalizeRole,
  splitPath,
  type CodeGraphNode,
  type CodeGraphPayload,
} from "./model";

// A representative get_architecture + search_graph payload.
const PAYLOAD: CodeGraphPayload = {
  architecture: {
    packages: [
      { name: "auth", node_count: 5, layer: 1 },
      { name: "billing", node_count: 3, layer: 0 },
      { name: "empty_pkg", node_count: 0 }, // no matching nodes → stays empty
    ],
  },
  nodes: [
    {
      qualified_name: "auth.login",
      file: "src/auth/login.py",
      label: "Function",
      signature: "def login(user, pw) -> bool",
      docstring: "Authenticate a user.",
      complexity: 4,
      callers: ["routes.post_login"],
      callees: ["db.get_user", "crypto.verify"],
    },
    {
      qualified_name: "auth.LoginForm",
      file: "src/auth/login.py",
      kind: "Class",
      complexity: 2,
    },
    {
      qualified_name: "auth.logout",
      file: "src/auth/logout.py",
      role: "route",
      fan_in: 7,
      fan_out: 1,
    },
    {
      qualified_name: "billing.charge",
      file: "src/billing/charge.py",
      label: "Method",
    },
  ],
};

function findNode(roots: CodeGraphNode[], id: string): CodeGraphNode | undefined {
  const stack = [...roots];
  while (stack.length) {
    const n = stack.pop()!;
    if (n.id === id) return n;
    stack.push(...n.children);
  }
  return undefined;
}

describe("normalizeRole", () => {
  it("maps known aliases to canonical roles", () => {
    expect(normalizeRole("Function")).toBe("function");
    expect(normalizeRole("func")).toBe("function");
    expect(normalizeRole("METHOD")).toBe("method");
    expect(normalizeRole("Class")).toBe("class");
    expect(normalizeRole("interface")).toBe("interface");
    expect(normalizeRole("protocol")).toBe("interface");
    expect(normalizeRole("endpoint")).toBe("route");
    expect(normalizeRole("dir")).toBe("folder");
    expect(normalizeRole("const")).toBe("variable");
  });

  it("is total: unknown/empty/non-string → 'unknown'", () => {
    expect(normalizeRole("wat")).toBe("unknown");
    expect(normalizeRole("")).toBe("unknown");
    expect(normalizeRole(undefined)).toBe("unknown");
    expect(normalizeRole(42)).toBe("unknown");
    expect(normalizeRole(null)).toBe("unknown");
  });
});

describe("splitPath", () => {
  it("splits posix and windows separators, trims junk", () => {
    expect(splitPath("src/auth/login.py")).toEqual(["src", "auth", "login.py"]);
    expect(splitPath("src\\auth\\login.py")).toEqual(["src", "auth", "login.py"]);
    expect(splitPath("./a//b/")).toEqual(["a", "b"]);
  });
  it("returns [] for empty/invalid", () => {
    expect(splitPath("")).toEqual([]);
    expect(splitPath(undefined)).toEqual([]);
    expect(splitPath(42)).toEqual([]);
  });
});

describe("buildCodeGraphModel — hierarchy", () => {
  const model = buildCodeGraphModel(PAYLOAD);

  it("builds a folder->file->function hierarchy", () => {
    // src is a top-level folder (derived from file paths).
    const src = model.roots.find((r) => r.id === "src");
    expect(src).toBeTruthy();
    const authFolder = src!.children.find((c) => c.id === "src/auth");
    expect(authFolder?.role).toBe("folder");
    const loginFile = findNode(model.roots, "src/auth/login.py");
    expect(loginFile?.role).toBe("file");
    // login.py holds two symbols: login (function) + LoginForm (class).
    const symbols = loginFile!.children;
    expect(symbols.map((s) => s.label).sort()).toEqual(["LoginForm", "login"]);
  });

  it("carries role + static metadata onto leaf nodes (no invented fields)", () => {
    const login = findNode(model.roots, "src/auth/login.py::auth.login");
    expect(login?.role).toBe("function");
    expect(login?.meta).toEqual({
      qualifiedName: "auth.login",
      file: "src/auth/login.py",
      signature: "def login(user, pw) -> bool",
      docstring: "Authenticate a user.",
      complexity: 4,
      callers: ["routes.post_login"],
      callees: ["db.get_user", "crypto.verify"],
    });
  });

  it("preserves an explicit role (route) and fan-in/out when caller lists absent", () => {
    const logout = findNode(model.roots, "src/auth/logout.py::auth.logout");
    expect(logout?.role).toBe("route");
    expect(logout?.meta.fanIn).toBe(7);
    expect(logout?.meta.fanOut).toBe(1);
    expect(logout?.meta.callers).toBeUndefined();
  });

  it("counts files and symbols", () => {
    // login.py, logout.py, charge.py = 3 files; 4 symbols.
    expect(model.fileCount).toBe(3);
    expect(model.symbolCount).toBe(4);
  });

  it("seeds empty package folders from architecture with no matching nodes", () => {
    const empty = model.roots.find((r) => r.id === "empty_pkg");
    expect(empty?.role).toBe("package");
    expect(empty?.children).toEqual([]);
  });
});

describe("buildCodeGraphModel — determinism", () => {
  it("same input → deep-equal output (stable ids + sorted children)", () => {
    const a = buildCodeGraphModel(PAYLOAD);
    const b = buildCodeGraphModel(PAYLOAD);
    expect(a).toEqual(b);
    expect(JSON.stringify(a)).toBe(JSON.stringify(b));
  });

  it("children are ordered folders → files → symbols, each sorted by label", () => {
    const model = buildCodeGraphModel({
      nodes: [
        { qualified_name: "z.fn", file: "pkg/z.py", label: "Function" },
        { qualified_name: "a.fn", file: "pkg/a.py", label: "Function" },
        { qualified_name: "sub.fn", file: "pkg/sub/m.py", label: "Function" },
      ],
    });
    const pkg = model.roots.find((r) => r.id === "pkg")!;
    const kinds = pkg.children.map((c) => c.role);
    // folder (sub) first, then files (a.py, z.py) sorted by label.
    expect(kinds[0]).toBe("folder");
    const files = pkg.children.filter((c) => c.role === "file").map((c) => c.label);
    expect(files).toEqual(["a.py", "z.py"]);
  });
});

describe("buildCodeGraphModel — empty/partial payloads", () => {
  it("empty / null / undefined → empty model, no throw", () => {
    for (const input of [undefined, null, {}, { architecture: null, nodes: null }]) {
      const m = buildCodeGraphModel(input as CodeGraphPayload);
      expect(m.roots).toEqual([]);
      expect(m.symbolCount).toBe(0);
      expect(m.fileCount).toBe(0);
    }
  });

  it("nodes with no file are parked under a deterministic (unfiled) bucket", () => {
    const m = buildCodeGraphModel({ nodes: [{ qualified_name: "orphan.fn", label: "Function" }] });
    const unfiled = m.roots.find((r) => r.id === "(unfiled)");
    expect(unfiled).toBeTruthy();
    expect(unfiled!.children[0].label).toBe("fn");
    expect(m.symbolCount).toBe(1);
  });

  it("architecture-only payload yields package folders with no files", () => {
    const m = buildCodeGraphModel({ architecture: { packages: [{ name: "core", node_count: 9 }] } });
    expect(m.roots.map((r) => r.id)).toEqual(["core"]);
    expect(m.fileCount).toBe(0);
    expect(m.symbolCount).toBe(0);
  });

  it("skips malformed nodes without throwing", () => {
    const m = buildCodeGraphModel({
      nodes: [null as any, 42 as any, { file: "" }, { qualified_name: "ok.fn", file: "a.py" }],
    });
    expect(m.symbolCount).toBe(1);
  });

  it("derives a symbol label from qualified_name, then name", () => {
    const m = buildCodeGraphModel({
      nodes: [
        { qualified_name: "a.b.deepName", file: "x.py", label: "Function" },
        { name: "bareName", file: "y.py", label: "Function" },
      ],
    });
    const labels = new Set<string>();
    const walk = (n: CodeGraphNode) => { if (!n.children.length) labels.add(n.label); n.children.forEach(walk); };
    m.roots.forEach(walk);
    expect(labels.has("deepName")).toBe(true);
    expect(labels.has("bareName")).toBe(true);
  });

  it("skips nodes with neither qualified_name nor name (unidentifiable noise)", () => {
    const m = buildCodeGraphModel({
      nodes: [
        { file: "z.py", label: "Function" }, // no qn/name → skipped
        { qualified_name: "keep.fn", file: "z.py", label: "Function" },
      ],
    });
    expect(m.symbolCount).toBe(1);
  });
});
