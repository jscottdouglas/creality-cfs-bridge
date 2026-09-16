#!/usr/bin/env python3
"""Privacy scan: refuse to publish anything that points back at one machine.

    python3 privacy_scan.py --root <dir|file> [--identifiers <file>] [--git]
                            [--git-since <sha>] [--no-allow]

Exit 0 when clean, 1 when a match is found, 2 when the scan could not run.

A line containing the marker `privacy-scan: allow` is skipped, for the cases
where a pattern is structurally right and the line is genuinely innocent: a
protocol field whose name happens to end in Token, a JavaScript property whose
name happens to be spelled like a key file's extension. Skipping
is never silent: every skipped line is printed as `path:line: allowed: <text>`,
so a marker cannot hide in a repository unnoticed and a reviewer sees exactly
what was waved through. `--no-allow` ignores the markers and scans those lines
like any other, which is how you check what the markers are actually covering.

With --git, the commit authors, the commit messages and the lines each commit
ADDS are all scanned, so data that was committed and later deleted is still
caught. --git-since limits the range to <sha>..HEAD. When --root names a single
file, the git commands run in that file's directory, so scanning one file and
the history it came from in a single call works.

Credentials are matched by value rather than by word: an assignment of a quoted
literal of six or more characters to password, passwd, api_key, token or secret
is a hit, while a bare mention of one of those words is not, so ordinary code
such as it.key() or "printhost_apikey" stays clean.

Exit 2 covers every case where the scan cannot see everything it claims to:
a missing --root, a directory that cannot be listed, a file that cannot be
read, an unreadable git history, and an --identifiers file that yields no
patterns. A gate that scanned nothing must never report "clean".

The patterns below are generic and public. Machine-specific identifiers (this
machine's user name, employer domain, serials) come from --identifiers, one
regex per line, which is never committed.
"""
import argparse, os, re, subprocess, sys

# `~/.cfsbridge` is the documented config and log location off Windows, so it
# belongs in published docs and is not a leak of anybody's home directory.
# The two addresses are the documented examples and nobody's real machine:
# `.50` is always the printer, `.60` is always a bridge running on another PC.
ALLOWED = ("192.168.1.50", "192.168.1.60",
           "jscottdouglas@users.noreply.github.com", "~/.cfsbridge")
# An inline, per-line waiver. Every use is printed, so it cannot hide.
ALLOW_MARKER = "privacy-scan: allow"
PATTERNS = [
    r"\b10\.\d{1,3}\.\d{1,3}\.\d{1,3}\b",
    r"\b172\.(1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3}\b",
    r"\b192\.168\.\d{1,3}\.\d{1,3}\b",
    r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}",
    r"C:\\Users\\", r"/home/[a-z]", r"/mnt/[a-z]/",
    # A home-relative path is somebody's own machine layout, and it survives
    # every scrub that only looks for absolute paths: a tilde-prefixed source
    # or virtualenv directory says as much about one PC as the absolute form
    # does. The first two patterns are the directories this project actually
    # leaked; the third is the general case. ALLOWED carries the one
    # home-relative path a reader is meant to see.
    r"~/src", r"~/\.venvs", r"~/[A-Za-z]",  # privacy-scan: allow (a pattern matching itself)
    r"\b(?:[0-9A-F]{2}:){7,}[0-9A-F]{2}\b",  # certificate fingerprints, long MAC-shaped ids
    # Credentials are matched by value, not by word: a bare "token" or
    # "apikey" is ordinary code (it.key(), "printhost_apikey"), while a
    # keyword assigned a quoted literal of six or more characters is a secret.
    # The optional quote before the separator catches the JSON and YAML form,
    # where the keyword itself is quoted too, as config files write it.
    # The lookbehind requires the keyword to start a word, so a protocol field
    # named getToken or a variable named user_secret_id is not a credential,
    # while the same keyword standing on its own with a value still is.
    # (Both cases are spelled out as literals in this scanner's own tests, not
    # here, or these comments would themselves be hits.)
    r"(?<![A-Za-z0-9_])(?:password|passwd|api[_-]?key|token|secret)[\"']?\s*[:=]\s*[\"'][^\"']{6,}[\"']",
    r"\bBearer\s+[A-Za-z0-9._-]{16,}",
    # Key and certificate file names, not the word "key": the lookahead keeps
    # it.key() and key_value out.
    r"\.(?:pem|key)\b(?![\w(])",
    # What an actual key leak looks like, as opposed to a mention of one.
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----",
]
SKIP_DIRS = {".git", "node_modules", "build", "deps", "__pycache__", "dist"}
SKIP_EXT = {".png", ".jpg", ".jpeg", ".gif", ".ico", ".icns", ".zip", ".7z", ".exe", ".dll", ".pdb", ".bin", ".woff", ".woff2", ".ttf", ".otf", ".pyc", ".so", ".dylib"}


class ScanError(RuntimeError):
    """The scan could not see everything, so its result must not be trusted."""


def load_identifiers(path):
    """Read one regex per line, ignoring blanks and # comments.

    An identifiers file that yields no patterns is an error: silently falling
    back to the generic patterns would report a machine-specific leak as clean.
    """
    try:
        with open(path, encoding="utf-8") as fh:
            pats = [l.strip() for l in fh if l.strip() and not l.startswith("#")]
    except OSError as e:
        raise ScanError("cannot read identifiers file %s: %s" % (path, e))
    if not pats:
        raise ScanError("identifiers file has no patterns: %s" % path)
    return pats


def _regex(extra):
    return [re.compile(p, re.I) for p in PATTERNS + list(extra)]


def _hits_in_text(text, regs, allow_marker=True):
    """Return (hits, allowed), each a list of (line number, text).

    `allowed` is every line carrying ALLOW_MARKER, whether or not a pattern
    would have fired on it: a marker on a line that needs no waiver is itself
    worth seeing, and the caller prints the list either way.
    """
    out, allowed = [], []
    for n, line in enumerate(text.splitlines(), 1):
        if allow_marker and ALLOW_MARKER in line:
            allowed.append((n, line.strip()))
            continue
        probe = line
        for a in ALLOWED:
            probe = probe.replace(a, "")
        for r in regs:
            m = r.search(probe)
            if m:
                out.append((n, m.group(0)))
                break
    return out, allowed


def _scan_file(path, regs, hits, allowed=None, allow_marker=True):
    name = os.path.basename(path)
    # A file name cannot carry a comment, so markers never apply to it.
    for _, m in _hits_in_text(name, regs, allow_marker=False)[0]:
        hits.append((path, 0, "filename: " + m))
    if os.path.splitext(name)[1].lower() in SKIP_EXT:
        return
    try:
        with open(path, encoding="utf-8", errors="ignore") as fh:
            text = fh.read()
    except OSError as e:
        # A file the gate cannot read is not a clean file.
        raise ScanError("cannot read %s: %s" % (path, e))
    found, waived = _hits_in_text(text, regs, allow_marker)
    for n, m in found:
        hits.append((path, n, m))
    if allowed is not None:
        for n, line in waived:
            allowed.append((path, n, line))


def scan_tree(root, extra_patterns=(), allowed_out=None, allow_marker=True):
    """Scan a directory tree, or a single file when root names one.

    Returns the hits. Lines waived by ALLOW_MARKER are appended to
    `allowed_out` when one is given, so the caller can print them.
    """
    regs = _regex(extra_patterns)
    hits = []
    if os.path.isfile(root):
        _scan_file(root, regs, hits, allowed_out, allow_marker)
        return hits

    def _unlistable(e):
        # os.walk swallows listing errors by default, which would silently skip
        # a whole subtree and still report "clean".
        raise ScanError("cannot list %s: %s" % (getattr(e, "filename", root), e))

    for d, dirs, files in os.walk(root, onerror=_unlistable):
        dirs[:] = [x for x in dirs if x not in SKIP_DIRS]
        for f in files:
            _scan_file(os.path.join(d, f), regs, hits, allowed_out, allow_marker)
    return hits


def _git_root(root):
    """git -C needs a directory, but --root may name a single file in the repo."""
    return root if os.path.isdir(root) else (os.path.dirname(root) or ".")


def _git_log(root, args, since):
    """Run git log over the range, raising rather than reporting an empty log."""
    rev = ["%s..HEAD" % since] if since else []
    r = subprocess.run(["git", "-C", _git_root(root), "log"] + args + rev,
                       capture_output=True, text=True)
    if r.returncode != 0:
        # A gate that cannot read the history must not report "clean".
        raise ScanError("git log failed in %s: %s" % (_git_root(root), r.stderr.strip()))
    return r.stdout


def scan_git_messages(root, extra_patterns=(), since=None, allowed_out=None, allow_marker=True):
    """Scan commit authors and messages; since limits the range to <since>..HEAD."""
    regs = _regex(extra_patterns)
    log = _git_log(root, ["--format=%H%x00%an <%ae>%x00%B%x1e"], since)
    hits = []
    for rec in log.split("\x1e"):
        if not rec.strip():
            continue
        sha, author, body = (rec.strip("\n").split("\x00") + ["", ""])[:3]
        found, waived = _hits_in_text(author + "\n" + body, regs, allow_marker)
        for n, m in found:
            hits.append(("commit " + sha[:10], n, m))
        if allowed_out is not None:
            for n, line in waived:
                allowed_out.append(("commit " + sha[:10], n, line))
    return hits


def scan_git_content(root, extra_patterns=(), since=None, allowed_out=None, allow_marker=True):
    """Scan the lines each commit ADDS.

    The working tree can be spotless while the history still hands a reader the
    printer's address: a file committed once and deleted later lives on in the
    pack. Scanning added lines catches that without diffing every revision of
    every file twice.
    """
    regs = _regex(extra_patterns)
    log = _git_log(root, ["-p", "--format=%x1e%H"], since)
    hits = []
    for rec in log.split("\x1e"):
        if not rec.strip():
            continue
        lines = rec.split("\n")
        sha = lines[0].strip()
        added = [l[1:] for l in lines[1:] if l.startswith("+") and not l.startswith("+++")]
        found, waived = _hits_in_text("\n".join(added), regs, allow_marker)
        for _, m in found:
            hits.append(("commit %s (content)" % sha[:10], 0, m))
        if allowed_out is not None:
            for _, line in waived:
                allowed_out.append(("commit %s (content)" % sha[:10], 0, line))
    return hits


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", required=True)
    ap.add_argument("--identifiers", help="private file, one regex per line")
    ap.add_argument("--git", action="store_true", help="also scan commit authors and messages")
    ap.add_argument("--git-since", metavar="SHA",
                    help="with --git, scan only <SHA>..HEAD instead of the whole history")
    ap.add_argument("--no-allow", action="store_true",
                    help="ignore `%s` markers and scan those lines too" % ALLOW_MARKER)
    a = ap.parse_args(argv)
    if not os.path.exists(a.root):
        # Otherwise a typo in --root would scan nothing and report "clean".
        ap.error("--root does not exist: %s" % a.root)
    allow_marker = not a.no_allow
    allowed = []
    try:
        extra = load_identifiers(a.identifiers) if a.identifiers else []
        hits = scan_tree(a.root, extra, allowed, allow_marker)
        if a.git:
            hits += scan_git_messages(a.root, extra, a.git_since, allowed, allow_marker)
            hits += scan_git_content(a.root, extra, a.git_since, allowed, allow_marker)
    except ScanError as e:
        print("privacy_scan: %s" % e, file=sys.stderr)
        print("scan incomplete, refusing to report clean", file=sys.stderr)
        return 2
    # Every waiver is printed, so a marker can never pass unseen.
    for p, n, line in allowed:
        print("%s:%d: allowed: %s" % (p, n, line))
    for p, n, m in hits:
        print("%s:%d: %s" % (p, n, m))
    if allowed:
        print("%d allowed line(s)" % len(allowed), file=sys.stderr)
    print("%d hit(s)" % len(hits), file=sys.stderr)
    return 1 if hits else 0


if __name__ == "__main__":
    sys.exit(main())
