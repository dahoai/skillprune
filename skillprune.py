#!/usr/bin/env python3
"""skillprune — audit your installed agent-skill surface against real usage.

Every installed skill puts its name+description in the system prompt on EVERY
turn, forever. This finds the ones that never fire and the ones that collide.

Data sources (all already on your machine, nothing is sent anywhere):
  ~/.claude/projects/**/*.jsonl   what actually fired, and when
  SKILL.md frontmatter            what is installed
  `claude plugin details`         real always-on token cost (not re-derived)
"""
import json, os, re, subprocess, sys, time
from collections import defaultdict
from pathlib import Path

HOME = Path.home() / ".claude"
# ponytail: 14d grace so a skill installed yesterday isn't called dead.
GRACE_DAYS = 14
# ponytail: Jaccard on description word-sets. Crude vs embeddings, but it needs
# no model and no deps; raise if you get noise, lower to catch subtler overlap.
COLLIDE = 0.35
# /compact, /model … are CLI builtins that share the <command-name> shape.
BUILTIN = set("compact model mcp login logout resume init plugin feedback clear "
              "help cost status config agents context exit vim terminal-setup "
              "doctor bug review pr-comments release-notes add-dir memory".split())
STOP = set("a an the and or of for to in on with your you it its is are be use "
           "using used when this that from any all via into as at by skill skills "
           "agent agents claude code use uses user".split())


def frontmatter(p: Path) -> dict:
    """Parse the leading --- block. Only name/description matter here."""
    try:
        txt = p.read_text(errors="replace")
    except OSError:
        return {}
    m = re.match(r"^---\s*\n(.*?)\n---", txt, re.S)
    if not m:
        return {}
    out, key = {}, None
    for line in m.group(1).splitlines():
        kv = re.match(r"^(\w[\w-]*):\s*(.*)$", line)
        if kv:
            key, val = kv.group(1), kv.group(2).strip().strip("'\"")
            out[key] = val
        elif key and line.strip():          # folded continuation line
            out[key] += " " + line.strip()
    return out


def installed() -> dict:
    """slug -> {path, desc, mtime}. Plugin skills get a `plugin:name` slug too.

    A marketplace checkout and its installed copy are the SAME skill on disk in
    two places; keyed naively they'd show up as a perfect self-collision. Dedupe
    on (name, description) and keep the plugin-qualified spelling.
    """
    best = {}
    for sk in sorted(HOME.rglob("SKILL.md")):
        fm = frontmatter(sk)
        name = fm.get("name") or sk.parent.name
        desc = fm.get("description", "")
        parts = sk.parts
        plugin = parts[parts.index("marketplaces") + 1] if "marketplaces" in parts else None
        rec = {"path": sk, "desc": desc, "plugin": plugin, "mtime": sk.stat().st_mtime}
        prev = best.get((name, desc))
        if prev is None or (plugin and not prev["plugin"]):
            best[(name, desc)] = rec

    skills = {}
    for (name, _), rec in best.items():
        slug = f"{rec['plugin']}:{name}" if rec["plugin"] else name
        skills.setdefault(slug, rec)
    return skills


def hooked() -> set:
    """Plugins shipping hooks. A hook fires without any Skill tool call, so
    these can look 'never used' while running on every single session."""
    root = HOME / "plugins" / "marketplaces"
    return {d.parent.name for d in root.glob("*/hooks") if d.is_dir()} if root.is_dir() else set()


# A skill reaches the model by more than one road. Counting only the Skill tool
# call marks slash-command-only skills as dead, which is how you end up
# uninstalling something you use every day.
INVOKED = (re.compile(rb'"skill":"([^"]+)"'),              # Skill tool
           re.compile(rb"<command-name>/?([^<]+)</command-name>"))  # /slash


def used() -> dict:
    """slug -> (count, last_epoch), scanned from transcripts."""
    hits = defaultdict(lambda: [0, 0.0])
    for jl in (HOME / "projects").rglob("*.jsonl"):
        try:
            blob = jl.read_bytes()
        except OSError:
            continue
        found = [m for pat in INVOKED for m in pat.findall(blob)]
        if not found:
            continue
        mt = jl.stat().st_mtime
        for raw in found:
            h = hits[raw.decode(errors="replace").strip()]
            h[0] += 1
            h[1] = max(h[1], mt)
    return {k: tuple(v) for k, v in hits.items()}


def plugin_names() -> dict:
    """marketplace dir -> plugin name. `claude plugin disable` wants the plugin
    name, and transcripts spell invocations `<plugin>:<skill>`; neither matches
    the directory the marketplace happens to be checked out into."""
    try:
        listing = subprocess.run(["claude", "plugin", "list"], capture_output=True,
                                 text=True, timeout=60).stdout
    except (OSError, subprocess.SubprocessError):
        return {}
    return {mkt: name for name, mkt in re.findall(r"❯\s+(\S+?)@(\S+)", listing)}


def costs() -> dict:
    """component -> always-on tokens, straight from `claude plugin details`."""
    out = {}
    try:
        listing = subprocess.run(["claude", "plugin", "list"], capture_output=True,
                                 text=True, timeout=60).stdout
    except (OSError, subprocess.SubprocessError):
        return out
    for plug in re.findall(r"❯\s+(\S+?)@", listing):
        try:
            d = subprocess.run(["claude", "plugin", "details", plug],
                               capture_output=True, text=True, timeout=60).stdout
        except (OSError, subprocess.SubprocessError):
            continue
        for name, tok in re.findall(r"^\s{2}(\S+)\s+~([\d.]+k?)\s+", d, re.M):
            out[f"{plug}:{name}"] = int(float(tok.rstrip("k")) * (1000 if tok.endswith("k") else 1))
    return out


def words(s: str) -> set:
    return {w for w in re.findall(r"[a-z]{3,}", s.lower()) if w not in STOP}


def collisions(skills: dict, thresh: float = COLLIDE) -> list:
    """Pairs whose descriptions overlap enough that the model must guess."""
    items = [(k, words(v["desc"])) for k, v in skills.items() if len(v["desc"]) > 40]
    pairs = []
    for i, (a, wa) in enumerate(items):
        for b, wb in items[i + 1:]:
            union = wa | wb
            if not union:
                continue
            j = len(wa & wb) / len(union)
            if j >= thresh:
                pairs.append((round(j, 2), a, b))
    return sorted(pairs, reverse=True)


def report() -> dict:
    skills, fired, cost, hooks = installed(), used(), costs(), hooked()
    pname = plugin_names()
    # Transcripts spell a call `<plugin>:<skill>`, and that plugin prefix rarely
    # matches the marketplace dir we derived the slug from. Match on the bare
    # skill name: two plugins sharing a skill name cross-credit each other, which
    # errs toward "in use" — the only safe direction for a tool that says delete.
    by_bare = defaultdict(lambda: [0, 0.0])
    for k, (n, t) in fired.items():
        b = k.split(":")[-1]
        if b in BUILTIN:
            continue
        by_bare[b][0] += n
        by_bare[b][1] = max(by_bare[b][1], t)

    def hits(slug):
        n, t = by_bare.get(slug.split(":")[-1], (0, 0.0))
        return n, t

    now, dead, live = time.time(), [], []
    for slug, rec in skills.items():
        n, last = hits(slug)
        tok = cost.get(slug, cost.get(slug.split(":")[-1]))
        est = tok is None
        if est:
            # Personal skills aren't in `claude plugin details`. What lands in the
            # prompt is the name + description, so measure exactly that. ~4 chars
            # per token is the usual English rule of thumb.
            tok = round((len(slug) + len(rec["desc"])) / 4)
        row = {"slug": slug, "n": n, "last": last, "cost": tok, "est": est,
               "age_days": (now - rec["mtime"]) / 86400, "plugin": rec["plugin"]}
        (live if n else dead).append(row)

    ripe = [d for d in dead if d["age_days"] >= GRACE_DAYS]
    ripe.sort(key=lambda d: -d["cost"])
    # A plugin is safe to disable only if not one of its skills has ever fired.
    by_plugin = defaultdict(lambda: [0, 0])
    for r in ripe:
        if r["plugin"]:
            by_plugin[r["plugin"]][0] += 1
            by_plugin[r["plugin"]][1] += r["cost"]
    for r in live:
        if r["plugin"]:
            by_plugin.pop(r["plugin"], None)
    for h in hooks:                 # a hook runs every session, Skill call or not
        by_plugin.pop(h, None)

    return {"installed": len(skills), "fired": len(live), "dead": ripe,
            "hooks": sorted(hooks), "pname": pname,
            "young": [d for d in dead if d["age_days"] < GRACE_DAYS],
            "reclaimable": sum(d["cost"] for d in ripe),
            "disable": sorted(by_plugin.items(), key=lambda kv: -kv[1][1]),
            "collisions": collisions(skills)}


def main() -> None:
    r = report()
    pct = 100 * len(r["dead"]) / max(r["installed"], 1)
    print(f"\n  {r['installed']} skills installed · {r['fired']} have ever fired "
          f"· {len(r['dead'])} dead ({pct:.0f}%) · {len(r['young'])} too new to judge")
    print(f"  ~{r['reclaimable']:,} tokens paid on every turn for skills you never use")
    print(f"  (~ = estimated from name+description; the rest from `claude plugin details`)\n")

    print("  DEAD — never invoked, still in your system prompt")
    for d in r["dead"][:25]:
        flag = "~" if d["est"] else " "
        note = "  [plugin has hooks]" if d["plugin"] in r["hooks"] else ""
        print(f"   {flag}{d['cost']:>6,} tok/turn  {d['slug']}  ({d['age_days']:.0f}d){note}")
    if len(r["dead"]) > 25:
        print(f"    … and {len(r['dead']) - 25} more")

    if r["disable"]:
        print("\n  SAFE TO DISABLE — no skill in these plugins has ever fired")
        for plug, (n, tok) in r["disable"]:
            print(f"    claude plugin disable {r['pname'].get(plug, plug):<30}"
                  f" # {n} skills, ~{tok:,} tok/turn")
    if r["hooks"]:
        print(f"\n  HELD BACK — these ship hooks that run every session, so a zero")
        print(f"  invocation count does not mean unused: {', '.join(r['hooks'])}")

    if r["collisions"]:
        print("\n  COLLISIONS — near-identical descriptions; the model picks blind")
        for j, a, b in r["collisions"][:15]:
            print(f"    {j}  {a}  ⟷  {b}")

    if "--json" in sys.argv:
        Path("skillprune.json").write_text(json.dumps(r, indent=1, default=str))
        print("\n  wrote skillprune.json")


def demo() -> None:
    """Self-check on synthetic data — the joins and thresholds, not the I/O."""
    assert frontmatter.__doc__
    fm_src = "---\nname: x\ndescription: line one\n  continued here\n---\nbody\n"
    p = Path(os.environ.get("TMPDIR", "/tmp")) / "_sp_SKILL.md"
    p.write_text(fm_src)
    fm = frontmatter(p)
    assert fm["name"] == "x", fm
    assert fm["description"] == "line one continued here", fm
    p.unlink()
    assert frontmatter(Path("/nonexistent/SKILL.md")) == {}

    # Jaccard must catch a real duplicate and ignore an unrelated pair.
    s = {"a": {"desc": "generate seo optimized blog article content writing " * 3},
         "b": {"desc": "generate seo optimized blog article content writing " * 3},
         "c": {"desc": "render cinematic product video with remotion timeline ffmpeg"}}
    cols = collisions(s)
    assert [(c[1], c[2]) for c in cols] == [("a", "b")], cols
    assert collisions(s, thresh=1.01) == []
    assert words("The and of SKILL video") == {"video"}

    # The three bugs real data exposed, pinned so they cannot come back.
    # 1. slash-command invocations count as use.
    assert INVOKED[1].findall(b"<command-name>/ponytail</command-name>") == [b"ponytail"]
    assert INVOKED[0].findall(b'"skill":"a:b"') == [b"a:b"]
    # 2. a marketplace copy + installed copy is one skill, not a 1.0 collision.
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        for rel in ("skills/x/SKILL.md", "plugins/marketplaces/p/skills/x/SKILL.md"):
            f = root / rel
            f.parent.mkdir(parents=True)
            f.write_text("---\nname: x\ndescription: same words here exactly\n---\n")
        global HOME
        HOME, orig = root, HOME
        got = installed()
        assert list(got) == ["p:x"], got            # deduped, plugin spelling wins
        assert collisions(got) == [], collisions(got)
        assert hooked() == set()
        (root / "plugins/marketplaces/p/hooks").mkdir()
        assert hooked() == {"p"}                    # 3. hooks make a plugin unsafe
        HOME = orig
    # 4. the bug that would have told you to delete a plugin you use: a call
    #    logged as `claude-ads:ads-google` must credit `<marketplace-dir>:ads-google`.
    import types
    g = dict(globals())
    g["installed"] = lambda: {"ai-marketing-hub:ads-google":
                              {"desc": "d", "plugin": "ai-marketing-hub",
                               "mtime": 0.0, "path": Path(".")}}
    g["used"] = lambda: {"claude-ads:ads-google": (2, time.time()), "compact": (126, 1.0)}
    g["costs"] = lambda: {}
    g["hooked"] = lambda: set()
    g["plugin_names"] = lambda: {"ai-marketing-hub": "claude-ads"}
    r = types.FunctionType(report.__code__, g)()
    assert r["fired"] == 1 and not r["dead"], r      # credited, not marked dead
    assert not r["disable"], r["disable"]            # and not offered for deletion
    print("ok")


if __name__ == "__main__":
    demo() if "--selfcheck" in sys.argv else main()
