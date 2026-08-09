# STUB: bring the tree to a formatter fixed point, then hold it there in CI

> **Status: stub / deferred.** Written during the feed-catalog work
> (`feed-catalog`), where an unrelated `cfg.py` reformat rode into a refactor branch
> twice — once during the Postgres merge, once during the catalog fixes — because
> `make format` reformats the whole tree, not the files you touched. Both times it was
> reverted by hand. The fix is small and mechanical, but it changes a shared file and
> adds a CI gate, so it wants its own change rather than riding along in someone else's.

## The problem

`make format` runs `ruff format dd conftest.py`, i.e. **the whole tree**. CI
(`.github/workflows/ci.yml`) runs `ruff check` and `ty check` — but **not**
`ruff format --check`. So:

- the tree is free to drift from what `ruff format` would produce, and has;
- whoever next runs `make format` silently picks that drift up into their branch;
- nothing fails, so it lands unless someone reads `git diff --stat` and recognises a
  file their change has no business touching.

That is exactly how a feed-naming branch ended up reformatting a DB-config helper's
signature.

## The blast radius is one file

Measured on `feed-catalog` at `e981185`:

```
$ uv run ruff format --check dd conftest.py
Would reformat: dd/common/cfg.py
1 file would be reformatted, 235 files already formatted
```

So this is not a sweeping reformat. It is **one file, one hunk** — `_db_config`'s
signature, which `ruff format` wants wrapped as:

```python
def _db_config(
    db_url_async: str,
) -> tuple[
```

Worth re-measuring before starting; the number only grows while the gate is missing.

## What to do

1. **Reformat, alone.** `uv run ruff format dd conftest.py` on a branch with nothing
   else in it, so the diff is unambiguously "formatter output, no semantic change".
   Verify with `make check` — `ruff format` never changes behaviour, but the commit
   should be able to say the suite was green across it.
2. **Add the gate.** In `.github/workflows/ci.yml`, before or beside the existing
   `uv run ruff check dd`:

   ```yaml
   - name: Format check
     run: uv run ruff format --check dd conftest.py
   ```

   `--check` exits non-zero and prints the offending paths without writing anything.
3. **Add a `make` target** so the local mirror of CI stays honest —
   `format-check: uv run ruff format --check dd conftest.py`, folded into `make check`
   alongside lint/typecheck/test.
4. **Write the rule** (below) into `CLAUDE.md`, under *Linting, formatting & type
   checking*.

Order matters: the gate must not land before the reformat, or CI is red on `main`.

## The CLAUDE.md rule

Deferred to land *with* the reformat, so it is written against a tree that already
satisfies it. Proposed text:

```markdown
- **Format what you edited, not the tree.** `make format` runs `ruff format` over all
  of `dd` — so if the tree has drifted from the formatter, it silently rewrites files
  your change never touched and they ride into your branch. Prefer
  `uv run ruff format <paths you changed>`. Before committing, read
  `git diff --stat` and revert any file your change has no business touching: a
  refactor branch that reformats an unrelated module has a bug in its process, not a
  tidy diff. (Once `ruff format --check` is in CI this stops being a trap, but the
  habit is still the right one.)
```

The parenthetical comes out once step 2 is done — or the whole rule softens to one
line, since the gate makes drift impossible to accumulate in the first place. Decide
when writing it.

## Not in scope

- **Changing the formatter's opinions.** No `[tool.ruff.format]` options, no
  `# fmt: off`. The point is to agree with the tool, not to negotiate with it.
- **`ruff check --fix` behaviour.** Lint autofixes are already gated by CI's
  `ruff check`, which is why lint has never drifted the way formatting has.
- **The JS side.** `dd/*/web_static/*.js` has no formatter and this stub does not
  propose one.
