#!/usr/bin/env bash
set -euo pipefail

if [[ "$#" -ne 2 ]]; then
  echo "usage: $0 IMAGE EXPECTED_REVISION" >&2
  exit 2
fi

image="$1"
expected_revision="$2"

if [[ "$(docker image inspect --format '{{.Os}}/{{.Architecture}}' "$image")" != "linux/arm64" ]]; then
  echo "lidar-processing image must be linux/arm64" >&2
  exit 1
fi
if [[ "$(docker image inspect --format '{{.Config.User}}' "$image")" != "10001:10001" ]]; then
  echo "lidar-processing image user must be 10001:10001" >&2
  exit 1
fi
if [[ "$(docker image inspect --format '{{index .Config.Labels "org.opencontainers.image.revision"}}' "$image")" != "$expected_revision" ]]; then
  echo "lidar-processing image source revision differs" >&2
  exit 1
fi

docker run --rm --platform linux/arm64 "$image" --help >/dev/null
docker run --rm --platform linux/arm64 --entrypoint python "$image" -c \
  'import ajin_lidar_processing.runtime, ajin_edge.wire.lidar_pb2_grpc'

if docker run --rm --platform linux/arm64 --entrypoint sh "$image" -c \
  'command -v uv || command -v uvx || command -v gcc || command -v make'; then
  echo "lidar-processing runtime image contains a build tool" >&2
  exit 1
fi
