# Contributing

Thank you for your interest in Lanloom.

## Ideas, bug reports and adapter requests: please open an issue

Contributions are welcome **as issues**:

- **Bug reports:** the version, what you did, what you expected and what happened. A diagnostic export or a screenshot helps; please remove addresses and names you do not want to share.
- **Ideas and feature requests:** what you would like to see and why it would help you.
- **Adapter requests:** which kind of device (firewall, switch, access point controller, hypervisor, ...), which local API it offers (link to its documentation), which firmware or software version you run, and what you would like Lanloom to read or, later, to change. If you can, attach recorded API replies with addresses, hardware addresses and names removed.
- **Security problems:** please do not open a public issue; see [SECURITY.md](SECURITY.md).

## Pull requests

Pull requests are read, but **not merged**. Accepted ideas are re-implemented by the maintainer, with their own tests and release checks. This keeps the code under one license and one review standard, and keeps every line that can reach a network device under the same safety rules.

There is no plugin mechanism: adapters are built in and listed in a fixed registry (see [docs/INTEGRATIONS.md](docs/INTEGRATIONS.md)). Lanloom is source-available, not open source; see [LICENSE](LICENSE).

## How requests are handled

Every issue is read. Requests that fit the project are planned and built in the order of the published priorities; the issue is updated when a release contains the change.
