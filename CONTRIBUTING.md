# Contributing

Thank you for your interest in Lanloom.

## Ideas, bug reports and adapter requests: please open an issue

Contributions are welcome **as issues**:

- **Bug reports:** the version, what you did, what you expected and what happened. A diagnostic export or a screenshot helps; please remove addresses and names you do not want to share.
- **Ideas and feature requests:** what you would like to see and why it would help you.
- **Adapter requests:** which kind of device (firewall, switch, access point controller, hypervisor, ...), which local API it offers (link to its documentation), which firmware or software version you run, and what you would like Lanloom to read or, later, to change. If you can, attach recorded API replies with addresses, hardware addresses and names removed.
- **Security problems:** please do not open a public issue; see [SECURITY.md](SECURITY.md).

## Translations

Every text the app shows lives in `lanloom/i18n/` (one file per language for the app and one for the map, for example `en.json` and `board.en.json`). English is the source. To suggest a language or a correction, **open an issue**:

- Say which language and, for a correction, which text in the app is wrong, with the better wording. For a new language, a complete translation can be attached as text (the English files show every key); the maintainer adds it with its own checks.
- Keep the placeholders as they are (`{age}`, `{n}` and so on) and keep plural forms complete: a text such as `{n, plural, one {# device} other {# devices}}` needs every form the language uses.
- Names of products and standard terms stay as they are.

Pull requests with translations are read like any other pull request and are not merged (see below); the text is taken over from the issue.

The checks that every release has to pass: all languages have the same keys and the same placeholders, plural forms are complete, no key is unused or missing, and visible text in the web files only comes from the translation files, never from the code. Numbers, dates and times are written through the browser's `Intl` for the chosen language.

## Pull requests

Pull requests are read, but **not merged**. Accepted ideas are re-implemented by the maintainer, with their own tests and release checks. This keeps the code under one license and one review standard, and keeps every line that can reach a network device under the same safety rules.

There is no plugin mechanism: adapters are built in and listed in a fixed registry (see [docs/INTEGRATIONS.md](docs/INTEGRATIONS.md)). Lanloom is source-available, not open source; see [LICENSE](LICENSE).

## How requests are handled

Every issue is read. Requests that fit the project are planned and built in the order of the published priorities; the issue is updated when a release contains the change.
