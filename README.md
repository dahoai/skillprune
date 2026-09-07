# skillprune

Every agent skill you install puts its name and description into the system
prompt on **every turn, forever**. Installing is one click; nothing tells you
what to remove.

The OSS ecosystem has solved discovery and install several times over —
[SkillDock](https://github.com/wanghuan9/skilldock) (519★),
[skillfish](https://github.com/knoxgraeme/skillfish) (315★),
[skilld](https://github.com/skilld-dev/skilld) (308★). The other half of the
loop is empty: every audit/eval/conflict tool on GitHub sits at 1–14 stars.

`skillprune` is that other half. It reads your real transcript history and
tells you what to turn off.

```
uvx skillprune                   # run it, install nothing
pipx install skillprune          # or keep it around
```

```
skillprune                       # report
skillprune --prune               # act on it, reversibly
skillprune --json                # + skillprune.json
skillprune --selfcheck           # the guards below, as assertions
skillprune --help                # all of the above
```

No install needed either way — it is one stdlib-only file, so
`python3 skillprune.py` works straight from a clone.

Example, on a real 291-skill install:

```
  171 loaded    35 fired    123 dead    13 too new to judge
  125 more sit on disk, not loaded by any plugin — those cost nothing

  ████████████████████░░░░░░░░  72% of your skill surface has never fired

  ~15,673 tokens in every system prompt, for skills that have never fired
  71% of the ~22,169 tokens your skills cost you per turn
```

## Removing what it finds

`skillprune --prune` prints a plan, asks once, and applies it. Two levers,
because they are two different things:

- **Plugins** where not one skill has ever fired → `claude plugin disable`.
  Reversible with `enable`. A plugin with even one live skill is never offered.
- **Personal skills** in `~/.claude/skills/` → **moved**, never deleted, to
  `~/.claude/.skillprune-trash/<timestamp>/`, next to a generated `restore.sh`
  that puts every one of them back.

There is no per-skill disable for plugin skills — the plugin is the unit the
tool manages, so that is the unit offered.

Set `CLAUDE_CONFIG_DIR` if your Claude Code config lives somewhere other
than `~/.claude`. With nothing to audit it says so, rather than reporting
0%.

## How it decides

| Source | Used for |
|---|---|
| `~/.claude/projects/**/*.jsonl` | what actually fired, and when |
| `SKILL.md` frontmatter | what exists on disk |
| `plugins/installed_plugins.json` | which of those are actually **loaded** |
| `claude plugin details` | real always-on token cost (not re-derived) |

Nothing leaves the machine. No dependencies beyond the standard library.

## Where it deliberately errs

A tool that says *delete this* has one unacceptable failure: naming something
you actually use. Five guards, each pinned by an assertion in `--selfcheck`:

- **Slash commands count.** A skill invoked only as `/foo` never produces a
  `Skill` tool call. Counting just the tool call marks it dead.
- **Hooks count.** A plugin shipping a `SessionStart` hook runs every session
  with an invocation count of zero. Those are held back, never auto-disabled.
- **Bare-name matching.** Transcripts log `<plugin>:<skill>`, and that prefix
  often disagrees with the marketplace directory the skill was found in. Two
  plugins sharing a skill name will cross-credit — that errs toward *in use*,
  the only safe direction here.
- **On disk is not loaded.** A marketplace you added and a plugin you later
  uninstalled both leave complete `SKILL.md` trees behind, and neither costs a
  single prompt token. Only what `installed_plugins.json` actually points at is
  counted. Skipping this inflated the count on the author's own machine from a
  true 171 to 291.
- **14-day grace.** Something installed yesterday hasn't had its chance.
  Tune `GRACE_DAYS`; skills under it are reported separately, never as dead.

Collision detection is Jaccard overlap on description word-sets (`COLLIDE`,
default 0.35) — crude next to embeddings, but it needs no model and no network.

MIT.
