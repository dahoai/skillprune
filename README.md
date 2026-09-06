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
python3 skillprune.py            # report
python3 skillprune.py --json     # + skillprune.json
python3 skillprune.py --selfcheck
```

Example, on a real 291-skill install:

```
  291 skills installed · 39 have ever fired · 143 dead (49%) · 109 too new to judge
  ~15,965 tokens paid on every turn for skills you never use
```

## How it decides

| Source | Used for |
|---|---|
| `~/.claude/projects/**/*.jsonl` | what actually fired, and when |
| `SKILL.md` frontmatter | what is installed |
| `claude plugin details` | real always-on token cost (not re-derived) |

Nothing leaves the machine. No dependencies beyond the standard library.

## Where it deliberately errs

A tool that says *delete this* has one unacceptable failure: naming something
you actually use. Four guards, each pinned by an assertion in `--selfcheck`:

- **Slash commands count.** A skill invoked only as `/foo` never produces a
  `Skill` tool call. Counting just the tool call marks it dead.
- **Hooks count.** A plugin shipping a `SessionStart` hook runs every session
  with an invocation count of zero. Those are held back, never auto-disabled.
- **Bare-name matching.** Transcripts log `<plugin>:<skill>`, and that prefix
  often disagrees with the marketplace directory the skill was found in. Two
  plugins sharing a skill name will cross-credit — that errs toward *in use*,
  the only safe direction here.
- **14-day grace.** Something installed yesterday hasn't had its chance.
  Tune `GRACE_DAYS`; skills under it are reported separately, never as dead.

Collision detection is Jaccard overlap on description word-sets (`COLLIDE`,
default 0.35) — crude next to embeddings, but it needs no model and no network.
