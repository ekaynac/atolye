# Items

An item is a **directory** with two files:
- `item.json`: a P-A referee item (`schemas/testing/referee-item.json` in ekaynac/auto-dev-cycle). It holds the
  repository, base, image, optional setup, test command, results format, hidden acceptance, and the editable and
  protected paths.
- `requirement.md`: the only text the agent receives. No other field of the item reaches any arm.

**Items with hidden acceptance are kept private until the three-way comparison ends** (`DECISIONS.md` R28), at
`$ATOLYE_ACCEPTANCE`:

| Set | Location | Contents |
|---|---|---|
| G | `$ATOLYE_ACCEPTANCE/G/<id>/` | Go items from adc-dev |
| P | `$ATOLYE_ACCEPTANCE/P/<id>/` | Python and TypeScript items on public repositories |

**Public here:** the Containerfiles of set P's prepared images (`p/<id>.Containerfile`). The prompts and the hidden
tests are published after the comparison.

**Run one item:**

```sh
uv run atolye-run "$ATOLYE_ACCEPTANCE/P/click-choice-prefix" --out runs/arm-c.jsonl --worker openhands
```
