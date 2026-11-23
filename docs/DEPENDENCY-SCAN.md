# Dependency scan

Lanloom runs on your own network, often with access to your devices, so a known flaw in something it ships is
treated as a release blocker. `scripts/dep_scan.py` checks two things:

- **the Python packages** pinned in `requirements.txt`, against the OSV database (only package names and versions
  are sent);
- **the built image** - operating system packages and the installed Python packages - with grype, for both
  architectures.

It runs in CI before an image is built for publishing, again on the published image, and in the release gate.

## The rule

| Severity | Without an exception | With a valid exception |
|---|---|---|
| critical | **red - no release** | notice with the reason of the exception |
| high | **red - no release** | notice with the reason of the exception |
| unknown | notice (nothing hides behind a missing score) | notice |
| medium, low | counted | counted |

This is the same rule as the shared release gate of the sibling projects. The rule is also written in
`security/vulnerability-policy.json`, but it can only be made stricter there: a policy file that would make critical
or high anything but red is refused, and a refused policy is a failed scan (exit 2), never a green one. A scan that
cannot run at all (no network, a broken tool) is never green either.

A self-check (`dep_scan.py bait`) proves every run that the scanner still reports a known critical package
(PyYAML 5.3.1) and a known high package (urllib3 1.26.4) as red, through both sources.

## Exceptions

A finding that cannot be fixed by raising a package gets an entry in `security/vulnerability-policy.json`. Every field
is required:

```json
{
 "id": "CVE-2026-0000",
 "packages": ["libexample1"],
 "kind": "not-reachable",
 "reason": "Does not reach Lanloom because ...",
 "checked": "2026-11-23",
 "review_by": "2027-02-23"
}
```

- `id` - the advisory id (an alias such as the GHSA id works too);
- `packages` - the packages the exception applies to; the same flaw in another package is red again;
- `kind` - `not-reachable` (the flaw cannot be reached in Lanloom) or `must-wait` (no fixed package exists yet);
- `reason` - says why: "does not reach Lanloom because ..." or "has to wait because ...";
- `checked` - the day the finding was looked at;
- `review_by` - at most 183 days after `checked`. After that day the exception counts as missing and the release
  stops until someone looks again.

A covered finding stays in every scan as a notice with its reason. An exception that no longer matches any finding
(the fixed package has arrived) is listed by the image scan as *unused* so it can be removed.

## Today's exceptions

All of them are operating system packages of the base image without a fixed package in the base distribution (as of
the checked date). None of them reaches Lanloom: it starts no external programs, has no native code of its own, its
containers run as an unprivileged user with no-new-privileges, and the image carries no set-user-ID or set-group-ID
file (the build removes them, the self-test checks it). The full reason of each entry is in the policy file.

| Advisory | Packages | Kind | Review by |
|---|---|---|---|
| CVE-2026-76642 | bsdutils, libblkid1, liblastlog2-2, libmount1, libsmartcols1, libuuid1, login, mount, util-linux | not-reachable | 2027-02-23 |
| CVE-2026-78408 | bsdutils, libblkid1, liblastlog2-2, libmount1, libsmartcols1, libuuid1, login, mount, util-linux | not-reachable | 2027-02-23 |
| CVE-2026-78409 | bsdutils, libblkid1, liblastlog2-2, libmount1, libsmartcols1, libuuid1, login, mount, util-linux | not-reachable | 2027-02-23 |
| CVE-2026-78410 | bsdutils, libblkid1, liblastlog2-2, libmount1, libsmartcols1, libuuid1, login, mount, util-linux | not-reachable | 2027-02-23 |
| CVE-2026-102010 | gcc-14-base, libgcc-s1, libstdc++6 | not-reachable | 2027-02-23 |
| CVE-2026-95619 | gcc-14-base, libgcc-s1, libstdc++6 | not-reachable | 2027-02-23 |
| CVE-2026-54369 | libacl1 | not-reachable | 2027-02-23 |
| CVE-2026-54370 | libacl1 | not-reachable | 2027-02-23 |
| CVE-2026-84243 | libc-bin, libc6 | not-reachable | 2027-02-23 |
| CVE-2025-69720 | libncursesw6, libtinfo6, ncurses-base, ncurses-bin | not-reachable | 2027-02-23 |
| CVE-2026-119373 | libsqlite3-0 | not-reachable | 2027-02-23 |
| CVE-2026-119374 | libsqlite3-0 | not-reachable | 2027-02-23 |
| CVE-2026-119379 | libsqlite3-0 | not-reachable | 2027-02-23 |
| CVE-2026-107493 | libssl3t64, openssl, openssl-provider-legacy | not-reachable | 2027-02-23 |
| CVE-2026-107496 | libssl3t64, openssl, openssl-provider-legacy | not-reachable | 2027-02-23 |
| CVE-2026-121482 | libssl3t64, openssl, openssl-provider-legacy | not-reachable | 2027-02-23 |
| CVE-2026-124709 | libssl3t64, openssl, openssl-provider-legacy | not-reachable | 2027-02-23 |
| CVE-2026-84786 | libssl3t64, openssl, openssl-provider-legacy | not-reachable | 2027-02-23 |
| CVE-2026-88823 | libssl3t64, openssl, openssl-provider-legacy | not-reachable | 2027-02-23 |
| CVE-2026-95691 | libssl3t64, openssl, openssl-provider-legacy | not-reachable | 2027-02-23 |
| CVE-2026-95692 | libssl3t64, openssl, openssl-provider-legacy | not-reachable | 2027-02-23 |
| CVE-2026-82560 | perl-base | not-reachable | 2027-02-23 |
| CVE-2026-9538 | perl-base | not-reachable | 2027-02-23 |

## Running it yourself

```sh
python3 scripts/dep_scan.py python                     # pinned Python packages (OSV)
python3 scripts/fetch_tools.py --dest /tmp/tools grype
python3 scripts/dep_scan.py bait --tools /tmp/tools    # self-check
python3 scripts/dep_scan.py image registry:ghcr.io/robeertm/lanloom:latest \
    --platform linux/amd64 --platform linux/arm64 --tools /tmp/tools
```

Exit 0 = no red finding, 1 = red finding, 2 = the scan could not run or the policy was refused.
