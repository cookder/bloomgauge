#!/bin/zsh
set -euo pipefail
project_dir="$(cd "$(dirname "$0")/.." && pwd)"
app="$project_dir/.build/beta/Bloomkeeper Beta.app"
image="$project_dir/.build/releases/Bloomkeeper-1.36.57-beta38-Apple-Silicon.dmg"
identity="${BLOOM_SIGN_IDENTITY:--}"
if [[ -e "${image%.dmg}.notarization.json" || -L "${image%.dmg}.notarization.json" ]]; then
    echo 'This artifact has a notarization receipt. Finish that submission; do not overwrite its bytes.' >&2; exit 1
fi
if [[ "$identity" != '-' || -n "${BLOOM_NOTARY_PROFILE:-}" ]]; then
    /usr/bin/python3 "$project_dir/native/distribution.py" preflight
fi
export BLOOM_APP_OUTPUT="$app" BLOOM_RELEASE_CHANNEL=beta
"$project_dir/native/build-app.sh"
stage="$project_dir/.build/beta/disk-image"
rm -rf "$stage"
mkdir -p "$stage"
/usr/bin/ditto "$app" "$stage/Bloomkeeper Beta.app"
[[ -L "$stage/Applications" ]] || ln -s /Applications "$stage/Applications"
notes_options=()
[[ "$identity" == '-' ]] || notes_options+=(--signed)
/usr/bin/python3 "$project_dir/native/distribution.py" installer-notes \
    --source "$project_dir/native/BETA_README.txt" "${notes_options[@]}" > "$stage/Read Me First.txt"
mkdir -p "$stage/Beta Test Kit"
cp "$project_dir/native/beta/TESTER_GUIDE.md" "$project_dir/native/beta/RESULT_TEMPLATE.json" "$project_dir/native/beta/EARNINGS_TEMPLATE.csv" "$stage/Beta Test Kit/"
output="$project_dir/.build/releases"
mkdir -p "$output"
/usr/bin/hdiutil create -ov -volname 'Bloomkeeper Beta' -srcfolder "$stage" -format UDZO "$image"
if [[ "$identity" != '-' ]]; then
    /usr/bin/codesign --force --timestamp --sign "$identity" --identifier local.bloom.dashboard.beta.dmg "$image"
    /usr/bin/codesign --verify --strict "$image"
fi
(cd "$output" && /usr/bin/shasum -a 256 "${image:t}" > "${image:t}.sha256")
echo "$image"
echo 'Package created. Notarization is a separate submit/finish step; see DOWNLOAD_SETUP.md.'
