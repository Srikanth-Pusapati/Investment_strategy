# Commit-SHA map for the repository move (recorded 2026-09-26)

The project is moving from the previous owner's GitHub account to the author's own. The moved history
was produced from a fresh bare clone of the current repository with `git filter-repo` and ONE combined
filter: (1) a mailmap that attributes the 67 GitHub web-merge commits (author = the previous account,
committer = `GitHub <noreply@github.com>`) to Srikanth Babu Pusapati, the author of the other 199
commits; (2) `Co-Authored-By: Claude …` and `Claude-Session: …` trailer lines removed from commit
messages; (3) `Merge pull request #N from <org>/<branch>` subjects rewritten to `Merge branch '<branch>'
into main` and the org prefix removed elsewhere in messages. Nothing else changed: no file content, no
dates, no commit order. Result: 266 commits, one author, one committer, zero trailers, zero org references.

**Invariant that matters for the evaluation contracts:** commit-message and identity rewrites do not
touch tree objects, so every `git ls-tree` fingerprint is unchanged. Verified on the moved `main`:
`investment_strategy/` tree id = `2a6ec14778beafd22d402faae80c82eafb0353b0`, the frozen run-7 id pinned in
`pre-final-test-run-7/EVAL_CONTRACT.md`. Commit SHAs DO change, so the SHAs pinned in the contracts and
switch docs map as follows (old = current repository, new = moved repository):

| What | Old SHA | New SHA |
|---|---|---|
| run-7 frozen merge (contract v3 Config row; PR #60 merge on feature/preview) | `eec972753ff71a3998a0df6911abf6c834cde0cf` | `07ae93d6194f4813f64f53455d692a22a649c167` |
| `origin/main` at the run-7 switch (identical tree) | `a0507a098cf2c69f49139caa57665739ca9ff8e9` | `936f4d06fade0b010ba8605573a7988c5b725dca` |
| main after PR #62 (live tree moved to main, Sep 21 evening) | `5516f35dd70c1c83e3fe5002ffcbf7e286144520` | `b83fac946473d774b4d506f9fcd42adc5a763e6f` |
| contract v3 pre-registration commit (PR #62) | `3e9cc17d138060fb513aab7e6ce7be38ee45dbb7` | `951e685bf9846bf09e9685708c3d4e4e32332ad4` |
| run-6 frozen live tree (Sep 16 endgame) | `31ba93e92587eb66048634902ca92f0add4c460f` | `1de70b13ab6d1b7257f05f28c3267875160f9630` |
| run-6 reference (RUN6_SWITCH / contract v2) | `10e22966b0389b602539c39ead99464c9d9e2710` | `426cdf81bb211c42c995ce9ac5e438bb7e10efad` |
| run-6 reference (contract v2 / RUN_SUMMARY) | `580915de5a1c1e14837d27d66c389f3d70d0b61d` | `ada201f29a039e3733536df2c90730a55a7c0b26` |
| main after PR #63 (Sep 21 ~18:40) | `8f71ed974c18ad597e53217d96956c54cfc85e45` | `4c2ff96477b234b5d140cf132dad420842670196` |
| run-7 session-4 backtrack commit (PR #67) | `e956d2df2dd1c6187a266da7c48d072ec01a5514` | `1cb165361755ad0563b6209fbda48bc8977af93e` |
| main after PR #67 (Sep 26) | `3642a61eb9da5d16c7fa2dbdc8e5514ae0f37eba` | `e7f74ee7ad6833eb05ec9c73b03d70780ef47c15` |

`1a82724` (run-6 live HEAD on Sep 21) was never pushed and has no counterpart in either repository.
A pinned SHA's new value depends only on its own ancestry and the filter, not on later commits, so the
table stays valid when the mirror is rebuilt after further commits land here; the rebuild script re-derives
the table and reports any drift.

**Live tree.** The running bot's checkout keeps its current `origin` through the run-7 close (Oct 20,
or Oct 27 if extended). The freeze forbids nothing about remotes, but re-pointing a shared working tree
mid-window means a history reset on the directory the bot and several sessions use; that waits for the
window to close. After the close: fresh clone of the moved repository, copy `.env` and `state/`, verify
`git rev-parse HEAD:investment_strategy`, then start the bot from the new clone. Until then the moved
repository is a snapshot refreshed by re-running the rebuild.

**Not done here, by decision of the owner:** the secrets scan of the full history (gitleaks 8.30,
values redacted) reported exactly one finding — `ANTHROPIC_API_KEY` in
`pre-final-test-run-4/analysis/config.json` line 84 (commit `e2a70264`, June/July run-4 analysis). Whether it
is a live key or a placeholder is for the owner to check; if live, rotate it and remove the file from the
moved history (`git filter-repo --path runs/pre-final-test-run-4/analysis/config.json --invert-paths`)
before the repository is made public. `.env` and `state/` were never tracked in any commit.
