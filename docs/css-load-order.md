# CSS load order

Between two rules of the same specificity, the one loaded later wins. So the order of the stylesheets is part of the
styling, and moving a rule from one sheet to another can change what it does even when its text stays the same. This
page lists the order `web.py` sends, and the rules for putting a rule in the right sheet.

## The order

The app page (`web.render_page`):

1. `app.css`
2. `goals.css`
3. `txmeta.css`
4. `billcal.css`
5. `rollover.css`
6. `alpaca.css`
7. `bills.css`
8. `chat.css`
9. `catpick.css`
10. `txfilter.css`
11. `wherewent.css`
12. `budgets.css`
13. `settings.css`
14. `light.css`
15. `tap.css`

Sheets 2 to 12 are the feature sheets, in `web.FEATURE_FILES` order. Each entry there also loads its `.js`, so a
feature sheet always comes with a script of the same name.

The sign-in and first-run page: `app.css`, `settings.css`, `light.css`, `tap.css` (`web.render_login_page`; no feature sheet).

`tests/test_css_load_order.py` fetches the three pages over HTTP and checks these lists, that each sheet is served as
CSS, and that this page and budgets.css's header say the same thing.

## What goes where

- **`app.css`**: the base. It holds the tokens, the page shell, and the components every page shares (`.card`, `.li`,
  `.grow`, buttons, sheets). It is the only sheet the sign-in page shares with the app.
- **A feature sheet**: everything that belongs to one feature's page or card. It comes after `app.css`, so it can
  refine a shared component with a selector of the same weight. It comes before `settings.css`, `light.css` and
  `tap.css`, so the light theme and the hit areas still apply on top of it.
- **`settings.css`**: the Settings, first-run, import and sign-in screens.
- **`light.css`**: the light theme (`:root[data-theme=light] …`), last but one, so it recolours everything above.
- **`tap.css`**: the 44px hit areas on a phone, last.

The budget cards (`.bcard`, `.sugg`) and the "No budget yet" rows (`.nb`, `.nbrow`) live in `budgets.css`, at its top,
moved there from `app.css`'s budgets / recurring section. The move was pixel-neutral. The proof compared the computed
style of every element and pseudo-element on every page, at 390, 1024 and 1440 in both themes, and found no
difference.

## Don't depend on source order across sheets

A rule that only wins because it comes later is a trap. Move it, or reorder `FEATURE_FILES`, and it silently loses.
When a component rule from `app.css` has to be overridden in a feature, make the override win on **specificity** and
say which rule it overrides, by selector, never by line number (line numbers go stale with the next edit):

```css
/* OVERRIDE: app.css's `.li.wrap .grow > b { white-space: normal }` matches this row; 0,4,1 beats its 0,3,1 */
.li.wrap.nbrow > .grow > b { white-space: nowrap; }
```

The two overrides on the "No budget yet" rows are written this way: the name never wraps, and the reason (which
carries money) wraps instead of being cut. Each has a browser test that fails without it
(`tests/test_budget_rows.py`), and the layout gate's `rowname` check (docs/ui-invariants.md, rule 19) catches a list
row's name wrapping anywhere in the app.
