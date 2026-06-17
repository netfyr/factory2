#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

usage() {
    cat <<'EOF'
Usage: run_container.sh [OPTIONS] <specs-dir> [-- factory-options...]

Run the AI software factory inside a container.

Arguments:
  specs-dir                Directory containing .md user story specifications

Options:
  -o, --output DIR         Output directory for project and state (default: ./factory-output)
  -p, --profile NAME       Language/toolchain profile (default: rust)
  -i, --image NAME         Base image name prefix (default: factory2)
  -b, --build              Force rebuild all container image layers
  -R, --runtime CMD        Container runtime: docker or podman (default: auto-detect)
  -h, --help               Show this help

Everything after -- is passed through to the factory.

Factory options:
  -j, --parallel N         Max parallel story pipelines (default: 1)
  -r, --retries N          Max verify fix attempts per story (default: 3)
  --strong-model MODEL     Model for plan phase (default: claude-opus-4-6)
  --default-model MODEL    Model for understand, implement, write-tests, verify (default: claude-sonnet-4-6)
  --fast-model MODEL       Model for dep analysis, summary, commit messages (default: claude-haiku-4-5)
  --max-turns N            Max turns per agent run (default: 100)
  --verify-turns N         Max turns for verify phase (default: 120)
  -v, --verbose            Stream agent output to terminal in real time
  --rerun STORY [...]      Force reprocessing of specific stories

Container image layers:
  1. factory2-base          Common: Fedora, Claude CLI, git, Python, user setup
  2. factory2-{profile}     Profile: language toolchain (e.g. rust, cargo, clippy)
  3. factory2 (optional)    User: extra tools from <specs-dir>/Containerfile

  To add project-specific tools (e.g., dnsmasq), create a Containerfile
  alongside your specs directory:

    FROM factory2-rust
    USER root
    RUN dnf install -y dnsmasq
    USER factory

Authentication: set EITHER Anthropic API key OR Vertex AI env vars before running.

  # Anthropic API
  export ANTHROPIC_API_KEY="sk-ant-..."

  # OR Vertex AI
  export CLAUDE_CODE_USE_VERTEX=1
  export CLOUD_ML_REGION=us-east5
  export ANTHROPIC_VERTEX_PROJECT_ID=my-project

Examples:
  ./run_container.sh ./my-specs
  ./run_container.sh ./my-specs -p python
  ./run_container.sh ./my-specs -- -j 4 --strong-model claude-opus-4-6
  ./run_container.sh ./my-specs -o ./output -- -v --fast-model claude-haiku-4-5-20251001
EOF
    exit 0
}

# ─── Defaults ────────────────────────────────────────────────────────

OUTPUT_DIR="./factory-output"
IMAGE_PREFIX="factory2"
PROFILE="rust"
FORCE_BUILD=false
RUNTIME=""
SPECS_DIR=""
FACTORY_ARGS=()

# ─── Parse Args ──────────────────────────────────────────────────────

while [ $# -gt 0 ]; do
    case "$1" in
        -o|--output)   OUTPUT_DIR="$2"; shift 2 ;;
        -p|--profile)  PROFILE="$2"; shift 2 ;;
        -i|--image)    IMAGE_PREFIX="$2"; shift 2 ;;
        -b|--build)    FORCE_BUILD=true; shift ;;
        -R|--runtime)  RUNTIME="$2"; shift 2 ;;
        -h|--help)     usage ;;
        --)            shift; FACTORY_ARGS=("$@"); break ;;
        -*)            echo "ERROR: Unknown option: $1" >&2; usage ;;
        *)
            if [ -z "$SPECS_DIR" ]; then
                SPECS_DIR="$1"; shift
            else
                echo "ERROR: Unexpected argument: $1" >&2; usage
            fi
            ;;
    esac
done

if [ -z "$SPECS_DIR" ]; then
    echo "ERROR: specs-dir is required" >&2
    usage
fi

SPECS_DIR="$(cd "$SPECS_DIR" && pwd)"
mkdir -p "$OUTPUT_DIR"
OUTPUT_DIR="$(cd "$OUTPUT_DIR" && pwd)"

# ─── Detect Container Runtime ───────────────────────────────────────

if [ -z "$RUNTIME" ]; then
    if command -v podman &>/dev/null; then
        RUNTIME="podman"
    elif command -v docker &>/dev/null; then
        RUNTIME="docker"
    else
        echo "ERROR: No container runtime found. Install podman or docker." >&2
        exit 1
    fi
fi

echo "Using container runtime: $RUNTIME"

# ─── Image Names ───────────────────────────────────────────────────

BASE_IMAGE="${IMAGE_PREFIX}-base"
PROFILE_IMAGE="${IMAGE_PREFIX}-${PROFILE}"
RUN_IMAGE="${IMAGE_PREFIX}"

# ─── Build Images ──────────────────────────────────────────────────

image_exists() {
    $RUNTIME image inspect "$1" &>/dev/null 2>&1
}

# Layer 1: base image
PROFILE_CONTAINERFILE="$SCRIPT_DIR/profiles/$PROFILE/Containerfile"
if [ ! -f "$PROFILE_CONTAINERFILE" ]; then
    echo "ERROR: No Containerfile for profile '$PROFILE' at $PROFILE_CONTAINERFILE" >&2
    echo "Available profiles:" >&2
    for d in "$SCRIPT_DIR"/profiles/*/Containerfile; do
        [ -f "$d" ] && echo "  $(basename "$(dirname "$d")")" >&2
    done
    exit 1
fi

if $FORCE_BUILD || ! image_exists "$BASE_IMAGE"; then
    echo "Building base image '$BASE_IMAGE'..."
    $RUNTIME build -t "$BASE_IMAGE" -f "$SCRIPT_DIR/Containerfile.base" "$SCRIPT_DIR"
fi

# Layer 2: profile image
if $FORCE_BUILD || ! image_exists "$PROFILE_IMAGE"; then
    echo "Building profile image '$PROFILE_IMAGE'..."
    $RUNTIME build -t "$PROFILE_IMAGE" -f "$PROFILE_CONTAINERFILE" "$SCRIPT_DIR"
fi

# Layer 3: user image (optional — from specs-dir/Containerfile)
USER_CONTAINERFILE="$SPECS_DIR/Containerfile"
if [ -f "$USER_CONTAINERFILE" ]; then
    echo "Building user image '$RUN_IMAGE' from $USER_CONTAINERFILE..."
    $RUNTIME build -t "$RUN_IMAGE" -f "$USER_CONTAINERFILE" "$SPECS_DIR"
elif [ "$PROFILE_IMAGE" != "$RUN_IMAGE" ]; then
    $RUNTIME tag "$PROFILE_IMAGE" "$RUN_IMAGE"
fi

# ─── Validate Environment ───────────────────────────────────────────

if [ -z "${ANTHROPIC_API_KEY:-}" ] && [ -z "${CLAUDE_CODE_USE_VERTEX:-}" ]; then
    echo "ERROR: Set ANTHROPIC_API_KEY or Vertex AI env vars (CLAUDE_CODE_USE_VERTEX, CLOUD_ML_REGION, ANTHROPIC_VERTEX_PROJECT_ID)" >&2
    exit 1
fi

# ─── Build auth-related container args ──────────────────────────────

AUTH_ARGS=()
cred_src=""

if [ -n "${CLAUDE_CODE_USE_VERTEX:-}" ]; then
    AUTH_ARGS+=(-e CLAUDE_CODE_USE_VERTEX)
    AUTH_ARGS+=(-e CLOUD_ML_REGION)
    AUTH_ARGS+=(-e ANTHROPIC_VERTEX_PROJECT_ID)

    # Pass through optional Vertex env vars
    [ -n "${ANTHROPIC_VERTEX_REGION:-}" ]    && AUTH_ARGS+=(-e ANTHROPIC_VERTEX_REGION)

    if [ -n "${GOOGLE_APPLICATION_CREDENTIALS:-}" ] && [ -f "${GOOGLE_APPLICATION_CREDENTIALS}" ]; then
        cred_src="$(realpath "${GOOGLE_APPLICATION_CREDENTIALS}")"
    elif [ -f "${HOME}/.config/gcloud/application_default_credentials.json" ]; then
        cred_src="$(realpath "${HOME}/.config/gcloud/application_default_credentials.json")"
    else
        echo "WARN: No GCP credentials found. The container will rely on metadata-server / workload identity." >&2
    fi

    # Outside GCE the metadata server (169.254.169.254) is unreachable.
    # google-auth-library probes it on every auth and the TCP SYN hangs
    # for minutes. Point it at localhost so the probe fails instantly.
    if [ -n "$cred_src" ]; then
        AUTH_ARGS+=(-e GCE_METADATA_HOST=0.0.0.0)
    fi
else
    AUTH_ARGS+=(-e ANTHROPIC_API_KEY)
fi

# ─── Prepare Workspace ──────────────────────────────────────────────

PROJECT_DIR="$OUTPUT_DIR"
mkdir -p "$PROJECT_DIR"

# Reclaim ownership from previous container runs that used subuid remapping.
# With --userns=keep-id this is only needed once to migrate old workspaces.
if [ "$RUNTIME" = "podman" ]; then
    $RUNTIME unshare chown -R 0:0 "$PROJECT_DIR" 2>/dev/null || true
fi

# Mount credentials directly into the container (never copy into project dir).
if [ -n "${cred_src:-}" ]; then
    AUTH_ARGS+=(-v "$cred_src:/run/secrets/gcp-credentials.json:ro,z")
    AUTH_ARGS+=(-e GOOGLE_APPLICATION_CREDENTIALS=/run/secrets/gcp-credentials.json)
fi

# ─── Run ─────────────────────────────────────────────────────────────

echo ""
echo "Starting factory..."
echo "  Specs:       $SPECS_DIR"
echo "  Project:     $PROJECT_DIR"
echo "  Profile:     $PROFILE"
echo "  Runtime:     $RUNTIME"
echo "  Image:       $RUN_IMAGE"
echo ""

# Inject --specs and --profile into factory args
FACTORY_ARGS=("--specs" "/specs" "--profile" "$PROFILE" "${FACTORY_ARGS[@]+"${FACTORY_ARGS[@]}"}")

# Remove stale container with this name (from a previous interrupted run)
$RUNTIME rm -f factory2-run 2>/dev/null || true

USERNS_ARGS=()
if [ "$RUNTIME" = "podman" ]; then
    # Map host UID into container so files have correct ownership on both sides.
    # No more subuid remapping, no post-run chown needed.
    USERNS_ARGS=(--userns=keep-id)
fi

$RUNTIME run --rm \
    --name factory2-run \
    --cap-add=NET_ADMIN \
    --cap-add=SYS_ADMIN \
    "${USERNS_ARGS[@]+"${USERNS_ARGS[@]}"}" \
    -v "$PROJECT_DIR:/workspace:z" \
    -v "$SPECS_DIR:/specs:ro,z" \
    "${AUTH_ARGS[@]}" \
    -e SKIP_PERMISSIONS="${SKIP_PERMISSIONS:-1}" \
    -e PYTHONPATH=/factory \
    "$RUN_IMAGE" \
    /workspace "${FACTORY_ARGS[@]+"${FACTORY_ARGS[@]}"}"

echo ""
echo "Factory complete."
echo "  Project:   $PROJECT_DIR/"
echo "  State:     $PROJECT_DIR/.factory/"
echo "  Results:   $PROJECT_DIR/.factory/output/"
