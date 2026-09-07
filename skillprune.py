#!/usr/bin/env python3
"""skillprune — audit your installed agent-skill surface against real usage.

Every installed skill puts its name+description in the system prompt on EVERY
turn, forever. This finds the ones that never fire and the ones that collide.

Data sources (all already on your machine, nothing is sent anywhere):
  ~/.claude/projects/**/*.jsonl   what actually fired, and when
  SKILL.md frontmatter            what exists on disk
  plugins/installed_plugins.json  which of those are actually LOADED
  `claude plugin details`         real always-on token cost (not re-derived)
"""
import json, os, re, shutil, subprocess, sys, time
from collections import defaultdict
from pathlib import Path

__version__ = "0.3.0"

W = min(shutil.get_terminal_size((88, 24)).columns, 92)
_COLOR = sys.stdout.isatty() and not os.environ.get("NO_COLOR")
_STEPS = sys.stderr.isatty()
DIM, BOLD, RED, YEL, GRN, CYA = "2", "1", "31", "33", "32", "36"


def c(s, *codes) -> str:
    return f"\033[{';'.join(codes)}m{s}\033[0m" if _COLOR else str(s)


def pad(s: str, n: int, right: bool = False) -> str:
    """Pad to n VISIBLE columns. Colour codes are bytes but not glyphs, so the
    usual f-string width spec silently over-counts and every column drifts."""
    fill = " " * max(0, n - len(re.sub(r"\033\[[0-9;]*m", "", s)))
    return fill + s if right else s + fill


def step(msg: str = "") -> None:
    """Transient progress on stderr. `costs()` shells out per plugin and takes
    ~15s; silence for that long reads as a hang. stdout stays pipe-clean."""
    if _STEPS:
        sys.stderr.write("\r\033[2K" + (c(f"  {msg}", DIM) if msg else ""))
        sys.stderr.flush()

HOME = Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")
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


def registry() -> dict:
    """installPath -> real plugin name, from the plugin manager's own record.

    This is the difference between what is ON DISK and what is LOADED. A
    marketplace you added and a plugin you later uninstalled both leave full
    SKILL.md trees behind; neither costs you a single prompt token. Counting
    them is how you end up reporting 291 skills when 177 are real.
    """
    try:
        d = json.loads((HOME / "plugins" / "installed_plugins.json").read_text())
    except (OSError, ValueError):
        return {}
    return {Path(e["installPath"]): key.split("@")[0]
            for key, entries in d.get("plugins", {}).items()
            for e in entries if e.get("installPath")}


def owner(p: Path, reg: dict, strict: bool):
    """Plugin name, "" for a personal skill, or None if this copy is not loaded."""
    for ip, name in reg.items():
        if ip in p.parents:
            return name
    if HOME / "skills" in p.parents:
        return ""
    if not strict:                  # no registry on disk (fresh install, tests)
        parts = p.parts
        if "marketplaces" in parts and len(parts) > parts.index("marketplaces") + 1:
            return parts[parts.index("marketplaces") + 1]
        return ""
    return None


def installed() -> dict:
    """slug -> {path, desc, plugin, mtime}, LOADED skills only.

    A marketplace checkout and its installed copy are the SAME skill on disk in
    two places; keyed naively they'd show up as a perfect self-collision. Dedupe
    on (name, description), and among the copies keep one the plugin manager
    actually points at — that copy knows the real plugin name, which the
    marketplace directory frequently disagrees with.
    """
    step("reading SKILL.md frontmatter…")
    reg = registry()
    strict = bool(reg)
    best = {}
    for sk in sorted(HOME.rglob("SKILL.md")):
        fm = frontmatter(sk)
        name = fm.get("name") or sk.parent.name
        desc = fm.get("description", "")
        own = owner(sk, reg, strict)
        rec = {"path": sk, "desc": desc, "owner": own, "mtime": sk.stat().st_mtime}
        prev = best.get((name, desc))
        if prev is None or (prev["owner"] is None and own is not None):
            best[(name, desc)] = rec

    global _INERT
    _INERT = sum(1 for v in best.values() if v["owner"] is None)
    skills = {}
    for (name, _), rec in best.items():
        if rec["owner"] is None:        # on disk, not loaded, costs nothing
            continue
        rec["plugin"] = rec["owner"] or None
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
    step("scanning transcripts…")
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


def plugin_list() -> str:
    """`claude plugin list` output, fetched once."""
    global _LIST
    if _LIST is None:
        step("querying installed plugins…")
        try:
            _LIST = subprocess.run(["claude", "plugin", "list"], capture_output=True,
                                   text=True, timeout=60).stdout
        except (OSError, subprocess.SubprocessError):
            _LIST = ""
    return _LIST


_LIST = None
_INERT = 0


def plugin_names() -> dict:
    """marketplace dir -> plugin name. Only a fallback now that the registry
    supplies real names, but it still rescues installs with no registry file."""
    return {mkt: name for name, mkt in re.findall(r"❯\s+(\S+?)@(\S+)", plugin_list())}


def costs() -> dict:
    """component -> always-on tokens, straight from `claude plugin details`."""
    out = {}
    plugs = re.findall(r"❯\s+(\S+?)@", plugin_list())
    for i, plug in enumerate(plugs, 1):
        step(f"measuring token cost… {i}/{len(plugs)}  {plug}")
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
               "age_days": (now - rec["mtime"]) / 86400, "plugin": rec["plugin"],
               "personal": rec["plugin"] is None, "path": str(rec["path"])}
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

    step()
    return {"installed": len(skills), "fired": len(live), "dead": ripe,
            "inert": _INERT,
            "live_tokens": sum(r["cost"] for r in live) + sum(r["cost"] for r in dead),
            "hooks": sorted(hooks), "pname": pname,
            "young": [d for d in dead if d["age_days"] < GRACE_DAYS],
            "reclaimable": sum(d["cost"] for d in ripe),
            "disable": sorted(by_plugin.items(), key=lambda kv: -kv[1][1]),
            "collisions": collisions(skills)}


def bar(frac: float, width: int = 28) -> str:
    n = round(max(0.0, min(1.0, frac)) * width)
    return c("█" * n, RED) + c("░" * (width - n), DIM)


def head(title: str, note: str) -> None:
    pad = max(2, W - len(title) - len(note) - 6)
    print(f"\n  {c(title, BOLD)} {c('─' * pad, DIM)} {c(note, DIM)}")


def render(r: dict) -> None:
    n_dead, n_live = len(r["dead"]), r["fired"]
    pct = n_dead / max(r["installed"], 1)
    share = r["reclaimable"] / max(r["live_tokens"], 1)

    print()
    print("  " + c("skillprune", BOLD) + "  " + c("·", DIM) + "  " + c(str(HOME), DIM))
    if not r["installed"] and not r.get("inert"):
        # "0% has never fired" is a true sentence about an empty set and a
        # useless one to read. Nothing found is a different outcome from
        # nothing wrong, and the difference is the whole point of the tool.
        print("\n  " + c("no skills found here.", YEL)
              + " nothing to audit — is this the right machine?\n")
        print(c(f"  looked in {HOME}/skills, plugins/cache and plugins/marketplaces", DIM))
        print(c("  set CLAUDE_CONFIG_DIR if your Claude Code config lives elsewhere\n", DIM))
        return
    print("\n  " + c(r["installed"], BOLD) + " loaded    " + c(n_live, GRN) + " fired    "
          + c(n_dead, RED) + " dead    " + c(len(r["young"]), DIM) + " too new to judge")
    if r.get("inert"):
        print(c(f"  {r['inert']} more sit on disk, not loaded by any plugin — those cost nothing", DIM))
    print("\n  " + bar(pct) + "  " + c(f"{pct:.0%} of your skill surface has never fired", DIM))
    print("\n  " + c(f"~{r['reclaimable']:,}", BOLD, YEL)
          + " tokens in every system prompt, for skills that have never fired")
    print(c(f"  {share:.0%} of the ~{r['live_tokens']:,} tokens your skills cost you per turn", DIM))
    print(c("  ~ estimated from name + description; the rest from `claude plugin details`", DIM))

    if r["dead"]:
        head("DEAD", "never invoked, still loaded every turn")
        for d in r["dead"][:25]:
            tilde = "~" if d["est"] else ""
            cost = c(f"{tilde}{d['cost']:,}", YEL) + c(" tok", DIM)
            name = d["slug"].split(":")[-1][:34]
            src = c("personal", CYA) if d["personal"] else c(str(d["plugin"])[:24], DIM)
            age = c(f"{d['age_days']:.0f}d", DIM)
            hook = c("  [ships hooks]", YEL) if d["plugin"] in r["hooks"] else ""
            print("    " + pad(cost, 12, right=True) + "   "
                  + pad(name, 36) + pad(src, 26) + age + hook)
        if len(r["dead"]) > 25:
            print(c(f"    … and {len(r['dead']) - 25} more", DIM))

    if r["disable"]:
        head("SAFE TO DISABLE", "not one skill in these has ever fired")
        for plug, (n, tok) in r["disable"]:
            nm = c(r["pname"].get(plug, plug), GRN, BOLD)
            print("    " + pad(nm, 38) + pad(c(f"{n} skills", DIM), 20)
                  + c(f"~{tok:,} tok/turn", YEL))

    if r["disable"] or any(d["personal"] for d in r["dead"]):
        print("\n    " + c("skillprune --prune", BOLD, CYA) + "  " + c("applies this, reversibly", DIM))

    if r["hooks"]:
        head("HELD BACK", "hooks run every session, so zero invocations != unused")
        print(c(f"    {', '.join(r['hooks'])}", DIM))

    if r["collisions"]:
        head("COLLISIONS", "near-identical descriptions; the model picks blind")
        for j, x, y in r["collisions"][:15]:
            print("    " + c(f"{j:.2f}", YEL) + "  " + pad(x[:34], 36)
                  + c("\u27f7", DIM) + "  " + y[:34])
    print()


def prune(r: dict, yes: bool = False) -> None:
    """Disable fully-dead plugins; move dead personal skills to a trash dir.

    Two different levers because they are two different things. A plugin is
    managed by the plugin manager, so `disable` is the honest reversal. A
    personal skill is just a directory you wrote, so it gets MOVED, never
    deleted — a tool that says `delete` has no business being the reason your
    own work is gone.
    """
    plugs = [(r["pname"].get(p, p), n, tok) for p, (n, tok) in r["disable"]]
    root = HOME / "skills"
    dirs = [(d["slug"], Path(d["path"]).parent, d["cost"]) for d in r["dead"] if d["personal"]]
    dirs = [(sl, dd, tok) for sl, dd, tok in dirs if root in dd.parents and dd != root]

    if not plugs and not dirs:
        print(c("\n  nothing safe to prune — every dead skill is inside a plugin that\n"
                "  still has live skills, so disabling it would take those with it.\n", DIM))
        return

    total = sum(t for _, _, t in plugs) + sum(t for _, _, t in dirs)
    print(f"\n  {c('PLAN', BOLD)}  {c('─' * max(2, W - 12), DIM)}")
    for name, n, tok in plugs:
        print("    " + c("disable", RED) + " plugin " + pad(c(name, BOLD), 34)
              + c(f"{n} skills  ~{tok:,} tok", DIM))
    for sl, dd, tok in dirs:
        print("    " + c("trash  ", RED) + " skill  " + pad(c(sl, BOLD), 34)
              + c(f"~{tok:,} tok", DIM))
    print(f"\n  reclaims {c('~' + format(total, ',') + ' tokens', BOLD, YEL)} on every turn.")
    if dirs:
        print(c("  personal skills are moved, not deleted; a restore script is written.", DIM))

    if not yes:
        try:
            if input(f"\n  {c('proceed? [y/N] ', BOLD)}").strip().lower() not in ("y", "yes"):
                print(c("  aborted, nothing changed.\n", DIM))
                return
        except (EOFError, KeyboardInterrupt):
            print(c("\n  aborted, nothing changed.\n", DIM))
            return

    print()
    for name, _, _ in plugs:
        try:
            cp = subprocess.run(["claude", "plugin", "disable", name],
                                capture_output=True, text=True, timeout=120)
            ok = cp.returncode == 0
            print(f"    {c('✓', GRN) if ok else c('✗', RED)} disabled {name}"
                  f"{'' if ok else '  ' + (cp.stderr or cp.stdout).strip()[:70]}")
        except (OSError, subprocess.SubprocessError) as e:
            print(f"    {c('✗', RED)} {name}: {e}")

    if dirs:
        trash = HOME / ".skillprune-trash" / time.strftime("%Y%m%d-%H%M%S")
        trash.mkdir(parents=True, exist_ok=True)
        lines = ["#!/bin/sh", "# undo this prune: restores every skill moved below.", "set -e"]
        for sl, dd, _ in dirs:
            dest = trash / dd.name
            try:
                shutil.move(str(dd), str(dest))
            except (OSError, shutil.Error) as e:
                print(f"    {c('✗', RED)} {sl}: {e}")
                continue
            lines.append(f'mv "$(dirname "$0")/{dd.name}" "{dd}"')
            print(f"    {c('✓', GRN)} trashed {sl}")
        (trash / "restore.sh").write_text("\n".join(lines) + "\n")
        (trash / "restore.sh").chmod(0o755)
        print(f"\n  undo:  {c(f'sh {trash}/restore.sh', BOLD, CYA)}")
    print(c("\n  restart Claude Code for the change to take effect.\n", DIM))


def main() -> None:
    r = report()
    if "--prune" in sys.argv:
        render(r)
        prune(r, yes="--yes" in sys.argv or "-y" in sys.argv)
    else:
        render(r)
    if "--json" in sys.argv:
        Path("skillprune.json").write_text(json.dumps(r, indent=1, default=str))
        print(c("  wrote skillprune.json\n", DIM))


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

    # 5. colour must not corrupt column widths.
    assert len(pad("ab", 6)) == 6
    assert pad("\033[31mab\033[0m", 6).endswith("    "), "ANSI counted as glyphs"
    assert pad("ab", 6, right=True).startswith("    ")

    # 6. THE accuracy bug: a SKILL.md on disk is not a loaded skill. A plugin
    #    you uninstalled and a marketplace you merely added both leave full
    #    trees behind that cost zero prompt tokens.
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        live = root / "plugins/cache/mkt/realplugin/1.0/skills/alive"
        gone = root / "plugins/cache/mkt/removed/1.0/skills/ghost"
        mine = root / "skills/mine"
        for d, nm in ((live, "alive"), (gone, "ghost"), (mine, "mine")):
            d.mkdir(parents=True)
            (d / "SKILL.md").write_text(f"---\nname: {nm}\ndescription: d{nm}\n---\n")
        (root / "plugins").mkdir(exist_ok=True)
        (root / "plugins/installed_plugins.json").write_text(json.dumps(
            {"plugins": {"realplugin@mkt": [{"installPath": str(live.parents[1])}]}}))
        HOME, orig = root, HOME
        got = installed()
        assert set(got) == {"realplugin:alive", "mine"}, got   # ghost dropped
        assert _INERT == 1, _INERT
        assert got["realplugin:alive"]["plugin"] == "realplugin"  # real name, not "mkt"
        assert got["mine"]["plugin"] is None                      # personal

        # 7. prune MOVES a personal skill and leaves a working undo behind.
        rep = {"pname": {}, "disable": [],
               "dead": [{"slug": "mine", "personal": True, "cost": 9,
                         "path": str(mine / "SKILL.md")}]}
        import contextlib, io
        with contextlib.redirect_stdout(io.StringIO()):
            prune(rep, yes=True)
        assert not mine.exists(), "personal skill not moved"
        sh = next((root / ".skillprune-trash").rglob("restore.sh"))
        assert subprocess.run(["sh", str(sh)], capture_output=True).returncode == 0
        assert (mine / "SKILL.md").exists(), "restore.sh did not put it back"
        HOME = orig

    # 8. the CLI surface: --help exits 0, garbage exits 2, neither scans
    me = [sys.executable, __file__]
    for args, code, want in ((["--help"], 0, "usage:"), (["--bogus"], 2, "unknown option")):
        r = subprocess.run(me + args, capture_output=True, text=True, timeout=20)
        assert r.returncode == code, (args, r.returncode)
        assert want in r.stdout + r.stderr, (args, r.stdout, r.stderr)
    r = subprocess.run(me + ["--version"], capture_output=True, text=True, timeout=20)
    assert r.stdout.strip() == f"skillprune {__version__}", r.stdout

    # 9. an empty machine says so, instead of reporting "0% has never fired"
    env = dict(os.environ, CLAUDE_CONFIG_DIR=str(root / "nothing-here"))
    r = subprocess.run(me, capture_output=True, text=True, timeout=60, env=env)
    assert "no skills found" in r.stdout, r.stdout
    assert "never fired" not in r.stdout, r.stdout

    print("ok")


HELP = """skillprune — audit your installed agent skills against real usage.

usage: skillprune [--prune [-y]] [--json] [--selfcheck] [--version] [--help]

  (no flags)   scan and print the report
  --prune      offer to disable dead plugins and trash dead personal skills
  -y, --yes    with --prune, skip the confirmation prompt
  --json       also write skillprune.json (full per-skill data)
  --selfcheck  run the built-in assertions and exit
  --version    print version and exit

Nothing leaves your machine. --prune disables plugins (reversible with
`claude plugin enable`) and MOVES personal skills to ~/.skillprune-trash/
with a restore.sh — it never deletes anything.

https://github.com/dahoai/skillprune
"""

FLAGS = {"--prune", "-y", "--yes", "--json", "--selfcheck", "--version", "-h", "--help"}


def cli():
    bad = [a for a in sys.argv[1:] if a not in FLAGS]
    if bad or {"-h", "--help"} & set(sys.argv):
        if bad:
            print(c(f"unknown option: {bad[0]}\n", RED), file=sys.stderr)
        print(HELP, file=sys.stderr if bad else sys.stdout)
        sys.exit(2 if bad else 0)
    if "--version" in sys.argv:
        print(f"skillprune {__version__}")
        return
    demo() if "--selfcheck" in sys.argv else main()


if __name__ == "__main__":
    cli()
