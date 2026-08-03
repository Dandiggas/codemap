# codemap

codemap builds an interactive architecture map of a codebase: three altitudes (Overview, Code, System) in one self-contained HTML file. It runs entirely offline with a Python standard-library scanner, and gets richer if you point a coding agent at it for the optional labeling pass.

- **Overview**: the repo as a handful of top-level areas, with a plain-language summary and the external services it talks to.
- **Code**: the directory tree with import edges drawn between siblings at every level, drilling down to individual files and their symbols. Each level is laid out at view time by [dagre](https://github.com/dagrejs/dagre) (v0.8.5, MIT, vendored into the template so the page still works offline from `file://`): boxes sized to their own content, ranks flowing left to right, arrows routed around boxes, and space reserved for every arrow label. If the dagre global is missing the tab falls back to the layout baked in by `lib/layout.py`.
- **System**: CI pipeline, containerization, external hosts, and environment variables, pulled from workflow files, Dockerfiles, and source.

## Quickstart (no AI required)

```bash
python3 codemap.py render <path-to-repo>
```

This scans the repo, builds import edges, and writes `<path-to-repo>/.codemap/codemap.html`. Open that file in a browser. Nothing is sent anywhere and nothing outside `.codemap/` is written.

Without a labeling pass you get:
- the full directory tree and file list
- import edges between files and directories at every level, with file-open links for your editor
- the System tab (CI, containers, external hosts, env vars) fully populated, since it comes straight from static scanning
- no plain-language captions, lens groupings, or overview summary. The Overview tab shows "No semantic labels yet. Run the labeling step" instead of a summary, and files/symbols show no captions, until the optional labeling pass below fills them in

## The optional semantic layer

File captions, symbol captions, arrow labels, lens groupings, and the overview summary come from an LLM reading the code once and caching the result. codemap does not call any model itself. Instead, point any coding agent (Claude Code, Codex, or similar) at `AGENTS.md` in this repo and tell it to run the labeling flow against your target repo. The agent reads the uncached files, writes labels into `.codemap/cache.json`, and re-renders.

Labels are cached by file content hash (sha1), so re-running the agent after edits only relabels what changed.

## Staleness

The rendered HTML is a static, self-contained file: it cannot reach back out to the filesystem to check whether the source it was built from has changed. It can only tell you how old itself is, in two independent ways:

1. **Timestamp staleness (always available)**: the map's header shows its own generation date, commit, and branch, and how many days old it is. Once that's more than 7 days, the stamp turns amber as a prompt to rerun codemap. This is a self-reported age, not a live diff against disk.
2. **Git-HEAD staleness (git repos only)**: `codemap.py render` also writes `.codemap/codemap-freshness.js`, stamping the commit the map was built from. Install a lightweight post-commit/post-merge hook so the sidecar refreshes automatically:

   ```bash
   python3 codemap.py hook <path-to-repo>
   ```

   This requires the target to be a git repository with at least one commit; it exits non-zero with an error otherwise.

## Supported languages and honest limitations

| Language | How it's parsed | What it catches | What it misses |
|---|---|---|---|
| Python | `ast` (real parser) | functions, classes, methods, imports (including relative `from . import x`), and call edges | dynamically constructed names (`getattr`, `importlib.import_module` with a variable) |
| TypeScript / JavaScript | regex | `class`, `function`, arrow-function assignments, `import`/`require` paths | dynamic `import()`, destructured/re-exported names, `$`-prefixed identifiers, decorators-only class members |
| Go | regex | `type ... struct/interface`, top-level `func`, methods with a named non-generic receiver, imports | aliased imports and blank imports (`import f "fmt"`, `import _ "x"`) written outside a parenthesized import block, generic type parameters, unnamed receivers |

Two more things worth knowing:

- **Arrows are import-level, not call chains.** An edge between two files or directories means one imports the other, not that a specific function calls another. Call data is collected for Python but is not currently drawn as edges.
- **Regex parsers can false-positive on comments and strings.** A commented-out `class Foo` or a string that happens to look like an import will occasionally show up as a symbol or edge. The scanner does not distinguish code from comments/strings for TS/JS and Go.

If a file's language isn't supported at all (anything outside `.py`, `.ts`/`.tsx`/`.js`/`.jsx`/`.mjs`, `.go`), it still appears in the tree but contributes no symbols, imports, or arrows.

## CLI reference

```
python3 codemap.py scan <root>                       # print scan+edges+infra+uncached-files as JSON, no HTML written
python3 codemap.py render <root> [--editor vscode|cursor]  # full build: writes .codemap/codemap.html, prints its path
python3 codemap.py freshness <root>                   # write only the git-HEAD sidecar
python3 codemap.py hook <root>                        # install the post-commit/post-merge freshness hook
```

`--editor` controls the scheme used for file-open links in the rendered HTML (`vscode://` or `cursor://`). If omitted, codemap looks for a `cursor` binary on `PATH` and falls back to `vscode`.

## QA

`qa/map-qa.mjs` is optional dev tooling, not part of the core app (the core stays zero-dependency, see Requirements below). It drives a rendered `codemap.html` with Playwright and checks the things a screenshot can't: every box's hover lights exactly as many edges as it has in the data (including pruned ones, which stay in the DOM hidden rather than being dropped), no duplicate edges get drawn, the prune chip's show-all/show-fewer toggle round-trips cleanly, lens activation leaves every box in exactly one of `.lit`/`.dimmed`, hovering a box while a lens is active lights that box's edges and only that box's edges (never the lens+hover union) and restores the lens resting state on mouseleave, no visible arrow label overlaps a box, and levels with more than six boxes actually use the page instead of hiding in a strip at the top.

```bash
npm i playwright   # once, only for this script — the core app needs nothing
node qa/map-qa.mjs <path-to-codemap.html>
```

It prints PASS/FAIL per check, exits 1 on any failure, and always saves screenshots to `<map-dir>/qa/` regardless of pass/fail.

## Requirements

Python 3.11 or later. Nothing to install: no pip packages, no network. The only third-party code involved is the dagre layout engine, vendored verbatim into `template.html` (MIT) so the rendered map stays a single offline file.
