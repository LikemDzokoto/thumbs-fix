# Submitting thumbs-fix: operator steps

Steps to publish and submit, in order. The repository is `LikemDzokoto/thumbs-fix`.

## 1. Check the tree

```bash
python -m pytest -q
```

In WSL or Linux with Hermes installed (see docs/live-setup.md), from the repo root (on Windows, pass the `/mnt/c/...` path of the repo):

```bash
bash scripts/check-hermes.sh
```

Expect "check-hermes: ALL PASSED". `hermes plugins doctor . --ci` and `hermes plugins validate . --install-deps` are what the catalog CI runs.

## 2. Create the public repository and push

Catalog rule 5: the entry must come from the repository's owner. Create `LikemDzokoto/thumbs-fix` on GitHub as a public repo with no files. Then, from your checkout, add it as `origin` and push the branch as `main`:

```bash
git remote add origin https://github.com/LikemDzokoto/thumbs-fix.git
git push -u origin master:main
git rev-parse HEAD        # the full 40-character sha to pin
```

`plugin.yaml` and `__init__.py` sit at the repo root, so no `subdir` is needed. Check the pushed repo has no secrets and no `.pyc` files (catalog rule 17), and that the GitHub Actions run (`.github/workflows/test.yml`) is green. The plugin needs no repository secrets.

## 3. Pin the sha

1. Take the final sha with `git rev-parse HEAD` after your last push. The pin must be the commit that contains the final README.
2. Copy `docs/catalog/thumbs-fix.yaml` for the catalog repo and replace `REPLACE_WITH_40_HEX_COMMIT_SHA` with that sha, in quotes: `sha: "0123...cdef"`. Quoting keeps YAML from reading an all-digit or `e`-containing hex string as a number. The copy in this repo keeps the placeholder, because a file cannot contain the sha of its own commit.

## 4. Open the catalog pull request

Repository: `NousResearch/hermes-agent`. Path: `plugin-catalog/thumbs-fix.yaml`. The rules are in `plugin-catalog/README.md` there.

hermes-agent is a very large repository, so avoid a full clone: fork without cloning, then clone shallow and sparse so only the catalog folder is checked out. (Or skip git and add the file in the GitHub web UI on your fork.)

```bash
gh repo fork NousResearch/hermes-agent --clone=false
LOGIN=$(gh api user --jq .login)
git clone --depth 1 --filter=blob:none --sparse https://github.com/$LOGIN/hermes-agent.git
cd hermes-agent
git sparse-checkout set plugin-catalog
git checkout -b add-thumbs-fix
cp <path-to-this-repo>/docs/catalog/thumbs-fix.yaml plugin-catalog/thumbs-fix.yaml   # with the real sha filled in
```

**Commit with your GitHub noreply email.** hermes-agent CI has a check, "Check for unmapped contributor emails" (`.github/workflows/repo-guards.yml`, `scripts/ci/check_contributor_emails.py`), that fails any commit in the PR whose author email is not a GitHub noreply address. That address is `<id>+<login>@users.noreply.github.com`. Find it in GitHub under Settings, Emails (with "Keep my email addresses private" on, it is shown there), or print it:

```bash
gh api user --jq '"\(.id)+\(.login)@users.noreply.github.com"'
```

Commit with it for this one commit (nothing is saved to your git config), then push your branch:

```bash
git add plugin-catalog/thumbs-fix.yaml
git -c user.email=<id>+<login>@users.noreply.github.com commit -m "Add thumbs-fix to the plugin catalog"
git push -u origin add-thumbs-fix
```

The PR description is the README's Disclosure section (catalog rule 13). Make the file from the README, then open the PR:

```bash
sed -n '/^## Disclosure/,/^## How this differs/p' <path-to-this-repo>/README.md | sed '$d' > disclosure.md
gh pr create --repo NousResearch/hermes-agent --title "Add thumbs-fix plugin" --body-file disclosure.md
```

Keep `tier: community`. Check the hermes-agent contribution guide for any extra PR rules, and wait for the catalog check on the PR to go green.

## 5. Post the video

The demo video is `docs/media/thumbs-fix-demo.mp4`. Post it on X tagging @NousResearch, with the repo link and the PR link. Submit both links to the organisers before the deadline (planned as 2026-11-11 22:00 UTC; confirm the hour).

## 6. Later changes

After the entry is merged, a fix is a new catalog PR that changes `sha` and `version`.
