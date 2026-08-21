# Sourced by push.sh / demo.sh on the Pi. Pushes a WebP to the local
# tronbyt-server using its Tidbyt-compatible API (stock pixlet's `push`
# hardcodes api.tidbyt.com, so we curl instead).
#
#   push_webp FILE [INSTALLATION_ID] [background|foreground]
#
# background + installation id = persistent "pushed" app whose image is
# replaced on every push (what the live battery face uses); foreground with no
# id = shown immediately, once (demo tour frames).
TRONBYT=http://localhost:8000
TRONBYT_DEVICE=tidbyt
TRONBYT_KEY_FILE=/opt/stack/tidbyt/tronbyt_key   # mode 600, never in the repo
# Second display: the 128x64 tronbyt-wide (its firmware 2x-upscales our 64x32
# frames until a native wide face exists — see ~/projects/tronbyt-wide).
TRONBYT_WIDE_DEVICE=wide
TRONBYT_WIDE_KEY_FILE=/opt/stack/tidbyt/tronbyt_wide_key

push_webp() {
  local file=$1 inst=${2:-} mode=${3:-background}
  local device=${4:-$TRONBYT_DEVICE} keyfile=${5:-$TRONBYT_KEY_FILE}
  python3 - "$file" "$inst" "$mode" <<'PY' \
    | curl -sf -m 15 -X POST \
        -H "Authorization: Bearer $(cat "$keyfile")" \
        -H 'Content-Type: application/json' --data-binary @- \
        "$TRONBYT/v0/devices/$device/push" > /dev/null
import base64, json, sys
f, inst, mode = sys.argv[1:4]
body = {"image": base64.b64encode(open(f, "rb").read()).decode(),
        "background": mode == "background"}
if inst:
    body["installationID"] = inst
print(json.dumps(body))
PY
}

# Same frame to the wide display; best-effort (never fail the Tidbyt push).
push_webp_wide() {
  [ -f "$TRONBYT_WIDE_KEY_FILE" ] && \
    push_webp "$1" "${2:-}" "${3:-background}" "$TRONBYT_WIDE_DEVICE" "$TRONBYT_WIDE_KEY_FILE" || true
}
