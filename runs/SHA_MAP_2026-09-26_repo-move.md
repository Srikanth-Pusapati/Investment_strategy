# Commit-SHA map for the repository move (recorded 2026-09-26, revised 2026-09-29)

The project moved from the previous owner's GitHub account to the author's own
(`Srikanth-Pusapati/Investment_strategy`). The moved history was produced from a fresh bare clone of the
previous repository with ONE `git filter-repo` pass:

1. a mailmap attributing the 67 GitHub web-merge commits (author = the previous account, committer =
   `GitHub <noreply@github.com>`) to Srikanth Babu Pusapati, the author of every other commit;
2. `Co-Authored-By: Claude …` and `Claude-Session: …` trailer lines removed from commit messages;
3. `Merge pull request #N from <org>/<branch>` subjects rewritten to `Merge branch '<branch>' into main`,
   and the org prefix removed elsewhere in messages;
4. **(revised 2026-09-29)** `runs/pre-final-test-run-4/analysis/config.json` removed from all history: the
   full-history secrets scan (gitleaks 8.30) flagged an `ANTHROPIC_API_KEY` value on its line 84 and the
   conservative fix that needs nobody to read the value is to not carry the file. The previous repository
   still has it; the owner should check the value there and rotate the key if it is live;
5. **(revised 2026-09-29)** the previous org handle replaced by `Srikanth-Pusapati` in file contents
   (journal entries and the old LICENSE versions), so the moved repository carries no reference to it.

No dates, no commit order and no other file content changed. Result: 270 commits, one author, one
committer, zero trailers, zero references to the previous org in messages or files.

**Invariant that matters for the evaluation contracts:** none of the above touches the `investment_strategy/`
subtree, so the code fingerprint is unchanged. Verified on the moved `main`: `investment_strategy/` tree id =
`2a6ec14778beafd22d402faae80c82eafb0353b0`, the frozen run-7 id pinned in `pre-final-test-run-7/EVAL_CONTRACT.md`.
Root tree ids and commit SHAs DO change (item 4 changes every root tree after July), so the SHAs pinned in
the contracts and switch docs map as follows. This table supersedes the 2026-09-26 one.

| What | Previous repository | Moved repository |
|---|---|---|
| run-7 frozen merge (contract v3 Config row; PR #60 merge on feature/preview) | `eec972753ff71a3998a0df6911abf6c834cde0cf` | `3d3ee6e43bc9824d0de32615417da6637c903bb1` |
| `origin/main` at the run-7 switch (identical tree) | `a0507a098cf2c69f49139caa57665739ca9ff8e9` | `6788684a5aa3f20d57bd56e50d6d7d0d80723f6b` |
| main after PR #62 (live tree moved to main, Sep 21 evening) | `5516f35dd70c1c83e3fe5002ffcbf7e286144520` | `721b745aec4bb59630b8ac5495d5c0b6884ff01c` |
| contract v3 pre-registration commit (PR #62) | `3e9cc17d138060fb513aab7e6ce7be38ee45dbb7` | `b32200576729de68562ad4e26005794c9aecb447` |
| run-6 frozen live tree (Sep 16 endgame) | `31ba93e92587eb66048634902ca92f0add4c460f` | `90cf346d7ea2b64d1246bbd5ebde6f102a303031` |
| run-6 reference (RUN6_SWITCH / contract v2) | `10e22966b0389b602539c39ead99464c9d9e2710` | `efa6c37f05aa0c23622dc1ddace2be7aece18d95` |
| run-6 reference (contract v2 / RUN_SUMMARY) | `580915de5a1c1e14837d27d66c389f3d70d0b61d` | `0e53f7b972d37ee684a6eee70ae6d8bafa4d451d` |
| main after PR #63 (Sep 21 ~18:40) | `8f71ed974c18ad597e53217d96956c54cfc85e45` | `9b0aba99f3c2b0c2dff104bdceeca9f1d847823e` |
| run-7 session-4 backtrack commit (PR #67) | `e956d2df2dd1c6187a266da7c48d072ec01a5514` | `50b05753c23eba1507e1b32569118361935b11e1` |
| main after PR #67 (Sep 26) | `3642a61eb9da5d16c7fa2dbdc8e5514ae0f37eba` | `b5b2ade89ce07a0b0be7a4e4f3f259fa184982ce` |
| main after PR #69 (Sep 29, the last state carried over) | `6627c7daed6eda4da6a68cd932ed349c5e3f1d14` | `b4bec03b365620e1024b03e985344c534b461ba4` |

`1a82724` (run-6 live HEAD on Sep 21) was never pushed and has no counterpart in either repository.
PR numbers quoted in the journal before 2026-09-29 refer to the previous repository's pull requests; PR
threads are GitHub objects, not git, and did not move.

**Live tree.** The running bot's checkout is re-pointed in place (same directory, so launchd jobs, the
venv, `state/` and `logs/` are untouched) by an operator-run script that refuses on a dirty tree, on more
than one bot instance, or during market hours, and aborts if the `investment_strategy/` tree id changes.
The bot is restarted from the re-pointed checkout through the control panel. The restart is logged in
`Todo-4.txt` under the freeze clause of contract v3.
