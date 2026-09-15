# Example configurations

Copy one over `config.yaml` (or point `WATCHER_CONFIG` at it) and run
`python watch.py --show-config` to check what the tool resolved, then
`python watch.py --dump` to see what the page actually gives you.

The workflow for adapting this to any site is always the same:

1. `--dump` the page while it is **closed**. Note a phrase that only appears
   then → `closed_markers`.
2. `--dump` it again while it is **open**. Note the form's field names →
   `form_field_names`.
3. Set `timezone_offset_hours` to the site's own timezone and `active_hours`
   to the hours worth watching.
4. `--once` until both states classify correctly.

If a page has no form at all — say a "Buy now" button appears — set
`require_textarea: false` and use `form_action_hints` to match the button's
target, or rely on `closed_markers` alone and let its absence mean open.
