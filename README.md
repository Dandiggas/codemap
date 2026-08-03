# codemap

codemap builds an interactive architecture map of a codebase: three altitudes (Overview, Code, System) in one self-contained HTML file. Everything is scanned locally by a Python standard-library scanner, and gets richer if you point a coding agent at it for the optional labeling pass.

There is exactly one network call in the whole tool, and it is optional: when the [`gh`](https://cli.github.com/) CLI is installed and the repo has a `github.com` remote, `render` asks `gh` for the latest GitHub Actions run per workflow to fill the deployed chip (see [System view](#system-view)). `python3 codemap.py render <root> --no-gh` skips it, and without `gh` or a GitHub remote it never happens in the first place. Nothing else leaves the machine: no source, no labels, no telemetry.

- **Overview**: the repo as a handful of top-level areas, with a plain-language summary and the external services it talks to.
- **Code**: the directory tree with import edges drawn between siblings at every level, drilling down to individual files and their symbols. Each level is laid out at view time by [dagre](https://github.com/dagrejs/dagre) (v0.8.5, MIT, vendored into the template so the page still works offline from `file://`): boxes sized to their own content, ranks flowing left to right, arrows routed around boxes, and space reserved for every arrow label. If the dagre global is missing the tab falls back to the layout baked in by `lib/layout.py`.
- **System**: CI pipeline, containerization, external hosts, and environment variables, pulled from workflow files, Dockerfiles, and source. Repos with infrastructure-as-code (Terraform, CloudFormation/SAM/serverless.yml, docker-compose, k8s, CDK) get an infra graph here too, with roles as first-class boxes and a deployed-state chip when `gh` is available (see System view below).

## Quickstart (no AI required)

```bash
python3 codemap.py render <path-to-repo>
```

This scans the repo, builds import edges, and writes `<path-to-repo>/.codemap/codemap.html`. Open that file in a browser. Nothing outside `.codemap/` is written, and no code or label ever leaves the machine. The only thing that can reach the network is the optional GitHub Actions status fetch described above; add `--no-gh` to skip it.

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

## System view

When a repo has infrastructure-as-code, the System tab replaces the plain boundaries row with an infra graph: one box per resource (Lambda, table, bucket, queue, service, deployment, ...) laid out by the same dagre engine as the Code tab, with arrows carrying the mechanism that connects them.

- **Sources**: Terraform (`.tf`), CloudFormation/SAM/serverless.yml, docker-compose, Kubernetes manifests (Deployments, Services, RBAC), and AWS CDK source (TypeScript and Python, best-effort: see limitations below). All parsing is deterministic, stdlib-only, and offline; no cloud API calls are made to build the graph itself.
- **Roles as first-class boxes**: an IAM role, a k8s ServiceAccount, or an equivalent principal gets its own box, dashed amber to mark it as an access path rather than a resource. Click one open to see its grants (action + target, one row per grant the parser actually found), what it's attached to, and the file:line it was declared at. An arrow labeled "via `<role name>`" means the connection between two resources goes through that role, not a direct call.
- **The optional labeling pass** (`AGENTS.md` step 4) adds a short "why" to each role and role-attributed connection, same discipline as file captions: grounded only in the grants and attachments the parser found, never invented.
- **Deployed chip**: if the [`gh`](https://cli.github.com/) CLI is on `PATH` and the repo has a `github.com` remote (any remote, not just `origin`), `codemap.py render` fetches the latest run per GitHub Actions workflow and shows it as a chip next to the pipeline strip (workflow, conclusion, short sha, how long ago). A chip turns amber if that run's commit differs from the commit the map itself was generated from, meaning the map and the last deploy may be out of sync. **This is the tool's only network call**; `render --no-gh` skips it outright. It is otherwise best-effort: no `gh`, no remote, no connectivity, or a failed call all fall back silently to whatever was cached at `.codemap/deployed.json` from the last successful fetch (or nothing at all), never to an error or a blocked render.
- **Environments and source groups**: every box carries a small group badge naming where it came from, a Terraform module directory (`envs/prod`, `envs/dev`, or `terraform` for the repo root) or a source file with its extension stripped (`template`, `docker-compose`, `k8s/app`, `cdk/stack`). When a scan finds more than one distinct group, an "Environments" chip row appears above the infra graph. Clicking a chip isolates that environment: its boxes and arrows stay lit, everything else dims, same click-to-filter behavior as the lenses on the Code tab, and clicking the chip again clears it. This is what makes a multi-env Terraform repo (`envs/prod` and `envs/dev` both declaring an `app` role, both rendered on one canvas) readable at a glance instead of one undifferentiated pile of same-named boxes. Single-group repos still show badges on every box, just no chip row, since there's nothing to filter between.
- **Zero-infrastructure repos** keep the original System tab exactly as before: pipeline strip, then a boundaries/externals row.

**Honest limitations**: these parsers read source text, they don't evaluate it. Terraform `module` and `data` blocks are not parsed at all: only `resource` blocks become boxes, so a stack composed mostly of registry or local modules shows a sparse graph (the module's own resources live in files codemap does read, but the wiring expressed through `module.x.output` references is invisible). Terraform ids are qualified by the file's directory, Terraform's module boundary, so a multi-env layout (`envs/prod` + `envs/dev` declaring the same names) keeps its environments apart; sibling `.tf` files in one directory resolve each other's references, files in different directories never do. Terraform/CFN intrinsics that need real evaluation (`Fn::Join` building a dynamic ARN, a variable passed through several layers of interpolation) resolve only when the literal pieces are present in the file; anything genuinely dynamic is left unresolved rather than guessed at. CDK support is regex-based pattern matching on `new <Construct>(...)` and `.grantX(...)` calls, not a real TS/Python parser, so it's marked `"confidence": "best-effort"` on every resource it produces and it misses constructs built through helper functions or indirection. A CFN role naming exactly one `Role:` attribute is resolved as-is; ambiguous multi-role setups aren't disambiguated. k8s env/volume references from a ConfigMap or Secret are found by a best-effort text scan of `envFrom`/volume blocks, not a schema-aware read of the whole pod spec.

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
python3 codemap.py render <root> [--editor vscode|cursor] [--no-gh]  # full build: writes .codemap/codemap.html, prints its path
python3 codemap.py freshness <root>                   # write only the git-HEAD sidecar
python3 codemap.py hook <root>                        # install the post-commit/post-merge freshness hook
```

`--editor` controls the scheme used for file-open links in the rendered HTML (`vscode://` or `cursor://`). If omitted, codemap looks for a `cursor` binary on `PATH` and falls back to `vscode`.

`--no-gh` skips the GitHub Actions deployed-state fetch, the tool's only network call. The deployed chip stays dormant and the render is guaranteed offline. `scan` never fetches anything, with or without the flag.

## QA

`qa/map-qa.mjs` is optional dev tooling, not part of the core app (the core stays zero-dependency, see Requirements below). It drives a rendered `codemap.html` with Playwright and checks the things a screenshot can't: every box's hover lights exactly as many edges as it has in the data (including pruned ones, which stay in the DOM hidden rather than being dropped), no duplicate edges get drawn, the prune chip's show-all/show-fewer toggle round-trips cleanly, lens activation leaves every box in exactly one of `.lit`/`.dimmed`, hovering a box while a lens is active lights that box's edges and only that box's edges (never the lens+hover union) and restores the lens resting state on mouseleave, no visible arrow label overlaps a box, and levels with more than six boxes actually use the page instead of hiding in a strip at the top. On the System tab it also checks the infra graph (every arrow terminating on a real box, role peeks matching their grants) and that a hostile `.codemap/deployed.json` can't hurt the page: a `javascript:` URL renders as inert text rather than a link, and a malformed `workflows` value still leaves the graph rendered.

```bash
npm i playwright   # once, only for this script — the core app needs nothing
node qa/map-qa.mjs <path-to-codemap.html>
```

It prints PASS/FAIL per check, exits 1 on any failure, and always saves screenshots to `<map-dir>/qa/` regardless of pass/fail.

## Requirements

Python 3.11 or later. Nothing to install: no pip packages, and no network access needed to build or open a map. The one optional exception is the GitHub Actions deployed-state fetch, which only runs when `gh` is installed and the repo has a GitHub remote, and which `render --no-gh` disables. The only third-party code involved is the dagre layout engine, vendored verbatim into `template.html` (MIT) so the rendered map stays a single offline file.
