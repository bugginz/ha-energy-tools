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

push_webp() {
  local file=$1 inst=${2:-} mode=${3:-background}
  python3 - "$file" "$inst" "$mode" <<'PY' \
    | curl -sf -m 15 -X POST \
        -H "Authorization: Bearer $(cat "$TRONBYT_KEY_FILE")" \
        -H 'Content-Type: application/json' --data-binary @- \
        "$TRONBYT/v0/devices/$TRONBYT_DEVICE/push" > /dev/null
import base64, json, sys
f, inst, mode = sys.argv[1:4]
body = {"image": base64.b64encode(open(f, "rb").read()).decode(),
        "background": mode == "background"}
if inst:
    body["installationID"] = inst
print(json.dumps(body))
PY
}
