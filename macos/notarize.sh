#!/usr/bin/env bash
# Notarize one artifact and staple the ticket to it.
#
#   NOTARY_APPLE_ID=… NOTARY_TEAM_ID=… NOTARY_PASSWORD=… ./notarize.sh <path>
#
# Exit codes are the interface, because "shipped without a ticket" is a real
# outcome and not an error:
#     0   notarized and stapled
#    20   Apple did not answer in time and ALLOW_UNNOTARIZED said to ship anyway
#   else  genuinely broken
#
# Both the .app and the .dmg go through here — the app's ticket does not travel
# inside the image, and the image's does not survive being unpacked.
set -uo pipefail

TARGET="${1:?usage: notarize.sh <path to .app or .dmg>}"
: "${NOTARY_APPLE_ID:?}" "${NOTARY_TEAM_ID:?}" "${NOTARY_PASSWORD:?}"

creds=(--apple-id "$NOTARY_APPLE_ID" --team-id "$NOTARY_TEAM_ID" --password "$NOTARY_PASSWORD")

# A .app has to be archived first; a .dmg is already one file and is submitted
# as it is. Zipping a .dmg would notarize the zip, and the ticket would then
# staple to nothing anyone ever opens.
case "$TARGET" in
  *.dmg) upload="$TARGET" ;;
  *)     upload="$(mktemp -d)/upload.zip"
         ditto -c -k --keepParent "$TARGET" "$upload" ;;
esac

id="$(xcrun notarytool submit "$upload" "${creds[@]}" --output-format json \
      | python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])')" || exit 1
[[ -n "$id" ]] || { echo "!!  no submission id came back" >&2; exit 1; }
echo "::notice::notarization submission $id for $(basename "$TARGET")"

# No point buying two hours of patience when the answer is already "ship it
# either way" — give the queue a fair chance and move on.
timeout=2h
[[ "${ALLOW_UNNOTARIZED:-}" == "true" ]] && timeout=20m

if xcrun notarytool wait "$id" "${creds[@]}" --timeout "$timeout"; then
  xcrun stapler staple "$TARGET" || exit 1
  exit 0
fi

xcrun notarytool log "$id" "${creds[@]}" || true
if [[ "${ALLOW_UNNOTARIZED:-}" == "true" ]]; then
  echo "::warning::shipping $(basename "$TARGET") unnotarized — submission $id"
  exit 20
fi
echo "::error::notarization did not finish for $id"
exit 1
