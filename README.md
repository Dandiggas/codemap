# codemap

codemap builds an interactive architecture map of a codebase: three altitudes (Overview, Code, System) in one self-contained HTML file. Everything is scanned locally by a Python standard-library scanner, and gets richer if you point a coding agent at it for the optional labeling pass.

- **Overview**: the repo as a handful of top-level areas, with a plain-language summary and the external services it talks to.
- **Code**: the directory tree with import edges between siblings at every level, drilling down to files and their symbols. Each level is laid out at view time by [dagre](https://github.com/dagrejs/dagre) (v0.8.5, MIT, vendored into the template so the page works offline from `file://`), falling back to the layout baked in by `lib/layout.py` if the global is missing.
- **System**: CI pipeline, containerization, external hosts, and environment variables from workflow files, Dockerfiles, and source. Repos with infrastructure-as-code also get an infra graph.

**Network policy**: the whole tool makes exactly one network call, and it is optional. When [`gh`](https://cli.github.com/) is installed and the repo has a `github.com` remote, `render` asks it for the latest GitHub Actions run per workflow to fill the deployed chip. `--no-gh` skips it; without `gh` or a GitHub remote it never happens. No source, labels, or telemetry ever leave the machine, and `scan` never fetches anything.

## Quickstart

```bash
python3 codemap.py render <path-to-repo>
```

Scans, builds import edges, and writes `<path-to-repo>/.codemap/codemap.html`. Nothing outside `.codemap/` is written.

Without a labeling pass you get the full tree and file list, import edges at every level with file-open links for your editor, and a fully populated System tab (it comes straight from static scanning). What you do not get is plain-language captions, lens groupings, or the overview summary, which shows "No semantic labels yet" until labels exist.

## The optional semantic layer

File captions, symbol captions, arrow labels, lens groupings, and the overview summary come from an LLM reading the code once and caching the result. codemap calls no model itself. Point any coding agent (Claude Code, Codex, or similar) at `AGENTS.md` and tell it to run the labeling flow against your target repo; it reads the uncached files, writes labels into `.codemap/cache.json`, and re-renders. Labels are cached by file content hash, so re-running only relabels what changed.

## Staleness

The rendered HTML is static, so it cannot check the filesystem for changes. It reports its own age two ways:

1. **Timestamp** (always): the header shows generation date, commit, branch, and age, turning amber past 7 days. Self-reported, not a live diff against disk.
2. **Git HEAD** (git repos): `render` writes `.codemap/codemap-freshness.js` stamping the commit it was built from. Install a hook so the sidecar refreshes on commit and merge:

   ```bash
   python3 codemap.py hook <path-to-repo>
   ```

   Requires a git repo with at least one commit; exits non-zero otherwise.

## System view

With infrastructure-as-code present, the System tab replaces the boundaries row with an infra graph: one box per resource (Lambda, table, bucket, queue, service, deployment), laid out by the same dagre engine, arrows carrying the mechanism that connects them.

- **Sources**: Terraform, CloudFormation/SAM/serverless.yml, docker-compose, Kubernetes manifests, and AWS CDK (TypeScript and Python, best-effort). Parsing is deterministic, stdlib-only, and offline.
- **Roles as first-class boxes**: an IAM role or k8s ServiceAccount gets its own dashed amber box, marking an access path rather than a resource. Open one for its grants, attachments, and declaring file:line. An arrow labeled "via `<role>`" means the connection goes through that role, not a direct call. The labeling pass adds a short "why" per role, grounded only in grants the parser found.
- **Deployed chip**: the latest Actions run per workflow shown next to the pipeline strip, amber when its commit differs from the map's, meaning map and deploy may be out of sync. Best-effort: no `gh`, no remote, or a failed call falls back silently to `.codemap/deployed.json` from the last success, never to an error or a blocked render.
- **Environments**: every box carries a badge naming its Terraform module directory or source file. With more than one group, an "Environments" chip row appears above the graph; clicking a chip isolates it, same click-to-filter behaviour as the Code tab lenses. This makes a multi-env Terraform repo readable instead of one pile of same-named boxes.
- **Zero-infrastructure repos** keep the original System tab: pipeline strip, then boundaries and externals.

**Limitations**: these parsers read source text, they do not evaluate it. Terraform `module` and `data` blocks are not parsed, so a stack composed mostly of modules shows a sparse graph. Terraform ids are qualified by directory, so multi-env layouts stay apart and sibling `.tf` files resolve each other while files in different directories never do. Intrinsics needing real evaluation (`Fn::Join`, chained interpolation) resolve only when the literal pieces are present; anything genuinely dynamic is left unresolved rather than guessed. CDK support is regex pattern matching on `new <Construct>(...)` and `.grantX(...)`, marked `"confidence": "best-effort"`, and misses constructs built through helpers. Ambiguous multi-role CFN setups are not disambiguated. k8s ConfigMap and Secret references come from a text scan of `envFrom` and volume blocks, not a schema-aware pod-spec read.

## Supported languages

| Language | How it's parsed | What it catches | What it misses |
|---|---|---|---|
| Python | `ast` (real parser) | functions, classes, methods, imports (including relative `from . import x`), and call edges | dynamically constructed names (`getattr`, `importlib.import_module` with a variable) |
| TypeScript / JavaScript | regex | `class`, `function`, arrow-function assignments, `import`/`require` paths | dynamic `import()`, destructured/re-exported names, `$`-prefixed identifiers, decorators-only class members |
| Go | regex | `type ... struct/interface`, top-level `func`, methods with a named non-generic receiver, imports | aliased and blank imports outside a parenthesized block, generic type parameters, unnamed receivers |

Arrows are import-level, not call chains: an edge means one file imports the other. Call data is collected for Python but not drawn. Regex parsers do not distinguish code from comments and strings, so a commented-out `class Foo` occasionally shows up as a symbol. Unsupported languages still appear in the tree but contribute no symbols or edges.

## CLI

```
python3 codemap.py scan <root>                        # print scan+edges+infra+uncached-files as JSON
python3 codemap.py render <root> [--editor vscode|cursor] [--no-gh]   # writes .codemap/codemap.html
python3 codemap.py freshness <root>                   # write only the git-HEAD sidecar
python3 codemap.py hook <root>                        # install the freshness hook
```

`--editor` sets the scheme for file-open links; omitted, codemap looks for `cursor` on `PATH` and falls back to `vscode`.

## QA

`qa/map-qa.mjs` is optional dev tooling, not part of the core app. It drives a rendered map with Playwright and checks what a screenshot cannot: hover lights exactly the edges a box has (including pruned ones, which stay hidden in the DOM), no duplicate edges, the prune toggle round-trips, lens activation leaves every box in exactly one of `.lit`/`.dimmed`, hover during a lens lights only that box's edges and restores on mouseleave, no arrow label overlaps a box, and levels with more than six boxes use the page. On the System tab it checks every arrow terminates on a real box, role peeks match their grants, and a hostile `.codemap/deployed.json` cannot hurt the page (a `javascript:` URL renders as inert text; malformed `workflows` still leaves the graph rendered).

```bash
npm i playwright   # once, only for this script
node qa/map-qa.mjs <path-to-codemap.html>
```

Prints PASS/FAIL per check, exits 1 on failure, always saves screenshots to `<map-dir>/qa/`.

## Requirements

Python 3.11 or later. No pip packages, no network access needed to build or open a map. The only third-party code is dagre, vendored verbatim into `template.html` (MIT) so the rendered map stays a single offline file.
