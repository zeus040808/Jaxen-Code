FROM docker:27.5.1-cli AS docker-cli

FROM node:24.19.0-bookworm-slim

COPY --from=docker-cli /usr/local/bin/docker /usr/local/bin/docker
COPY --from=docker-cli /usr/local/libexec/docker/cli-plugins/ /usr/local/libexec/docker/cli-plugins/

RUN node --version && docker --version && docker buildx version

WORKDIR /work

