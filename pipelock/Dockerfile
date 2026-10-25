# Pipelock, the per-stack egress proxy agents' tools reach through HTTPS_PROXY.
# Built from source without the `enterprise` build tag, so the binary is the
# Apache-2.0 Community edition; the published image also links the ELv2
# enterprise/ tree. Built by lettucectl; needs no build context beyond this dir.
FROM golang:1.27.2-bookworm AS build
# A tag can be moved; the commit it pointed to when we pinned it cannot.
ARG PIPELOCK_VERSION=3.6.0
ARG PIPELOCK_COMMIT=3e868ac5d5b62d3a2790958542171143af8a0e38
WORKDIR /src
RUN git clone --quiet --depth 1 --branch "v${PIPELOCK_VERSION}" \
      https://github.com/pipelab-org/pipelock.git . \
 && test "$(git rev-parse HEAD)" = "${PIPELOCK_COMMIT}"
# Fail the build if anything under enterprise/ (ELv2) would be linked in.
RUN go list -deps ./cmd/pipelock > /tmp/deps && ! grep -q '/pipelock/enterprise' /tmp/deps
RUN CGO_ENABLED=0 go build -trimpath -ldflags "-s -w \
      -X github.com/luckyPipewrench/pipelock/internal/cliutil.Version=${PIPELOCK_VERSION} \
      -X github.com/luckyPipewrench/pipelock/internal/cliutil.GitCommit=${PIPELOCK_COMMIT} \
      -X github.com/luckyPipewrench/pipelock/internal/cliutil.GoVersion=$(go env GOVERSION) \
      -X github.com/luckyPipewrench/pipelock/internal/proxy.Version=${PIPELOCK_VERSION}" \
      -o /out/pipelock ./cmd/pipelock

# Alpine rather than scratch: the entrypoint needs a shell to create the
# signing key on first start.
FROM alpine:3.24.2
RUN apk add --no-cache ca-certificates
COPY --from=build /out/pipelock /usr/local/bin/pipelock
COPY pipelock.yaml /etc/pipelock/pipelock.yaml
COPY entrypoint.sh /usr/local/bin/entrypoint.sh
# A fixed non-root user that owns /data, so the named volume Docker creates on
# first start inherits that ownership.
RUN addgroup -S -g 65532 pipelock \
 && adduser -S -D -H -u 65532 -G pipelock -s /sbin/nologin pipelock \
 && mkdir /data && chown 65532:65532 /data && chmod 700 /data
USER 65532
VOLUME /data
EXPOSE 8888
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
  CMD ["pipelock", "healthcheck"]
ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
