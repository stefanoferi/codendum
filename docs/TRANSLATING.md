# Translating Codendum

English is the canonical language of Codendum. The code, comments, CLI
messages, configuration keys and every normative document are written in
English. Translations help people deploy and operate the service in their own
language. When a translation and the English text disagree, the English text
wins.

## Layout

```text
README.md             canonical guide (English)
docs/<lang>/README.md translation of README.md, one directory per language
docs/it/README.md     Italian translation (currently the only one)
```

`<lang>` is an [IETF BCP 47](https://www.rfc-editor.org/info/bcp47) language
tag, for example `it`, `de` or `pt-BR`. Other documents are not translated:
CONTRIBUTING, SECURITY, CODE_OF_CONDUCT, CHANGELOG, MAINTAINING and this file.
Translations of the Code of Conduct should reuse an official Contributor
Covenant translation rather than a new one.

Every translation must:

1. Link back to the English README in its first lines, and the English README
   must link to the translation. Add the language to the links at the top of
   every README.
2. Translate the **whole** README. Summaries are not accepted: installation,
   configuration, security and benchmark instructions must be complete.
3. Keep the `<!-- section: name -->` markers exactly as in English and in the
   same order.
4. Keep every command identical. Code blocks marked `bash` must contain the same
   commands in the same order. Comment lines starting with `#` may be
   translated, commands may not. The same applies to file paths, variable
   names, configuration keys, placeholder host names and anything the reader
   types or sees on screen, such as script output.
5. Carry a `translation-source` marker naming the English revision it matches:

   ```text
   <!-- translation-source: README.md sha256=<sha256 of README.md> -->
   ```

## Keeping translations in sync

The simplest rule: **a pull request that changes `README.md` updates the
translations in the same pull request.**

When the contributor cannot write a language, the pull request may still be
merged. In that case:

1. Leave that translation's `translation-source` marker unchanged. CI then
   reports that the translation is behind the English source (a warning, not a
   failure).
2. Add this notice below the language links of the outdated translation, in
   English and, if possible, in the target language:

   ```text
   > **Outdated translation.** This page does not yet include changes made to the
   > English README after <date in ISO 8601, for example 2026-10-15>. Refer to the
   > English version for current instructions.
   ```

3. Open an issue labelled `translation` that lists the changed sections.

When you bring a translation up to date, remove the notice and update the
marker with the checksum of the English file you translated from:

```bash
sha256sum README.md
```

Structural differences, such as missing sections, a different section order,
different commands or broken links, always fail CI, because a reader following
the translated steps would get a different result.

## Review

- At least one reviewer who reads the target language fluently checks the
  meaning against the English text, not only the grammar.
- Technical terms that users will look up in logs or upstream documentation
  stay in English or keep the English term in parentheses on first use (for
  example "prefill", "KV cache", "preemption").
- Keep the register neutral and applicable both to classrooms and to company
  teams. Use units explicitly and write dates in ISO 8601.

## Checks

`tests/check-docs.sh` (run in CI) verifies:

- the language links near the top of both files;
- identical section markers and the required order of the operational
  sections;
- identical commands in `bash` code blocks;
- that every relative link resolves;
- that no unresolved placeholder text is left;
- whether each `translation-source` marker matches the current `README.md`
  (a warning when it does not).

## Adding a language

1. Copy `docs/it/README.md` to `docs/<lang>/README.md`, or start from
   `README.md` and add the markers.
2. Translate it, fix the relative links (they are two directories deeper) and
   set the `translation-source` marker.
3. Add the language link at the top of `README.md` and of the other
   translations.
4. Extend `tests/check-docs.sh` so that the new file is checked like the Italian
   one.
5. Run `tests/check-docs.sh` locally and open a pull request.
