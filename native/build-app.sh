#!/bin/zsh
set -euo pipefail
project_dir="$(cd "$(dirname "$0")/.." && pwd)"
app_dir="${BLOOM_APP_OUTPUT:-$project_dir/.build/BloomGauge.app}"
channel="${BLOOM_RELEASE_CHANNEL:-local}"
identity="${BLOOM_SIGN_IDENTITY:--}"
executable=BloomDashboard
if [[ "$channel" == beta ]]; then executable=BloomDashboardBeta; fi
if [[ -L "$app_dir" || "$app_dir" != "$project_dir/.build/"* ]]; then
    echo 'Build only into a non-symlink staging path inside .build.' >&2; exit 1
fi
/usr/bin/python3 - "$app_dir" "$project_dir/.build" <<'PYCODE'
import pathlib,sys
if not pathlib.Path(sys.argv[1]).resolve().is_relative_to(pathlib.Path(sys.argv[2]).resolve()):
    raise SystemExit('Output must remain inside build staging.')
PYCODE
runtime_dir="$project_dir/.build/portable-runtime"
if [[ ! -x "$runtime_dir/python/bin/python3" || ! -f "$runtime_dir/provenance.json" ]]; then
    echo 'Prepare the portable runtime first: python3 native/prepare-runtime.py' >&2; exit 1
fi
if [[ ! -f "$project_dir/dist/local/index.html" ]]; then echo 'Build the local UI first.' >&2; exit 1; fi
sparkle_root="$project_dir/.build/sparkle"
/usr/bin/python3 "$project_dir/native/prepare-sparkle.py" --verify
sparkle_framework="$sparkle_root/Sparkle.xcframework/macos-arm64_x86_64/Sparkle.framework"
# Empty staging prevents stale private files or obsolete executable code surviving.
rm -rf "$app_dir"
mkdir -p "$app_dir/Contents/MacOS" "$app_dir/Contents/Resources/web" "$project_dir/.build/module-cache"
mkdir -p "$app_dir/Contents/Frameworks"
/usr/bin/ditto "$sparkle_framework" "$app_dir/Contents/Frameworks/Sparkle.framework"
# BloomGauge is not sandboxed. Official Sparkle guidance permits omitting these
# optional XPC services; keep its non-sandboxed Autoupdate and Updater helpers.
rm -rf "$app_dir/Contents/Frameworks/Sparkle.framework/Versions/B/XPCServices"
rm -f "$app_dir/Contents/Frameworks/Sparkle.framework/XPCServices"
resources="$app_dir/Contents/Resources"
/usr/bin/clang -O2 -mmacosx-version-min=14.0 -fobjc-arc -framework Foundation -framework IOKit "$project_dir/native/telemetry.m" -o "$project_dir/.build/telemetry"
/usr/bin/swiftc -O -target arm64-apple-macos14 -module-cache-path "$project_dir/.build/module-cache" -F "$app_dir/Contents/Frameworks" -framework Sparkle -Xlinker -rpath -Xlinker @executable_path/../Frameworks -framework AppKit -framework WebKit -framework ServiceManagement -framework UserNotifications "$project_dir/native/App.swift" "$project_dir/native/Updates.swift" "$project_dir/native/CachePermission.swift" "$project_dir/native/NotificationRelay.swift" -o "$app_dir/Contents/MacOS/$executable"
/usr/bin/swiftc -O -target arm64-apple-macos14 -module-cache-path "$project_dir/.build/module-cache" -framework AppKit -framework CoreImage "$project_dir/native/qr.swift" -o "$project_dir/.build/qr"
while IFS= read -r resource; do
    case "$resource" in
        ''|'#'*) continue ;;
        telemetry|qr) cp "$project_dir/.build/$resource" "$resources/" ;;
        push_vendor) cp -R "$runtime_dir/push_vendor" "$resources/" ;;
        *) [[ "$resource" != */* ]] || exit 1; cp "$project_dir/native/$resource" "$resources/" ;;
    esac
done < "$project_dir/native/bundle-resources.txt"
cp -R "$runtime_dir/python" "$resources/python"
cp -R "$runtime_dir/python-licenses" "$resources/python-licenses"
cp "$runtime_dir/provenance.json" "$resources/runtime-provenance.json"
cp "$project_dir/THIRD_PARTY_NOTICES.md" "$resources/"
cp "$sparkle_root/LICENSE" "$resources/SPARKLE_LICENSE"
cp "$sparkle_root/provenance.json" "$resources/sparkle-provenance.json"
cp -R "$project_dir/dist/local/." "$resources/web/"
chmod +x "$resources/"*.command
/usr/bin/swiftc -O -module-cache-path "$project_dir/.build/module-cache" -framework AppKit "$project_dir/native/app-icon.swift" -o "$project_dir/.build/app-icon"
mkdir -p "$project_dir/.build/Bloom.iconset"
"$project_dir/.build/app-icon" "$project_dir/.build/icon1024.png"
for size in 16 32 128 256 512; do
    /usr/bin/sips -z "$size" "$size" "$project_dir/.build/icon1024.png" --out "$project_dir/.build/Bloom.iconset/icon_$size"x"$size.png" >/dev/null
    double=$((size*2))
    /usr/bin/sips -z "$double" "$double" "$project_dir/.build/icon1024.png" --out "$project_dir/.build/Bloom.iconset/icon_$size"x"$size@2x.png" >/dev/null
done
/usr/bin/iconutil -c icns "$project_dir/.build/Bloom.iconset" -o "$resources/Bloom.icns"
# Save paths relative to the bundle; no developer home paths in the manifest.
/usr/bin/python3 "$project_dir/native/release-manifest.py" "$app_dir" "$channel"
# Sign nested Mach-O code inside out. No broad library-validation exemption.
sign_options=(--force --sign "$identity")
if [[ "$identity" != '-' ]]; then
    sign_options+=(--options runtime --timestamp)
else
    # An ad-hoc identity has no Team ID. Hardened library validation therefore
    # rejects even our bundled/re-signed Python extensions. Developer previews
    # use ordinary ad-hoc signatures; customer Developer ID builds keep hardened
    # runtime and same-team library validation, without exemption entitlements.
    sign_options+=(--options 0)
fi
while IFS= read -r -d '' executable; do
    if /usr/bin/file -b "$executable" | /usr/bin/grep -q 'Mach-O'; then
        /usr/bin/codesign "${sign_options[@]}" "$executable"
    fi
done < <(/usr/bin/find "$app_dir/Contents" -path "$app_dir/Contents/Frameworks" -prune -o -type f -print0)
# Explicit inside-out bundle signing preserves Sparkle helper metadata. Never
# rely on --deep signing, and never give the main app a library-validation bypass.
embedded_sparkle="$app_dir/Contents/Frameworks/Sparkle.framework"
/usr/bin/codesign "${sign_options[@]}" --preserve-metadata=identifier,entitlements "$embedded_sparkle/Versions/B/Autoupdate"
/usr/bin/codesign "${sign_options[@]}" --preserve-metadata=identifier,entitlements "$embedded_sparkle/Versions/B/Updater.app"
/usr/bin/codesign "${sign_options[@]}" --preserve-metadata=identifier,entitlements "$embedded_sparkle"
/usr/bin/codesign "${sign_options[@]}" "$app_dir"
/usr/bin/codesign --verify --deep --strict "$app_dir"
echo "$app_dir"
