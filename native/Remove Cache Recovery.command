#!/bin/sh
set -eu
/usr/bin/sudo /bin/sh "$(dirname "$0")/remove-cache-recovery.sh"
printf "\nPress Return to close this window."
read -r answer
