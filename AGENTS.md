# codemap labeling contract

This file is the instruction set for any coding agent asked to run codemap against a repository (the "target repo"). It is written for any agent harness, not a specific one. Follow the steps in order.

`<repo-clone-path>` below means wherever this codemap repo lives on disk (the clone that contains `codemap.py`). The target repo is the one being mapped, given as an argument or defaulting to the current working directory.

## The flow

**1. Determine root and scan.**

Root is the argument given to you, or the current working directory if none was given. Run:

```bash
python3 <repo-clone-path>/codemap.py scan <root>
```

This prints JSON: `{"tree_summary": ..., "file_edges": [{"src": ..., "dst": ..., "kind": "import"}, ...], "infra": ..., "uncached": [{"path": ..., "sha1": ...}, ...]}`. Read it. `uncached` lists every source file whose content hash isn't in the label cache yet (new file, or changed since it was last labeled). `file_edges` is the full, exact set of import edges the scanner found; this is where arrow keys in step 3 come from, do not invent or rename them.

**2. If `uncached` is non-empty, label those files.**

Read the uncached files in batches of about 20. If your harness supports parallel subagents, dispatch one per batch on a cheap model tier: labeling is a summarization task, not a reasoning task. For each file, produce:

- a **file caption**: what the file does, ≤ 12 words, no restating the filename
- **symbol captions** for the file's top-level symbols (functions, classes, methods): one line each, what it does
- this file's contribution to **arrow labels**: for each import edge the scanner already found touching this file (do not invent new edges, see Honesty rules), a short label naming what actually flows across that edge (data, a specific type, control, config)

Across the whole batch, also produce:

- **2 to 4 lens groups**: named groupings of related files that cut across directories (e.g. "auth flow", "payment webhook"). A lens is a name plus a list of repo-relative paths.
- one **overview summary**: ≤ 60 words describing what the repo is and does
- for every external host the scanner found in `infra.externals`, one **WHY**: a short reason the codebase talks to that host (e.g. "transactional email" for `api.sendgrid.com`)

**3. Merge labels into the cache.**

The cache lives at `<root>/.codemap/cache.json` and has this exact shape:

```json
{
  "files": {
    "<repo-relative-path>": {
      "sha1": "<sha1 of the file's current bytes>",
      "caption": "<file caption, ≤ 12 words>",
      "symbol_captions": {"<symbol name>": "<one-line caption>"}
    }
  },
  "arrows": {
    "<src file path>→<dst file path>": "<label naming what flows>"
  },
  "lenses": {
    "<lens name>": ["<repo-relative-path>", "..."]
  },
  "overview": {
    "summary": "<≤ 60 words>",
    "externals": {"<host>": {"why": "<short reason>"}}
  }
}
```

`sha1` for each file comes straight from the `uncached` list in step 1, use that value, don't recompute it. `arrows` keys are repo-relative FILE path pairs, one per entry in scan's `file_edges` output, joined with the → character: `"<src path>→<dst path>"` exactly as `src` and `dst` appear there (see Honesty rules). Never key an arrow by directory name.

Merge, do not overwrite: load the existing cache, dict-update `files`/`arrows`/`lenses` with your new entries (new entries win on key collision), replace `overview` wholesale since it's a single summary, then save.

```python
import sys
sys.path.insert(0, "<repo-clone-path>")
from lib.cache import load, save

root = "<root>"
cache = load(root)
cache["files"].update(new_file_labels)      # {path: {sha1, caption, symbol_captions}}
cache["arrows"].update(new_arrow_labels)    # {"src→dst": label}
cache["lenses"].update(new_lenses)          # {name: [paths]}
cache["overview"] = new_overview            # {summary, externals: {host: {why}}}
save(root, cache)
```

**4. Render and open.**

```bash
python3 <repo-clone-path>/codemap.py render <root>
```

This prints the path to `<root>/.codemap/codemap.html`. Open it for the user (the map should end up visible on screen, not just referenced by path).

**5. Offer the freshness hook once.**

If this is the first time codemap has been run against this target repo and it's a git repository with at least one commit, offer once to install the freshness hook:

```bash
python3 <repo-clone-path>/codemap.py hook <root>
```

If the target isn't a git repo (or has zero commits), don't offer the hook: tell the user staleness detection is timestamp-only for this repo.

**6. Offer to gitignore `.codemap/`.**

If the target repo has a `.gitignore` and it doesn't already exclude `.codemap/`, offer to add it. `.codemap/` contains a generated HTML file, a label cache, and a freshness sidecar, none of it belongs in version control.

## Label style rules

- File captions: ≤ 12 words, describe what the file does, not what it's named.
- Symbol captions: one line, same rule.
- Arrow labels: name what flows across the edge, not the mechanism. "user record" is a good label; "imports" is not, since the arrow already means import.
- Lenses: 2 to 4 per repo. Fewer than 2 isn't worth the cache entries; more than 4 stops being a map and starts being a list.
- Overview summary: ≤ 60 words.
- Every external host gets a WHY, even a short one ("error tracking", "object storage").

## Honesty rules

- **Never label an edge the scanner didn't emit.** Arrow labels only apply to edges present in the scan output (imports the parser actually found). If you notice a real dependency the scanner missed (a dynamic import, a Go blank import, a call the regex parser can't see), do not add an arrow for it: that's a scanner gap, not a labeling job. Mention it to the user in passing if it matters.
- **Captions describe what code does, not what you'd like it to do or think it should do.** No speculation about intent, no aspirational descriptions, no marketing language.
- **If a language isn't supported** (anything outside Python, TS/JS, Go), say so plainly rather than leaving it looking silently blank.
