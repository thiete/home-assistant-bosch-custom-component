# 2026-07 fixes: rationale, root causes, and how to re-verify them

This document records what was changed on the `working/all-fixes` branch, why,
and — most importantly — **how the root causes were actually confirmed**, so
that a future maintainer facing a new Home Assistant (HA) breaking change can
repeat the same verification process instead of guessing.

The upstream project (`bosch-thermostat/home-assistant-bosch-custom-component`)
has an inactive maintainer: the last tagged release is `v0.28.2`
(2025-08-01), but master had accumulated unreleased fixes and several open
issues describe breakage caused by HA core moving on without a corresponding
release here. This work was done by triaging the open issues, separating
"HA broke us" bugs from device-specific/feature-request issues, and fixing
the former.

## Versions in effect when this was written

| Component | Version | Notes |
|---|---|---|
| Home Assistant Core | 2026.7.2 | Installed fresh via pip for verification; matches the versions reporters used (2026.4.0–2026.6.2) |
| Python | 3.14.3 | System Python; HA test venv also used 3.14 |
| `bosch-thermostat-client` (PyPI) | `v0.28.2` | Pinned in `manifest.json` before this work; unchanged by our two fixes |
| `bosch-thermostat-client` (EasyControl fork) | `Cerbrus/bosch-thermostat-client-python@fix/easycontrol-model-detection` | Pulled in only by the EasyControl/CT200 merge, see below |
| `pytest-homeassistant-custom-component` | 0.13.346 | Used only for local verification, not a project dependency |
| This integration, before | `0.28.2` (manifest version, last bumped alongside the `v0.28.2` tag) | |
| This integration, after | `0.29.0` | Bumped on `working/all-fixes` — see [Version bump](#version-bump) |

Repo state at the time: `master` @ `684ab40` (2026-05-12).

## Fix 1 — blocking gateway construction (branch `fix/blocking-gateway-init`)

**Issues:** #570, #556
**File:** [`custom_components/bosch/__init__.py`](custom_components/bosch/__init__.py), `BoschGatewayEntry.async_init`
**Commit:** `ea0ef7c`

### Symptom
```
Detected blocking call to load_default_certs ... inside the event loop by
custom integration 'bosch' at custom_components/bosch/__init__.py, line 239:
self.gateway = BoschGateway(...)
```
logged on every startup/reload (#570), and suspected as a contributor to HA
hanging/crashing every few days (#556) since a blocked event loop can cascade.

### Root cause
`BoschGateway(...)` (and, after the EasyControl merge, `Oauth2Gateway(...)`)
builds an `ssl.SSLContext` synchronously inside `__init__`, which calls
`load_default_certs()` — a real blocking syscall. HA's own blocking-call
detector (`homeassistant.util.loop`) flags any such call made directly on the
event loop; see
[developers.home-assistant.io/docs/asyncio_blocking_operations](https://developers.home-assistant.io/docs/asyncio_blocking_operations/#load_default_certs)
(this URL is the one HA itself prints in the log message).

### Fix
Wrap gateway construction in `hass.async_add_executor_job(...)`.

### Verification status
**Reasoned, not live-tested.** There is no physical Bosch gateway available
to connect through in this environment. The fix is mechanically
straightforward and matches HA's own documented remediation for this class
of warning, but it has not been confirmed against a real device connection.

## Fix 2 — entity name blanked to `None` on incomplete poll data (branch `fix/entity-has-name`)

**Issues:** #562, #543 ("All sensor entities now show the same friendly_name
'Bosch sensors'")
**File:** [`custom_components/bosch/sensor/base.py`](custom_components/bosch/sensor/base.py), `BoschBaseSensor.async_update.check_name`
**Commit:** `b52ed7c`

### Investigation (read this before touching entity naming again)

The first hypothesis was wrong, and it's worth recording why, since it's an
easy trap to fall into again. HA's `Entity` class (as of HA core 2026.7.2,
`homeassistant/helpers/entity.py`) uses a `CachedProperties` metaclass that
backs every `_attr_*` field with a private `__attr_*` instance attribute and
generates a `property` for it. Overriding `name` directly (as this
integration's `BoschEntity.name` did) is a known anti-pattern HA is steering
integrations away from, and the metaclass's error message for an unset
backing attribute is literally
`AttributeError: '<Class>' object has no attribute '__attr_state_class'` —
which is *exactly* the wording in issue #560. That similarity is what
initially suggested the naming bug and #560 shared a cause, and led to a
first (wrong) attempt at fixing #562/#543 by mirroring `self._name` through a
`_attr_name` property.

That fix was reverted after empirical testing showed it didn't reproduce or
resolve anything, because:

- #560's actual crash is unrelated to entity naming (see
  [Already-fixed-but-unreleased](#already-fixed-but-unreleased-issues) below)
  — it's a separate, already-fixed bug in the same file.
- The real #562/#543 bug was found by installing HA core 2026.7.2 in an
  isolated venv (`pip install homeassistant==2026.7.2
  pytest-homeassistant-custom-component`) and reading the actual installed
  source, then reproducing the bug against the real (unmodified)
  `BoschSensor`/`BoschBaseSensor` classes using
  `pytest-homeassistant-custom-component`'s `hass` fixture, a `MockConfigEntry`,
  and a real `EntityPlatform.async_add_entities([...])` call — not a unit test
  in isolation, since the bug only appears in HA's actual friendly-name
  computation.

### Root cause

`homeassistant/helpers/entity_registry.py:_async_get_full_entity_name`
computes an entity's displayed name by joining `(area_name, device_name,
entity_name)`, skipping falsy parts. If `entity_name` (derived from
`entity.name`) is falsy, the result silently collapses to just the device
name. `BoschSensor` and `BoschBinarySensor` both hard-code
`_domain_name = "Sensors"` / `device_name = "Bosch sensors"`, so every
generic sensor shares one device — meaning if any one of them loses its
name, it displays as literally "Bosch sensors".

The actual code bug, in `check_name()`:
```python
def check_name():
    if data.get(NAME, "") != self._name:
        self._name = data.get(NAME)   # <-- no default here
```
Any poll response missing the `"name"` key — normal for raw endpoints like
`/system/sensors/temperatures/outdoor_t1`, which only return `id`/`value`/
`unit` — compares `""` (the get-with-default) against the current name,
finds them different, and then reassigns using `data.get(NAME)` **without**
a default, setting `self._name = None`. Once `entity.name` is `None`, the
friendly-name collapse described above kicks in.

### Fix
```python
def check_name():
    new_name = data.get(NAME)
    if new_name and new_name != self._name:
        self._name = new_name
```
Only updates the name when the response actually contains a non-empty new
one; never blanks it when the field is simply absent.

### Verification status
**Empirically confirmed**, not just reasoned. A throwaway regression test
(not committed — see [Reproducing this test](#reproducing-this-test) to
rebuild it) did the following against real installed HA 2026.7.2 with the
actual unmodified integration classes:
1. Registered a `BoschSensor` under a `MockConfigEntry` + real device.
2. Confirmed initial `friendly_name` = `"Bosch sensors Outdoor temperature"`.
3. Simulated a poll response missing `"name"`.
4. On the pre-fix code: `friendly_name` collapsed to exactly `"Bosch
   sensors"` — reproducing #562/#543 verbatim.
5. On the post-fix code: `friendly_name` stayed `"Bosch sensors Outdoor
   temperature"`.

Only `BoschSensor` and `CircuitSensor` are affected (they inherit
`BoschBaseSensor.async_update` as-is). `RecordingSensor`, `EnergySensor`,
and `NotificationSensor` override `async_update()` entirely and never call
`check_name()`.

### Important: an unmerged upstream fix already exists for this bug

`origin/copilot/fix-sensor-name-display` (commit `a6172f0`, 2026-04-03,
co-authored by the actual maintainer `pszafer`, via a GitHub Copilot coding
agent run) independently arrives at the same root-cause fix
(`self._attr_name = data.get(NAME, self._attr_name)`), but goes further: it
removes `BoschEntity`'s overridden `name` property entirely and renames
`self._name` to `self._attr_name` throughout the whole codebase (sensors,
switches, select, number, climate, water_heater), adopting the modern
HA-recommended `_attr_name` pattern everywhere rather than just in this one
method.

**That branch is unmerged as of this writing** (`git merge-base
--is-ancestor a6172f0 master` → not an ancestor). Before reapplying or
extending this document's narrower fix in the future, check whether
`copilot/fix-sensor-name-display` (or a descendant of it) has since been
merged upstream — if so, this fix is redundant and the broader refactor
should be preferred instead.

### Reproducing this test

```bash
python3 -m venv /tmp/ha_test_env
/tmp/ha_test_env/bin/pip install homeassistant pytest-homeassistant-custom-component
/tmp/ha_test_env/bin/pip install "bosch-thermostat-client==0.28.2"
```
Then write a test using `pytest_homeassistant_custom_component.common.hass`
and `MockConfigEntry`, import the real classes from
`custom_components.bosch.sensor.bosch` (with the repo root on `sys.path`),
construct a fake `bosch_object` stub (only needs `.parent_id`, `.id`,
`.device_class`, `.state_class`, `.entity_category`, `.get_property()`,
`.update_initialized`, `.state`, `.state_message`, `.path`), add it via a
real `EntityPlatform` with `platform.config_entry` set to the mock entry
(so `device_info` resolves to a real device), and inspect
`hass.states.get(entity.entity_id).attributes["friendly_name"]` before/after
an update. Note `pytest.ini` needs `asyncio_mode = auto` for
`pytest-homeassistant-custom-component` to work with recent `pytest`/
`pytest-asyncio`, and the entity constructor needs `is_enabled=True` or it
registers disabled and never writes a live state.

## EasyControl / CT200 POINTT OAuth2 support (branch `merge/cerbrus-ct200-oauth`)

**Issues:** #554, #535 ("Cannot connect to CT200" / "Unknown model")
**Merged from:** [`Cerbrus/home-assistant-bosch-custom-component`](https://github.com/Cerbrus/home-assistant-bosch-custom-component)
(commits `96d147f`, `480764f`), fetched ad hoc via
`git fetch https://github.com/Cerbrus/home-assistant-bosch-custom-component.git master:refs/remotes/cerbrus-fork/master`
— **this is not a configured remote**, just a locally fetched ref; re-fetch
with the same command if it's gone.

### Background
Upstream PR [#536](https://github.com/bosch-thermostat/home-assistant-bosch-custom-component/pull/536)
(unmerged) added support for Bosch's newer POINTT cloud API / SingleKey ID
OAuth2 login, needed for CT200/EasyControl gateways that the older
HTTP/XMPP auth doesn't support. It shipped with four bugs, documented in
[`Cerbrus/hassio-issue-bosch-thermostat`](https://github.com/Cerbrus/hassio-issue-bosch-thermostat):
1. The OAuth redirect target is a mobile-app custom URI scheme
   (`com.bosch.tt.dashtt.pointt://`) that desktop browsers can't follow.
2. The POINTT API requires devices to be "claimed" to the account first;
   the original PR skipped this step.
3. Model detection called `exit(1)` instead of raising, and never checked
   `productID` (the only identifier POINTT API devices expose).
4. Wrong circuit types (AC instead of EasyControl's HC/DHW/ZN), causing a
   `KeyError` during setup.

Cerbrus's fork fixes all four. It also depends on a separate fork of the
client library — `Cerbrus/bosch-thermostat-client-python@fix/easycontrol-model-detection`
— referenced directly via a `git+https://` URL in `manifest.json`, **not** a
versioned PyPI release.

### Merge conflict resolved
Both this merge and Fix 1 rewrote the same gateway-construction block in
`async_init`. Resolved by keeping the executor-wrap applied uniformly to
*both* the new `Oauth2Gateway` path and the original `BoschGateway` path —
it's cheap insurance, and it isn't confirmed whether `Oauth2Gateway.__init__`
does the same synchronous SSL work.

### External dependency this doesn't fix
Completing the OAuth flow still requires a one-time, external helper to
capture the browser's redirect to `com.bosch.tt.dashtt.pointt://`, since
desktop browsers can't follow it on their own. Cerbrus's `oauth-helper/`
(Windows-only, registers the scheme via the Windows Registry) fills that
role; a macOS port was attempted but the direct-executable-in-a-.app-bundle
approach was blocked by Gatekeeper (`spctl` rejects an unsigned bundle even
after ad-hoc `codesign --sign -`) and was not completed. This is entirely
outside the HA integration's code — it only matters during initial
EasyControl device setup, not for ongoing operation.

## Combined branch (`working/all-fixes`)

Merge order: `master` → `fix/blocking-gateway-init` → `fix/entity-has-name`
→ `cerbrus-fork/master`. One manual conflict (described above), otherwise
clean.

### Version bump
`manifest.json` version bumped `0.28.2` → `0.29.0` (commit `b9fdadd`).
Reasons:
- Semantically reflects a real feature addition (EasyControl OAuth2), not
  just a patch.
- Forces HA to reinstall `bosch-thermostat-client` from the new git source
  rather than assuming a cached `0.28.2` PyPI install still satisfies the
  requirement. **If deploying to an instance that previously ran this
  integration**, also clear the dependency cache manually —
  `find /config/deps -name "bosch*thermostat*" -exec rm -rf {} +` — before
  restarting, since HA's requirement-install caching can be keyed loosely
  enough that this doesn't happen automatically.

## Already-fixed-but-unreleased issues

Not fixed by this work because they were already fixed on `master` — just
never released. Master's last tag is `v0.28.2` (2025-08-01); commit
`320853a` ("Fix water_heater false error, add mean_type/unit_class to
statistics, fix temperature state_class", 2026-03-28) already resolves:
- **#560** — `AttributeError` on `_attr_state_class` crashing every ~30s
  (the pre-fix code compared `self._attr_state_class == "measurement"`
  with no guard for it never being set at all).
- **#550, #549** — `async_add_external_statistics` missing `mean_type`,
  which HA warned "will stop working in Home Assistant 2026.11".
- **#515** (partially) — `state_class 'total'` invalid for
  `device_class: temperature`.

These need a release cut (version bump + tag), not new code. The `0.29.0`
bump on `working/all-fixes` incidentally carries them forward too, since it
builds on top of `master` @ `684ab40`, which already includes `320853a`.

## Issues set aside (not HA-breakage, out of scope for this work)

Device-specific connectivity/credential issues, and feature requests, were
explicitly excluded from this pass: #572, #566, #559, #557, #554 (device
side, separate from the OAuth support merged above), #551 (`wontfix`),
#548, #538, #528 (`wontfix`), #522, #518, #517, #462, #456, #377, #335,
#288. #561 was left as ambiguous — "Your devices: null" could be an HA-side
regression or a Bosch cloud-side change, and wasn't investigated further.

## Reference: HA internals this analysis depended on

If a future HA version changes entity-naming or blocking-call behavior
again, these are the places to re-read first (paths as of HA core 2026.7.2):
- `homeassistant/helpers/entity.py` — `CachedProperties` metaclass (backs
  every `_attr_*` with `__attr_*`), `Entity._name_internal`,
  `Entity.suggested_object_id`, `Entity._async_calculate_state` (calls
  `er.async_get_full_entity_name`).
- `homeassistant/helpers/entity_registry.py` —
  `async_get_full_entity_name` / `_async_get_full_entity_name` (the
  device/area/entity name join logic that collapses to device name on a
  falsy entity name).
- `homeassistant/helpers/entity_platform.py` — `_async_add_entity` (where
  `entity.name` is read for `original_name` at registration time).
- [developers.home-assistant.io/docs/asyncio_blocking_operations](https://developers.home-assistant.io/docs/asyncio_blocking_operations/) —
  HA's own documentation for the blocking-call detector class of bug.

The general lesson from this pass: **don't trust a plausible-looking HA
internals theory without reproducing the actual bug against the real,
currently-installed HA version.** `pip install homeassistant==<version>
pytest-homeassistant-custom-component` in a throwaway venv, then exercising
the real integration classes through `pytest-homeassistant-custom-component`'s
fixtures, is fast enough to do this every time and caught a wrong hypothesis
before it shipped.
