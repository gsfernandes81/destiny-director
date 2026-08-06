# Report what happened to a manual Send — stub

## Status: deferred note (2026-08-05). Not scoped.

Spun out of Phase 1 of `plans/anchor_command_web_migration.md`. `POST /feed/{name}/send`
(`dd/anchor/extensions/feed_actions.py`) answers **200 the moment the announcer starts**,
not when the post lands. The page says "Send started — check Mirror logs for delivery",
which is true and is also the whole problem: the operator is told to go look somewhere
else, and a send that fails after the response looks identical to one that worked.

## Why it returns early, and why that must not change

This is deliberate and the reasons are load-bearing — a future implementation must keep
the early return and add reporting *around* it, not replace it with an await:

- Both announcers retry construction **forever** with backoff
  (`autopost.discord_announcer`, `xur.api_to_discord_announcer`), and
  `utils.send_message` retries too. Awaiting that inside an HTTP handler is a request
  that can hang for hours.
- `api_to_discord_announcer` posts its placeholder to the live channel *before*
  constructing, so its retries must not be bounded from outside — see the "once we've
  committed to posting, we always finish" invariant in `xur.py`.

So the send genuinely is a background job. What is missing is a way to watch it.

## The requirement that shapes the design

**Closing the page must not affect the send, and the UI has to say so.**

The send runs in the bot process, detached from the request that started it. Someone who
closes the tab, loses their phone signal, or navigates away has not cancelled anything —
the post is still going out. Any status UI that looks like a progress bar invites the
opposite reading ("if I close this, does it stop?"), and an operator who sits on a page
for two minutes because they are afraid to leave is worse off than the current
fire-and-forget.

Concretely, whatever gets built should:

- State it in the confirm dialog, at the moment of committing — something in the shape of
  "this keeps going if you close the page", not buried in a tooltip.
- Make the status view a **thing you can come back to**, not a thing you must stay on:
  reachable from the feed's row after the fact, so the answer to "did it work" does not
  depend on having kept a tab open.
- Never show a cancel affordance, because there is no cancel.

## What the work is, roughly

The state already exists in the process — `_sending` holds the in-flight feed names and
`_run_send` already logs both outcomes. The gap is that none of it is readable from the
web. Sketch:

- Give a send an id and a small record (feed, started at, publish flag, outcome, error),
  held in memory or written to the DB if it should survive a restart.
- `GET /feed/{name}/status` (or a single `/sends` endpoint) reports the current and most
  recent send per feed; the row on `/autopost_settings` shows the last outcome inline.
- The send modal switches from "started" to polling that endpoint, with the
  close-the-page line visible throughout.

## Open questions before scoping

- **In-memory or persisted?** In-memory is a few lines and loses everything on the
  restart that a failed send might well be followed by. The DB has `MirrorDelivery`-shaped
  precedent, but a manual send is not a mirror and should not be squeezed into that table.
- **Does this want to be per-send status, or does the mirror log already answer it?** The
  mirror log shows the post *once it exists*. That covers "did it land" but not "is it
  still retrying" or "did construction fail on the fourth attempt" — the cases where the
  operator currently learns nothing. Check whether extending the mirror log's view is
  cheaper than a parallel status surface before building the latter.
- **Should a stuck send be visible as an alert rather than a page?** A send that has been
  retrying for twenty minutes is something to be told about (the CRITICAL owner-ping path
  in `dd.common.discord_logging`), not something to discover by revisiting a page. That
  may be the higher-value half of this plan.
