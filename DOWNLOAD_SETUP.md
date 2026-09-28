# Bloomkeeper beta42 download setup

Current candidate: **1.36.61 beta42/build13661**. Publication requires the exact release acceptance and hosted verification receipt. Earlier releases remain immutable.

Bloomkeeper, including the optimizer, is free while we evaluate whether it improves earnings over Darkbloom alone. Optimizer access has no scheduled expiration. Updating never turns automation on or resumes an explicitly paused plan. Choose Optimizer on separately when ready. Existing settings, history, access records and privacy choices are preserved.

Download the signed Apple Silicon DMG, drag Bloomkeeper into Applications and connect Darkbloom. Existing users can choose Check for Updates and approve installation. Let any model switch or warm-up finish first. Update the Mac, then reload its private phone dashboard.

Reporting and Manual controls remain available. Automatic model selection still requires current identity, resource and readiness checks. Optional cache cleanup keeps its existing narrow permission and frequency limits. Optional usage sharing starts off; problem reports are sent only with Send or opt-in automatic sending.

## 1. One-time Apple account setup

Use an existing paid Apple Developer membership, or [enroll with Apple](https://developer.apple.com/programs/enroll/). Standard membership is US$99 per year; local pricing may vary. The account owner completes Apple's identity, agreement and payment steps. Direct download uses Developer ID and notarization; no Mac App Store listing is needed.

**Recommended for this side-project beta: enroll as an Individual.** Apple also directs sole proprietors/single-person businesses to this route. Use your own legal name and an Apple Account with two-factor authentication. Individual enrollment does **not** require a D-U-N-S number or forming a company. Those are organization-enrollment requirements. Bloomkeeper remains the app's product name; do not put it in the personal first/last-name fields. If the current flow asks for organization documents, return to the entity-type choice and choose Individual. If an already-submitted enrollment cannot be edited, use Apple's enrollment support to resolve the account type rather than creating a company just for this step. [Apple's enrollment requirements](https://developer.apple.com/help/account/membership/program-enrollment), [explicit individual D-U-N-S exemption](https://developer.apple.com/support/D-U-N-S/).

An individual program member can use Developer ID and notarization for direct Mac distribution. If a separate legal organization is established later, Apple provides an individual-to-organization conversion request; it is not a prerequisite for this beta. [Developer ID](https://developer.apple.com/developer-id/), [membership conversion](https://developer.apple.com/help/account/membership/updating-your-account-information).

Create a **Developer ID Application** certificate, which signs this app and DMG. Apple's certificate page requires the Account Holder role. We do not need an Installer certificate for drag-and-drop installation. [Apple's certificate instructions](https://developer.apple.com/help/account/certificates/create-developer-id-certificates).

Full Xcode is not required for the account website route:

1. Open Keychain Access. Choose Certificate Assistant → Request a Certificate from a Certificate Authority. Enter your email and a name for the key, leave the CA email empty, and save the request to disk. The private key stays on this Mac. [Apple's CSR instructions](https://developer.apple.com/help/account/certificates/create-a-certificate-signing-request).
2. In Apple's Certificates, Identifiers & Profiles, add a Developer ID Application certificate using that request. Download and double-click the `.cer` to install it in the same Mac's Keychain.
3. Verify locally with `security find-identity -v -p codesigning`. A valid Developer ID Application entry must appear; an Apple Development certificate or certificate without its private key is insufficient.

Configure notarization once in your own Terminal:

```sh
xcrun notarytool store-credentials bloom-notary
```

Follow the interactive Apple ID / Team ID / app-specific-password prompts. This stores the credentials in Keychain and validates them. Do not paste passwords or export private keys into chat, the repository or a release bundle. No account credentials are collected by Bloomkeeper.

## 2. Build and verify the isolated candidate

Run these commands from the repository root. Set the identity to the exact fingerprint printed by the local identity check. The fingerprint and profile label are not passwords:

```sh
export BLOOM_SIGN_IDENTITY='YOUR_DEVELOPER_ID_CERTIFICATE_FINGERPRINT'
export BLOOM_NOTARY_PROFILE='bloom-notary'
python3 native/distribution.py preflight
```

Preflight reports only readiness and identity count. It checks the exact Developer ID identity, available Apple tools and Keychain profile authentication before building. Exit 2 means account setup is incomplete. It creates no certificates, changes no Keychain settings and submits no app.

Then build and check the release with a **new evidence directory** (previous evidence is never overwritten):

```sh
python3 native/release_check.py --build \
  --output /path/to/new-release-results \
  --node /path/to/node \
  --playwright /path/to/playwright/index.mjs \
  --chromium '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'
```

It builds and signs the isolated beta, runs local checks, and creates the DMG. Exit 2 is expected until notarization is finished. Packaging no longer uploads or waits indefinitely just because a notarization profile is configured.

Preserve every released DMG and its notarization receipt. For a future candidate, assign a new version/path and obtain source ownership/freeze before building; never overwrite an accepted release. Signed candidates have distinct installation wording; unsigned builds remain explicitly labeled developer previews. Never ship an unsigned preview as the polished download.

## 3. Submit once and finish the same Apple job

```sh
python3 native/distribution.py submit \
  --dmg '.build/releases/Bloom-NEW-VERSION-Apple-Silicon.dmg'
```

This uploads to Apple's notary service, saves the submission ID and exact SHA256 in an owner-only `.notarization.json` beside the DMG, then returns without waiting for processing. A saved receipt prevents a second upload or accidental rebuild over the submitted bytes. If the upload response is lost, the receipt records uncertainty: check `xcrun notarytool history --keychain-profile bloom-notary` locally and reconcile the original submission before retrying. Do not delete the receipt to force another upload.

After Apple processes the submission:

```sh
python3 native/distribution.py finish \
  --dmg '.build/releases/Bloom-NEW-VERSION-Apple-Silicon.dmg'
```

An in-progress result exits 3 and can be checked later with this same command. A rejection remains a failure: inspect the notary log locally and fix the reported issue before creating a new candidate. No security bypass is applied.

Only an accepted job can proceed to ticket stapling. The tool checks the artifact hash, ticket, code signature and Gatekeeper assessment, then writes the **post-stapling** checksum. Following Apple's container guidance, we sign the app and DMG, notarize the outer DMG and staple its ticket. [Apple's packaging workflow](https://developer.apple.com/documentation/xcode/packaging-mac-software-for-distribution).

Run `release_check.py` again **without `--build`**, with a new results directory. This checks the exact notarized DMG and app, including both Gatekeeper assessments and packaged runtime loading. Do not re-sign or rebuild the accepted artifact; that would invalidate the evidence. A new version gets a distinct artifact and receipt.

## 4. Make the actual download available

After these checks, the coordinator uploads the exact DMG, checksum and versioned welcome ZIP to the configured HTTPS download service, then updates the storefront. Use the current candidate handoff after its final acceptance checks. The local customer dashboard remains private; optional usage reports use the published consent-based endpoint. Keep versioned files; a newer release must not silently replace older bytes.

The download page should show Apple Silicon/macOS compatibility, version, file size, concise install steps, optional Tailscale phone requirements, a support destination, and the beta limitations. Free download does not need checkout. Support is available through Help & feedback, Slack or email. Optimizer access is included, without an activation step. Usage sharing starts off, is optional and links to the privacy page. Hosting is configured at `https://bloomformac.com/#release`; only the coordinator should switch the verified release links.

Download through a real browser on an independent Mac, open it with normal quarantine protections intact, drag the app into Applications and complete first launch without developer tools. Record the real result in the included Beta Test Kit. Browser download, physical Mac/phone behavior, sleep/wake and paid optimizer benefit cannot be established by local script checks alone.

Step 1 is complete when that real signed download/install path works. Independent installation, physical-phone and earnings comparisons remain to be observed.
