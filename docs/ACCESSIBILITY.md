# Accessibility

Lanloom is meant to be usable without a mouse and without seeing the picture. The circuit board is a picture; the
**tree view** of the Network view says the same thing in words, and everything else in the app can be reached with the
keyboard.

- [The tree view](#the-tree-view) · [Keyboard](#keyboard) · [Screen readers](#screen-readers) · [Colours and contrast](#colours-and-contrast) · [How it is tested](#how-it-is-tested) · [Known limits](#known-limits)

## The tree view

In the *Network* view, the switch **Board | Tree** (left of the area chips) changes the face of the view. The choice is
kept in the browser; `#/network?v=tree` opens the tree directly.

![The network as a tree, the card of an access point open](screenshots/network-tree-light.png)

The tree is the physical chain, read from the same links as the board: **Internet → gateway → switches and access
points → devices and guests**. Each line shows

- the **name** and the address (or the kind of device),
- the **link**: cable with the port on both ends, wireless with band and network name, or virtual (a guest on a host),
- the **live traffic** as a number, where it is measured,
- the **state** as a word (*Real-time*, *Periodic*, *Interrupted*, with *Offline* in front when a device is down), never only
  as a colour,
- for a branch, how many elements hang below it, and a hint when some of them need attention.

Branches fold and unfold; what you folded stays folded while the live data comes in and when you come back later.
Devices with no known link are listed under *Other addresses*. The area chips and *Problems only* work in the tree as
well and always keep the way up to the Internet.

On a phone the line wraps: name and address first, then link, traffic and state. Opening a line opens the same card as
on the board; closing the card with `Esc` or the button puts you back on that line.

![The tree on a phone](screenshots/network-tree-phone-dark.png)

## Keyboard

Everything can be done with the keyboard; press `?` anywhere in the signed-in app (or search for "Keyboard help") to see the list.

![The keyboard help](screenshots/keyboard-help-light.png)

| Where | Keys |
|---|---|
| Everywhere | `Tab` / `Shift Tab` move, `Enter` or `Space` press, `Esc` closes the card, dialog or sheet and returns to where you were, `/` or `Ctrl K` search, `L` and `I` the side rails, `?` this help |
| First stop | *Skip to content* jumps over the navigation |
| Board | `Tab` goes from tile to tile (the tiles are buttons, named by what they show), `Enter` opens the card; while a tile has the focus, the chain of its partners is lit as it is for the mouse |
| Tree | `↑` `↓` move, `→` opens a branch or goes to its first element, `←` closes it or goes to the element above, `Home` `End` jump to the first or last, `*` opens every branch of the level, a letter jumps to the next name that starts with it, `Enter` or `Space` opens the card |

The tree is a single tab stop: `Tab` enters it once and leaves it once; inside you use the arrows. The focus is always
drawn, in every colour theme.

## Screen readers

- The tree uses the standard *tree* pattern: `tree`, `treeitem`, level, position in the set, expanded or collapsed. The
  name of a line starts with the words that are on the line (so voice control can say them), then adds the kind and the
  state.
- Every button, tile, tab and field has a name that contains its visible text.
- The page has one landmark for the content that takes the focus when you move to another view; the page title follows the view.
- **Spoken status** goes through one polite status region: a device that stopped talking or is back, the overall grade, the
  live connection, and the change of the view. It is throttled: at most one announcement every few seconds, three items at
  most in one go (then "and 2 more changes"), and a device is announced again after a minute at the earliest, so a device
  that flaps cannot talk all day. The first data after opening the page is never announced.
- A picture of numbers has its numbers as a table: under the traffic plot and the availability bars of a card is **Show
  as table** (closed until you ask for it).
- The board itself is a picture with named buttons; the tree is its text alternative.

## Colours and contrast

Text and symbols meet the contrast of WCAG 2.2 level AA in the light and the dark theme, in both colour themes (Circuit and Aurora). Nothing is shown by colour alone: states are words, links have a line style, problems
have a count. Calmer or past things are drawn in a quieter colour, not made see-through, so their text keeps its contrast.
The animation of the tree follows the system setting *Reduce motion*.

## How it is tested

Before a release the app runs against a demo network in a real browser (desktop width and phone width) and an automated
check (axe-core) looks at every view in the light and the dark theme and both colour themes: **no serious or critical
finding is allowed**. Further checks walk the tree with the keyboard (move, fold, open, `Esc`, focus back, the fold state
after a redraw), check that every control has a name that contains its visible text, that nothing clickable is out of reach of the keyboard, that the page is not one big live
region, and that the spoken status is throttled. A machine finds about a third of the problems; if you meet one a
machine cannot, please open an issue and name the screen reader and browser you use.

## Known limits

- The board (the picture) is not itself navigable by arrow keys; use `Tab` between tiles or the tree view.
- Charts drawn on a canvas (traffic, availability, the world map) have their numbers in a table or a list next to them but
  cannot be explored point by point with the keyboard.
- Translations: the app is available in English and German; the help texts of the integrations are English.
