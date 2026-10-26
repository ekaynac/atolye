When a citty CLI receives an unknown sub command, suggest the closest known one.

Today `runCommand` throws a `CLIError` with code `E_UNKNOWN_COMMAND` and the message `Unknown command <name>` (the name wrapped in `cyan()`). Change it so that, when a suggestion exists, the message is `Unknown command <name>. Did you mean <suggestion>?` (both names wrapped in `cyan()`; colours are off under test, so the plain text is e.g. `Unknown command bild. Did you mean build?`). Without a suggestion the message stays exactly as today. The error class and code do not change, and `runMain` keeps printing the error message as it does now.

How the suggestion is chosen, among the sub commands of the command whose sub command lookup failed (for nested commands, the innermost one):
- Candidates are, in the declaration order of `subCommands`, each sub command's key followed by its `meta.alias` values (a string or an array, in order). Sub commands and their `meta` may be lazy (functions or promises), as everywhere else in citty.
- Sub commands with `meta.hidden: true` are skipped entirely: neither their key nor their aliases are suggested. (Typing a hidden command's exact name still runs it, as today.)
- The distance is the Levenshtein edit distance (insertion, deletion and substitution each cost 1), compared case-insensitively.
- A candidate qualifies when its distance is at most 2 and strictly less than the length of the typed name.
- The qualifying candidate with the smallest distance wins; on a tie, the earliest candidate wins. The suggestion is printed as declared (an alias is printed as the alias, in its original case).

Examples, with sub commands `build`, `dev` and `deploy`: `bild` -> `build`; `BUILD` -> `build`; `biuld` -> `build`; `xyz` -> no suggestion.

Where changes may go: `src/`, `test/`, `README.md` and `CHANGELOG.md`. The evaluation refuses the whole change if it touches anything else, or any `conftest`/test-runner/build configuration (`vitest.config.*`, `tsconfig*.json`, `package.json`) or lockfile, even inside those directories.

Environment: the dev dependencies are preinstalled in `/node_modules` (Node resolves them from the repository). Run the tests from the repository root with `/node_modules/.bin/vitest run`.
