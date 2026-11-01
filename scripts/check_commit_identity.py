"""Fail when a commit in the given range carries an author or committer address that is not a
no-reply address. Usage: check_commit_identity.py <rev-range> (as understood by `git log`)."""
import subprocess
import sys

ALLOWED_SUFFIXES = ("@users.noreply.github.com",)
ALLOWED_EXACT = ("noreply@github.com",)


def main(argv):
    rng = argv[1:] or ["HEAD"]
    out = subprocess.run(["git", "log", "--format=%H%x00%ae%x00%ce"] + rng, check=True, capture_output=True, text=True).stdout
    bad = []
    for line in out.splitlines():
        sha, author, committer = line.split("\x00")
        for role, addr in (("author", author), ("committer", committer)):
            a = addr.lower()
            if not (a.endswith(ALLOWED_SUFFIXES) or a in ALLOWED_EXACT):
                bad.append("%s %s is not a no-reply address" % (sha[:10], role))
    for b in bad:
        print(b)
    print("checked %d commit(s), %d problem(s)" % (len(out.splitlines()), len(bad)))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
