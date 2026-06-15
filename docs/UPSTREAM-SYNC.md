# Syncing with upstream (AiDot-Development-Team/hass-AiDot)

This integration is a heavily-diverged fork: it adds camera support (live
WebRTC/SDES streaming, motion events, cloud-clip playback, two-way audio, PTZ,
opt-in LAN control) on top of upstream's lights-only integration, and its
history has been rewritten. It therefore has **no common git ancestor** with
upstream, so `git merge upstream/main` will not work (unrelated histories).
Sync individual changes by **cherry-pick** instead.

Status at last check: we are a superset of upstream and not missing any of its
changes (upstream's most recent feature, the reauth flow, is already present
here; its other recent commits are version bumps).

## One-time setup

```bash
git remote add upstream https://github.com/AiDot-Development-Team/hass-AiDot.git
```

## Pulling a fix FROM upstream

```bash
git fetch upstream
git log upstream/main --oneline -20           # scan upstream history
git show <commit-sha>                          # inspect a fix
git cherry-pick <commit-sha>                   # apply it
#  …resolve conflicts (our config_flow.py / coordinator.py are refactored and
#    extended well beyond upstream's, so most fixes need hand-application)
```

When a fix doesn't map cleanly, re-implement it and reference the upstream SHA
in the commit message.

## Pushing a fix TO upstream

```bash
git fetch upstream
git switch -c fix-xyz upstream/main           # branch off upstream's tree
#  …re-implement the fix against upstream's layout, commit…
git push <your-upstream-fork> fix-xyz         # then open a PR to AiDot-Development-Team
```

## Do not

- `git merge upstream/main` — no common ancestor; it would conflict on nearly
  every file.
