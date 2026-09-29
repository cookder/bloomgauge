# Security

BloomGauge reads your Darkbloom login token, can change which model your provider serves, and can optionally be granted permission to run `purge`. Please report security problems privately.

## Reporting a vulnerability

Use GitHub's **Report a vulnerability** button on this repository's Security tab, or email support@bloomgauge.io. Please don't open a public issue.

Include the affected version, the impact, and the steps to reproduce. Leave out any real credentials or account details.

## Scope

In scope:
- the local dashboard (`127.0.0.1:8765`) and phone listener (`127.0.0.1:8766`), including origin and host checks
- the Tailscale phone-access identity checks
- handling of `~/.darkbloom/auth_token`
- provider commands issued by the optimizer and manual controls
- the optional cache-recovery sudoers rule (`native/enable-cache-recovery.sh`)
- the update feed and code signing

Out of scope: Darkbloom's own provider, coordinator and console. Report those to Darkbloom.
