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

- The app speaks English, German, French, Spanish, Dutch and Italian. **For a correction** of one of them, open an issue that names the language, the screen and the text that is wrong, with the wording you would use (the key is not needed; a screenshot or the exact old text is enough). Corrections come in as issues, so every wording is read once more against the other languages before it goes in.
- **For a new language**, a complete translation can be attached as text (the English files show every key); the maintainer adds it with its own checks. Each added language raises the work of every later change by one language, so a request needs a person who can read the result for mistakes.
- Keep the placeholders as they are (`{age}`, `{n}` and so on) and keep plural forms complete: a text such as `{n, plural, one {# device} other {# devices}}` needs every form the language uses.
- Names of products and standard terms stay as they are.

Pull requests with translations are read like any other pull request and are not merged (see below); the text is taken over from the issue.

The checks that every release has to pass: all languages have the same keys and the same placeholders, plural forms are complete, no text is left in English by accident (a text that is the same on purpose is listed with its reason), nothing is cut off or sticks out of the page at phone and desktop width in any language, no key is unused or missing, and visible text in the web files only comes from the translation files, never from the code. Numbers, dates and times are written through the browser's `Intl` for the chosen language.

## Pull requests

Pull requests are read, but **not merged**. Accepted ideas are re-implemented by the maintainer, with their own tests and release checks. This keeps the code under one license and one review standard, and keeps every line that can reach a network device under the same safety rules.

Code that reads the data of one site keeps to the rules in [docs/SITE-SEPARATION.md](docs/SITE-SEPARATION.md) (the request's site from the address and the header, no fallback, every answer and live frame names its site, the page compares after every request and frame); the release checks hold every route and view to them.

There is no plugin mechanism: adapters are built in and listed in a fixed registry (see [docs/INTEGRATIONS.md](docs/INTEGRATIONS.md)). Lanloom is source-available, not open source; see [LICENSE](LICENSE).

## How requests are handled

Every issue is read. Requests that fit the project are planned and built in the order of the published priorities; the issue is updated when a release contains the change.
