# Landing page

A static page for the project, in `docs/site/`: plain HTML, one style sheet, two small scripts, the same design tokens as the app (light, dark and automatic; the colour themes *Circuit* and *Aurora*). It loads nothing from other servers: the font, the icon, every picture and the clip are files in this folder, and a content security policy in the page forbids anything else. English only; the languages of the app are not touched by it.

| File | What it is |
|---|---|
| `index.html` | the page: hero with the clip, features, a tour viewer (every view, mode and colour theme), the phone layout, install, notes |
| `site.css`, `site.js`, `init.js` | styles; the switches, the viewer and the clip; the remembered mode and theme applied before the page is drawn |
| `media/demo.webm`, `media/demo.mp4`, `media/demo.gif` | the clip of the sample network (map with moving dots, data paths, nightly report); the GIF is the fallback and is kept under 5 MB; `media/demo-still.png` is the still picture shown before it plays, and the only picture when the visitor prefers reduced motion |
| `img/` | the screenshots of the tour (`<view>-{light,dark}-{circuit,aurora}.png`, `phone-…` for the phone layout), 256 colours each |
| `social.png` | the preview image for links, 1280 × 640 |
| `fonts/` | the font file of the app and its licence |

## Preview

```sh
python3 -m http.server 8000 --directory docs/site
```

Then open `http://127.0.0.1:8000/`.

## The preview image and its version

Link previews are kept per address, so a changed picture under an unchanged address is not shown again. The page therefore names the image as `social.png?v=<version>`. `scripts/site_stamp.py` writes the current `VERSION` into that address (`--check` only compares); the Pages workflow runs it before it publishes.

## Publishing with Pages

The workflow `.github/workflows/pages.yml` publishes `docs/site/`. It starts by hand only (*Actions › Pages › Run workflow*) and has no effect until Pages is switched on for the repository (*Settings › Pages › Source: Actions*). The addresses in `index.html` (`canonical`, `og:url`, `og:image`) assume the usual project address of the repository owner; change them when the page is published somewhere else.

## Pictures

All pictures show the built-in sample network (`LANLOOM_DEMO=1`, invented names and addresses; `LANLOOM_DEMO_HOUR=12.5` gives the daytime look of the energy view). They are checked like every other picture of the repository: no brand names, no addresses and no names in the text of a picture (read by character recognition), at most four pictures per row of a gallery, a width of at least 300 px.
