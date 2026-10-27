Add opt-in prefix matching to `click.Choice`.

Add a keyword parameter `allow_prefix: bool = False` to `click.Choice.__init__`, after `case_sensitive`. Store it as the attribute `Choice.allow_prefix` and include it in `Choice.to_info_dict()` under the key `"allow_prefix"`.

When `allow_prefix` is false, nothing changes: a prefix is rejected exactly as today.

When `allow_prefix` is true, `Choice.convert()` resolves a value like this. Matching uses normalized values, as today: the value and every choice go through `Choice.normalize_choice()` (enum member names, the context's `token_normalize_func`, casefolding when `case_sensitive=False`).
1. If the normalized value equals a normalized choice, return that choice. An exact match always wins, even when it is also a prefix of other choices.
2. Otherwise, if the normalized value is non-empty and is a prefix of exactly one normalized choice, return that original choice (the enum member, for enum choices).
3. If it is a prefix of two or more normalized choices, fail (the usual `BadParameter`, raised through `self.fail` like the other conversion errors) with the message `{value!r} is ambiguous; it could be {matches}.`, where `{value!r}` is the value as given, before normalization (as in the existing invalid-choice message), and `{matches}` lists the matching normalized choices in declaration order, each formatted with `repr()`, joined with `", "`. Example: with choices `["start", "stop", "status"]`, the value `"sta"` fails with `'sta' is ambiguous; it could be 'start', 'status'.` On the command line this reads `Error: Invalid value for '--mode': 'sta' is ambiguous; it could be 'start', 'status'.`
4. Otherwise (no choice has it as a prefix, or the value is empty), fail with the existing invalid-choice message, unchanged.

Shell completion, help and metavar output, and the existing error messages stay as they are. This works the same for options, arguments and `multiple=True` options, since they all convert through `Choice.convert()`. Update any existing tests the change affects, and add an entry to `CHANGES.md`.

Where changes may go: `src/click/`, `tests/`, `docs/` and `CHANGES.md`. The evaluation refuses the whole change if it touches anything else (for example `pyproject.toml`), or any `conftest.py`, `pytest.ini`, lockfile, `sitecustomize.py` or a module named `pytest`, even inside those directories.

Environment: the test dependencies are preinstalled and the package itself is not installed, so run the tests from the repository root with `PYTHONPATH=src python -m pytest`.
