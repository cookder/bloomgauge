#!/bin/sh
set -eu
/usr/bin/sudo /bin/sh "$(dirname "$0")/enable-cache-recovery.sh"
printf "\nPress Return to close this window."
read -r answer
