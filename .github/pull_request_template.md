## What and why

<!-- What changes, and the problem it solves. Link the issue if there is one. -->

## What it touches

<!-- Tick everything the change can affect. -->

- [ ] Camera entities and live view (go2rtc registration, WebRTC, the HLS dialog)
- [ ] Recordings, snapshots or the media browser
- [ ] Config flow, options or their text (`strings.json`, `translations/`)
- [ ] Camera controls and sensors (switches, selects, numbers, PTZ, siren)
- [ ] Motion events and notifications
- [ ] The camera library requirement in `manifest.json`
- [ ] Nothing above: docs, CI or tooling only

Camera models tested on:

Home Assistant version tested on:

## How it was tested

- [ ] `pytest tests/` passes
- [ ] `ruff check .` passes, and `ruff format --check` on the files you changed
- [ ] hassfest passes (CI, or locally)
- [ ] Tested in a running Home Assistant (describe below)

<!-- A single clean session proves little on these cameras. Say how many
     views, recordings or restarts you ran, and give numbers - load times,
     durations, error counts, before and after - rather than "works for me". -->

## Checklist

- [ ] The title is a conventional commit (`fix(camera): ...`, `feat(options): ...`, `docs: ...`)
- [ ] `CHANGELOG.md` has an entry under `[Unreleased]` for anything a user can notice
- [ ] `strings.json` and `translations/en.json` match, and descriptions contain no `{` or `}`
- [ ] A raised `python-aidot-cameras` floor in `manifest.json` names a version already on PyPI
- [ ] New options default to today's behaviour, or the change of default is called out above
- [ ] Code, tests, logs and this description contain no credentials, tokens, account emails, device ids, MAC or IP addresses, or camera names
