# slugify

`slugify(text)` returns a URL slug:

- lower case;
- Turkish letters are transliterated: ç→c, ğ→g, ı→i, İ→i, ö→o, ş→s, ü→u;
- every run of characters that are not `a-z` or `0-9` becomes one `-`;
- no `-` at the start or the end;
- an input with no letters or digits returns `""`.
